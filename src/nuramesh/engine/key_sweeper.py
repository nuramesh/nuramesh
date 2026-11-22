"""The key sweeper and the org-erasure hook (R1.14c spec 6 steps 4 and 7; open core).

The sweeper removes the secret MATERIAL of versions that must no longer exist (revoked, expired,
failed, abandoned, orphaned) from the secret store; the ledger is never edited (append-only). It
runs every 60 s as `system:key-sweeper`, which holds no authority in the fold (a record only).

* **Intent first.** `model.key_destroyed` is appended BEFORE the store is touched, in a transaction
  that re-reads the fold and re-decides under the write lock; then `disable` and `destroy` run
  outside any transaction. A crash after the event leaves a version the store still lists, and the
  next run destroys it without a second event (the STORE decides whether a value is present).
* **Fail closed and per org.** Every pass works on ONE organization through `list_versions(org)`
  and refs built from that org's id, so another org's material is never read or touched. A store
  that cannot answer means nothing is destroyed for that org in this run.
* **One sweeper at a time** per deployment, through a kernel-released lock (`SweepLock`).
* **Erased orgs.** An org with a shred tombstone and no `key_erasure_verified` marker is erased
  again on every run until the store lists nothing, then the marker is written once and the org is
  skipped from then on (no store call). The hook runs the same erasure right after a shred commits.

Known deviations and limits (recorded, not hidden):

* A partial destroy failure writes no `key_erased`; the next run counts only the versions still
  listed, so `count` is the number destroyed in the completing run, not a total for the org.
* The hook's `verified` check and the sweeper's `verified` write are separate transactions, so
  a narrow race between them is possible; the marker is only a skip hint, and the next shred or
  sweeper run lists the store again and repairs it.

No payload carries a value, a path or free text (`KEY_PAYLOAD_KEYS`).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.model_catalog import ConnectionsState, apply_connection_event
from nuramesh.core.model_keys import ABANDON_AFTER, SWEEPER_ACTOR, KeyFold
from nuramesh.core.secret_store import SecretMeta, SecretStore, SecretStoreError
from nuramesh.engine.content import ContentService
from nuramesh.engine.org_ledger import PAGE, org_stream
from nuramesh.stores.base import EventDraft, Store, UnitOfWork

_LOG = logging.getLogger("nuramesh.key_sweeper")
ORG_PREFIX = "org:"
VERIFIED = EventType.MODEL_KEY_ERASURE_VERIFIED.value
PENDING = EventType.MODEL_KEY_ERASURE_PENDING.value
DESTROYABLE = frozenset({"revoked", "failed", "abandoned", "expired"})
POSTGRES_LOCK_KEY = 7_283_410_077
"""The advisory lock key of the sweeper (the write lock of the store has another one)."""


class SweepLock(Protocol):
    """At most one holder; a holder that dies releases it (never a pid file or a marker)."""

    def hold(self) -> contextlib.AbstractContextManager[bool]:
        """Context manager that yields True when this caller got the lock, False when not."""


class ProcessSweepLock:
    """In one process (memory store, tests)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def hold(self) -> Iterator[bool]:
        got = self._lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                self._lock.release()


class FileSweepLock:
    """`fcntl.flock` on a lock file next to the SQLite database: the kernel releases it when the
    process dies (SIGKILL included), and a file left on disk does not block anyone."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextlib.contextmanager
    def hold(self) -> Iterator[bool]:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        got = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                got = False
            yield got
        finally:
            os.close(fd)  # closing drops the flock


class PostgresSweepLock:
    """A session-level advisory lock on its own connection: the database releases it when the
    session ends, for any reason."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextlib.contextmanager
    def hold(self) -> Iterator[bool]:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as db:
            row = db.execute("SELECT pg_try_advisory_lock(%s)", (POSTGRES_LOCK_KEY,)).fetchone()
            got = bool(row and row[0])
            try:
                yield got
            finally:
                if got:
                    db.execute("SELECT pg_advisory_unlock(%s)", (POSTGRES_LOCK_KEY,))


@dataclass
class SweepReport:
    """What one run did (counts only; never a value, a path or a version list)."""

    ran: bool = False
    destroyed: int = 0
    erased_orgs: int = 0
    verified_orgs: int = 0
    failures: int = 0
    temps_swept: int = 0
    temp_failures: int = 0
    skipped: list[str] = field(default_factory=list)
    """Fixed codes only (`lock_unavailable`); never a message, a path or a value."""
    orgs: list[str] = field(default_factory=list)


def read_state(
    store: Store, org_id: str, uow: UnitOfWork | None = None
) -> tuple[KeyFold, ConnectionsState]:
    """The key fold and the connections of one org from its stream (committed, or inside `uow`)."""
    return _read_all(store, org_id, uow)[:2]


def _read_all(
    store: Store, org_id: str, uow: UnitOfWork | None = None
) -> tuple[KeyFold, ConnectionsState, dict[str, datetime]]:
    """Also the time each key id stopped being the `key_ref` of its connection (the repoint)."""
    stream, head = org_stream(org_id), 0
    fold, connections = KeyFold(), ConnectionsState()
    dropped: dict[str, datetime] = {}
    while True:
        if uow is not None:
            page: list[LedgerEvent] = uow.events_after(stream, head, PAGE)
        else:
            page = list(store.read_stream(stream, after_seq=head, limit=PAGE))
        for event in page:
            fold.apply(event)
            before = _refs(connections)
            apply_connection_event(connections, event)
            for ref, name in before.items():
                now_ref = _refs(connections).get(ref)
                if now_ref is None and ref not in dropped and name in connections.providers:
                    record = connections.providers[name].record
                    if record is not None:
                        dropped[ref] = event.at
            head = event.seq
        if len(page) < PAGE:
            return fold, connections, dropped


def _refs(connections: ConnectionsState) -> dict[str, str]:
    """Key id -> connection key, for every connection that now names a stored key."""
    return {
        c.record.key_ref: name
        for name, c in connections.providers.items()
        if c.record is not None and c.record.key_ref is not None
    }


def read_fold(store: Store, org_id: str, uow: UnitOfWork | None = None) -> KeyFold:
    """The key fold of one org from its stream (committed, or inside `uow`)."""
    return read_state(store, org_id, uow)[0]


def _record(
    uow: UnitOfWork, org_id: str, kind: EventType, actor: str, payload: dict[str, Any]
) -> None:
    uow.append(EventDraft(stream=org_stream(org_id), type=kind, actor=actor, payload=payload))


class OrgErasure:
    """Destroy every stored version of one org in the secret store (idempotent, org-bound)."""

    def __init__(self, store: Store, secrets_store: SecretStore) -> None:
        self.store, self.secrets = store, secrets_store

    def listed(self, org_id: str) -> list[SecretMeta] | None:
        """The org's versions, or None when the store cannot answer (nothing is touched)."""
        found: list[SecretMeta] | None = None
        try:
            found = self.secrets.list_versions(org_id)
        except SecretStoreError:
            _LOG.warning("key_store_unavailable")  # a fixed code: an outage is never silent
            found = None
        return found

    def erase(self, org_id: str, *, actor: str, via: str) -> tuple[int, bool]:
        """Disable and destroy all versions. Returns `(count, complete)`: `count` listed, and
        whether every one was destroyed. `model.key_erased {count}` is written only when at least
        one version was listed and all of them are gone; a failure writes nothing and leaves the
        rest to the next sweeper run (the org stays unverified)."""
        listed = self.listed(org_id)
        if listed is None:
            return 0, False
        complete = True
        for meta in listed:
            if meta.ref.org_id != org_id:  # defence in depth: never touch another org
                complete = False
                continue
            for step in (self.secrets.disable, self.secrets.destroy):
                try:
                    step(meta.ref)
                except SecretStoreError as exc:
                    if exc.code != "not_found":
                        complete = False
        if listed and complete:
            with self.store.transaction() as uow:
                _record(
                    uow,
                    org_id,
                    EventType.MODEL_KEY_ERASED,
                    actor,
                    {"count": len(listed), "via": via},
                )
        return len(listed), complete

    def attach(self, service: ContentService) -> None:
        """Run the hook after every shred of `service` (spec 6 step 7)."""
        service.key_erasure = lambda org_id, actor: self.hook(org_id, actor=actor)

    def hook(self, org_id: str, *, actor: str) -> bool:
        """The hook after a shred commit. Returns `keys_pending` (True when the keys could not be
        destroyed now; the sweeper finishes the job). Never raises, never runs in a transaction."""
        pending = True
        try:
            if read_fold(self.store, org_id).erasure_last == VERIFIED:
                with (
                    self.store.transaction() as uow
                ):  # voids the marker before the store is touched
                    _record(
                        uow,
                        org_id,
                        EventType.MODEL_KEY_ERASURE_PENDING,
                        actor,
                        {"via": "erasure_hook"},
                    )
            _count, complete = self.erase(org_id, actor=actor, via="erasure_hook")
            pending = not complete
        except Exception:  # noqa: BLE001 - the shred caller keeps the shred's own result
            _LOG.warning("key_erasure_hook_failed")  # a fixed code: no value, no text, no trace
            pending = True
        return pending


class KeySweeper:
    """One run destroys what must go and repairs erased orgs; see the module text."""

    def __init__(
        self,
        store: Store,
        secrets_store: SecretStore,
        *,
        lock: SweepLock | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.secrets = store, secrets_store
        self._lock: SweepLock = lock if lock is not None else ProcessSweepLock()
        self._clock = clock if clock is not None else store.now
        self.erasure = OrgErasure(store, secrets_store)
        self.before_destroy: Callable[[], None] | None = None
        """A test hook between the intent event and the store call (the crash point)."""

    def org_ids(self) -> list[str]:
        """The orgs the server knows: every org stream and every tombstone."""
        found = {s[len(ORG_PREFIX) :] for s in self.store.streams() if s.startswith(ORG_PREFIX)}
        found |= {t.org_id for t in self.store.key_tombstones()}
        return sorted(found)

    def run_once(self) -> SweepReport:
        report = SweepReport()
        with contextlib.ExitStack() as stack:
            try:
                got = stack.enter_context(self._lock.hold())
            except Exception:  # noqa: BLE001 - a lock that cannot be taken skips the run
                report.skipped.append("lock_unavailable")
                _LOG.warning("key_sweep_skipped_lock")  # a fixed code: no message, no trace
                return report
            if not got:
                return report
            report.ran = True
            tombstoned = {t.org_id for t in self.store.key_tombstones()}
            for org_id in self.org_ids():
                try:
                    self._sweep_temps(org_id, report)
                    if org_id in tombstoned:
                        self._repair_erased(org_id, report)
                    else:
                        self._sweep_org(org_id, report)
                except Exception:  # noqa: BLE001 - one org never stops the others
                    report.failures += 1
        return report

    def _sweep_temps(self, org_id: str, report: SweepReport) -> None:
        """Stale temporary put files of this org (optional store method; never aborts the run)."""
        sweep = getattr(self.secrets, "sweep_temps", None)
        if sweep is None:
            return
        try:
            report.temps_swept += int(sweep(org_id))
        except Exception:  # noqa: BLE001 - a temp scan failure leaves the rest of the sweep alone
            report.temp_failures += 1

    # -- erased orgs ---------------------------------------------------------------------

    def _repair_erased(self, org_id: str, report: SweepReport) -> None:
        before = read_fold(self.store, org_id)
        if before.erasure_last == VERIFIED:
            return  # verified: no store call at all
        report.orgs.append(org_id)
        count, complete = self.erasure.erase(org_id, actor=SWEEPER_ACTOR, via="sweeper")
        if not complete:
            report.failures += 1
            return
        if count:
            report.erased_orgs += 1
            return  # key_erased is written; the next run lists once more and verifies
        with self.store.transaction() as uow:
            now = read_fold(self.store, org_id, uow)
            if now.seq != before.seq:
                return  # the org's key ledger moved since the list: retry next run (review D1)
            if now.erasure_last != VERIFIED:
                _record(
                    uow,
                    org_id,
                    EventType.MODEL_KEY_ERASURE_VERIFIED,
                    SWEEPER_ACTOR,
                    {"via": "sweeper"},
                )
                report.verified_orgs += 1

    # -- live orgs -----------------------------------------------------------------------

    def _sweep_org(self, org_id: str, report: SweepReport) -> None:
        listed = self.erasure.listed(org_id)
        if listed is None:
            report.failures += 1
            return
        for meta in listed:
            if meta.ref.org_id != org_id:
                continue
            self._destroy_if_due(org_id, meta, report)

    @staticmethod
    def _cause(
        fold: KeyFold,
        meta: SecretMeta,
        now: datetime,
        connections: ConnectionsState | None = None,
        dropped: dict[str, datetime] | None = None,
    ) -> str | None:
        """Why this listed version must go, from the fold only; None leaves it alone."""
        ref = meta.ref
        key = fold.key(ref.key_id)
        found = None if key is None else key.versions.get(ref.version)
        if found is None:  # no `key_stored`: an orphan once older than 600 s (exactly 600 stays)
            return "orphan" if now - meta.created_at > ABANDON_AFTER else None
        state = found.state(now)
        if state in DESTROYABLE:
            return state
        if state in ("current", "grace") and connections is not None and key is not None:
            # Unreferenced (spec 6 step 4, VK54): the connection no longer names this key id, and
            # its last entry (the repoint) is older than ten minutes. A removed connection has no
            # record and no time here: it is left alone (the owner revokes by key id).
            record = connections.providers.get(key.connection)
            when = None if dropped is None else dropped.get(ref.key_id)
            if (
                record is not None
                and record.record is not None
                and record.record.key_ref != ref.key_id
                and when is not None
                and now - when > ABANDON_AFTER
            ):
                return "unreferenced"
        return None

    def _destroy_if_due(self, org_id: str, meta: SecretMeta, report: SweepReport) -> None:
        ref = meta.ref
        now = self._clock()
        with self.store.transaction() as uow:  # decide AND record under the write lock
            fold, connections, dropped = _read_all(self.store, org_id, uow)
            cause = self._cause(fold, meta, now, connections, dropped)
            if cause is None:
                return
            key = fold.key(ref.key_id)
            if key is None or ref.version not in key.destroyed_versions:
                _record(
                    uow,
                    org_id,
                    EventType.MODEL_KEY_DESTROYED,
                    SWEEPER_ACTOR,
                    {
                        "key_id": ref.key_id,
                        "version": ref.version,
                        "via": "sweeper",
                        "cause": cause,
                    },
                )
        if self.before_destroy is not None:
            self.before_destroy()
        for step in (self.secrets.disable, self.secrets.destroy):
            try:
                step(ref)
            except SecretStoreError as exc:
                if exc.code != "not_found":
                    report.failures += 1
                    return  # the event stays; the next run finishes the job
        report.destroyed += 1
