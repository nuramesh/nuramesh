"""Stored model keys: set, rotate and revoke as ledgered actions (spec r114c 6, 7).

The ledger is the truth; the secret store holds the values. **Vault I/O never runs inside a
database transaction** (it would hold the write lock for a slow call), so a rotate is three short
steps: (1) read the fold, check, `put` the new version to the store; (2) ONE transaction that
re-reads the fold under the write lock and appends `model.key_stored` only if nothing about the
key moved since the request read the head (`KeyFold.append_refused`: another live pending version,
or a stored, activated or revoked entry after the head); (3) test the version, then ONE
transaction that activates it only if it is still pending. A request refused at step 2 compensates
its own put (`key_destroyed` with `cause: rotation_lost`, intent first, then disable and destroy).

Numbers are `max(count of key_stored, highest listed in the store, highest destroyed) + 1` and
are never reused; a `put` that answers `conflict` is retried with the next number, at most three
puts in all (`version_conflict`). Every refusal is a `KeyRefused` with a fixed code and text; the
value is never in a message, an event or a log. Only a `human:` actor may write (the route also
checks the owner role and the MFA rules, which are not here).
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from nuramesh.core.content import LedgerHashKind, MarkedLedgerHasher
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.model_keys import (
    DEFAULT_GRACE_SECONDS,
    ENABLED_STATES,
    MAX_GRACE_SECONDS,
    KeyFold,
    is_human,
)
from nuramesh.core.secret_store import SecretRef, SecretStore, SecretStoreError
from nuramesh.core.secrets_text import Redacted, reveal_secret
from nuramesh.engine.content import ContentError
from nuramesh.engine.key_file import KEY_SHAPE
from nuramesh.engine.org_ledger import PAGE, org_stream
from nuramesh.stores.base import EventDraft, Store, UnitOfWork

MAX_PUTS = 3
READER_ACTOR = "system:key-reader"
TEXTS = {
    "forbidden": "only a person may change a stored key",
    "key_shape": "the key is not usable",
    "invalid_grace": "grace_seconds is between 0 and 604800",
    "org_key_unavailable": "the organization's ledger key is not available",
    "rotation_in_progress": "another change of this key is in progress",
    "version_conflict": "the key version could not be stored",
    "version_not_pending": "the key version is no longer pending",
    "unknown_version": "that key version is not known",
    "unknown_key": "that key is not known",
    "secret_store_unavailable": "the secret store is not available",
    "org_erased": "the organization's data has been erased",
}
_STATUS = {
    "forbidden": 403,
    "key_shape": 422,
    "invalid_grace": 422,
    "org_key_unavailable": 409,
    "rotation_in_progress": 409,
    "version_conflict": 409,
    "version_not_pending": 409,
    "unknown_version": 404,
    "unknown_key": 404,
    "secret_store_unavailable": 503,
    "org_erased": 409,
}


class KeyRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """A key change was refused; nothing was appended unless the code says otherwise. The text is
    fixed by the code and never quotes the request."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.status = _STATUS[code]
        super().__init__(TEXTS[code])


@dataclass(frozen=True)
class KeyChange:
    """What a set or rotate did."""

    key_id: str
    version: int
    state: str
    """`current` once activated, `failed` when the connection test failed."""
    created: bool
    """True for the first key of a connection (the route answers 201), False for a rotate (200)."""
    failure_code: str | None = None
    previous_version: int | None = None


@dataclass(frozen=True)
class Revoked:
    key_id: str
    versions: tuple[int, ...]
    """The versions this call revoked (empty when everything asked for was already revoked)."""


HasherFor = Callable[[UnitOfWork | None], MarkedLedgerHasher]
VersionTest = Callable[[SecretRef], str | None]
"""The connection test bound to one version: None when it passed, else a fixed failure code."""


def _text(value: Redacted) -> str:
    """The one place this file reveals a value (shape check and keyed fingerprint)."""
    return reveal_secret(value)


def new_key_id() -> str:
    return "mk_" + secrets.token_hex(10)


class ModelKeys:
    """Set, rotate and revoke the stored keys of one organization."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        secrets_store: SecretStore,
        hasher_for: HasherFor,
        *,
        test: VersionTest | None = None,
        fixed_text: str = "",
        clock: Callable[[], datetime] | None = None,
        key_ids: Callable[[], str] = new_key_id,
    ) -> None:
        self.store, self.org_id, self.secrets = store, org_id, secrets_store
        self.stream = org_stream(org_id)
        self._hasher_for = hasher_for
        self._test = test if test is not None else (lambda _ref: None)
        self._fixed_text = fixed_text
        self._clock = clock if clock is not None else store.now
        self._key_ids = key_ids
        self.after_early_check: Callable[[], None] | None = None
        """A test hook run between the early checks and the first `put` (the race point)."""
        self.before_append: Callable[[], None] | None = None
        """A test hook run between a successful `put` and the conditional append."""

    # -- reading ---------------------------------------------------------------------------

    def fold(self, uow: UnitOfWork | None = None) -> KeyFold:
        """The key state now, from the org stream (committed, or inside `uow`)."""
        return self._read(uow)[0]

    def _read(self, uow: UnitOfWork | None) -> tuple[KeyFold, int]:
        fold, head = KeyFold(), 0
        while True:
            if uow is not None:
                page: list[LedgerEvent] = uow.events_after(self.stream, head, PAGE)
            else:
                page = list(self.store.read_stream(self.stream, after_seq=head, limit=PAGE))
            for event in page:
                fold.apply(event)
                head = event.seq
            if len(page) < PAGE:
                return fold, head

    # -- set and rotate --------------------------------------------------------------------

    def set_key(
        self,
        connection: str,
        value: Redacted,
        *,
        by: str,
        key_id: str | None = None,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        via: str = "api",
        mfa: bool = False,
        on_first_activation: Callable[[UnitOfWork, str], None] | None = None,
        reason_hash_for: Callable[[UnitOfWork], str] | None = None,
    ) -> KeyChange:
        """Store a new version of the connection's key (`key_id`, or a fresh one for a first
        set), test it and activate it. `on_first_activation(uow, key_id)` runs inside the
        activation transaction of a FIRST key (the route writes the connection update there)."""
        if not is_human(by):
            raise KeyRefused("forbidden")
        if not 0 <= grace_seconds <= MAX_GRACE_SECONDS:
            raise KeyRefused("invalid_grace")
        text = _text(value)
        if not KEY_SHAPE.fullmatch(text) or text in self._fixed_text:
            raise KeyRefused("key_shape")
        fingerprint, marker = self._fingerprint(text)
        fold, head = self._read(None)
        now = self._clock()
        if fingerprint in fold.enabled_fingerprints(marker["hash_key_id"], now):
            raise KeyRefused("key_shape")
        first = key_id is None
        key = self._key_ids() if key_id is None else key_id
        if not first and fold.has_live_pending(key, now):
            raise KeyRefused("rotation_in_progress")
        if self.after_early_check is not None:
            self.after_early_check()
        self._refuse_if_erased()
        ref = self._put(key, value, fold)
        if self.before_append is not None:
            self.before_append()
        self._append_stored(ref, connection, fingerprint, marker, head, by=by, via=via, mfa=mfa)
        failure = self._run_test(ref)
        if failure is not None:
            return self._failed(ref, connection, failure, by=by, via=via)
        return self._activate(
            ref,
            connection,
            by=by,
            via=via,
            mfa=mfa,
            grace_seconds=grace_seconds,
            on_first_activation=on_first_activation,
            reason_hash_for=reason_hash_for,
        )

    def _refuse_if_erased(self) -> None:
        """An erased organization (a shred tombstone) never gets a new key value (review M1)."""
        if self.store.key_tombstone(self.org_id) is not None:
            raise KeyRefused("org_erased")

    def _fingerprint(self, text: str) -> tuple[str, dict[str, str]]:
        code = ""
        digest, marker = "", {}
        try:
            hasher = self._hasher_for(None)
            digest = hasher(LedgerHashKind.MODEL_KEY, text.encode())
            marker = dict(hasher.marker())
        except ContentError:
            code = "org_key_unavailable"
        if code:
            raise KeyRefused(code)
        return digest, marker

    def value_matches(
        self, value: Redacted, fingerprint: str | None, hash_key_id: str | None
    ) -> bool:
        """Whether `value` is the material the ledger recorded (see `check_value`); anything but
        a match, including an unavailable ledger key, is False (fail closed)."""
        return self.check_value(value, fingerprint, hash_key_id) == "match"

    def check_value(
        self, value: Redacted, fingerprint: str | None, hash_key_id: str | None
    ) -> Literal["match", "mismatch", "unavailable"]:
        """Whether `value` is the material the ledger recorded: its keyed fingerprint equals the
        one in `model.key_stored`. A row made under another org-key id cannot be checked (the
        same skip as the duplicate check, spec 6 step 1) and counts as a match; a ledger key that
        is not available is `unavailable` (the caller refuses and writes no `key_mismatch`). The
        one read of the stored value for this check goes through `_text`, the one reveal site of
        this file."""
        if fingerprint is None:
            return "mismatch"
        code = ""
        digest = ""
        marker: dict[str, str] = {}
        try:
            digest, marker = self._fingerprint(_text(value))
        except KeyRefused as exc:
            code = exc.code
        if code:
            return "unavailable"
        if hash_key_id is not None and marker.get("hash_key_id") != hash_key_id:
            return "match"
        return "match" if digest == fingerprint else "mismatch"

    def record_read_failed(self, key_id: str, version: int, code: str) -> None:
        """Append `model.key_read_failed` (a record, never authority) unless one was written for
        the same version inside the last 60 seconds. Carries the connection, the ids and a fixed
        code; never a value."""
        with self.store.transaction() as uow:
            fold, _ = self._read(uow)
            key = fold.key(key_id)
            if key is None or fold.erasure_last is not None:
                return  # nothing is written after an erasure marker
            now = self._clock()
            found = key.versions.get(version)
            if (
                found is None
                or found.destroyed
                or found.state(now) not in ENABLED_STATES
                or not fold.read_failed_due(key_id, version, now)
            ):
                return  # a revoked, destroyed or expired version is not a failed read
            uow.append(
                EventDraft(
                    stream=self.stream,
                    type=EventType.MODEL_KEY_READ_FAILED,
                    actor=READER_ACTOR,
                    payload={
                        "connection": key.connection,
                        "key_id": key_id,
                        "version": version,
                        "code": code,
                    },
                )
            )

    def _listed_max(self, key_id: str) -> int:
        listed: list[int] = []
        code = ""
        try:
            listed = [m.ref.version for m in self.secrets.versions(self.org_id, key_id)]
        except SecretStoreError:
            code = "secret_store_unavailable"
        if code:
            raise KeyRefused(code)
        return max(listed, default=0)

    def _put(self, key_id: str, value: Redacted, fold: KeyFold) -> SecretRef:
        """Put the next version; on `conflict` retry above the conflicted number (3 puts)."""
        version = fold.next_version(key_id, self._listed_max(key_id))
        for attempt in range(MAX_PUTS):
            ref = SecretRef(org_id=self.org_id, key_id=key_id, version=version)
            code = ""
            try:
                self.secrets.put(ref, value)
            except SecretStoreError as exc:
                code = exc.code
            if code == "":
                return ref
            if code != "conflict":
                raise KeyRefused("secret_store_unavailable")
            if attempt == MAX_PUTS - 1:
                break
            fresh = self._read(None)[0]
            version = max(version + 1, fresh.next_version(key_id, self._listed_max(key_id)))
        raise KeyRefused("version_conflict")

    def _append_stored(
        self,
        ref: SecretRef,
        connection: str,
        fingerprint: str,
        marker: dict[str, str],
        head: int,
        *,
        by: str,
        via: str,
        mfa: bool,
    ) -> None:
        """The conditional append. A refused request compensates its own put and answers 409."""
        refused = ""
        with self.store.transaction() as uow:
            fold, _ = self._read(uow)
            if uow.key_tombstone(self.org_id) is not None:
                # a shred committed while the value was being put: the ledger's erasure marker
                # must not stay "verified" over a live value, so void it and refuse (review M1)
                refused = "org_erased"
                # always write the pending entry: it moves the ledger, so a sweeper that listed
                # before this put cannot verify over it (review D1)
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_ERASURE_PENDING,
                        actor=by,
                        payload={"via": "set_key"},
                    )
                )
            elif fold.append_refused(ref.key_id, head, self._clock(), ref.version):
                refused = "rotation_in_progress"
            else:
                payload: dict[str, Any] = {
                    "connection": connection,
                    "key_id": ref.key_id,
                    "version": ref.version,
                    "fingerprint": fingerprint,
                    **marker,
                    "via": via,
                }
                if mfa:
                    payload["mfa"] = True
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_STORED,
                        actor=by,
                        payload=payload,
                    )
                )
        if refused:
            self._compensate(ref, by=by, via=via)
            raise KeyRefused(refused)

    def _compensate(self, ref: SecretRef, *, by: str, via: str) -> None:
        """Intent first: `key_destroyed {cause: rotation_lost}`, then disable and destroy. A store
        failure is left to the sweeper (the request still answers 409)."""
        with self.store.transaction() as uow:  # decide AND record under the write lock
            fold, _ = self._read(uow)
            key = fold.keys.get(ref.key_id)
            # idempotent per (key_id, version): the sweeper may have destroyed this number already
            if key is None or ref.version not in key.destroyed_versions:
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_DESTROYED,
                        actor=by,
                        payload={
                            "key_id": ref.key_id,
                            "version": ref.version,
                            "via": via,
                            "cause": "rotation_lost",
                        },
                    )
                )
        self._remove(ref)

    def _remove(self, ref: SecretRef) -> None:
        """Disable then destroy, outside any transaction; errors are the sweeper's to repair."""
        for step in (self.secrets.disable, self.secrets.destroy):
            try:
                step(ref)
            except SecretStoreError:
                pass

    def _run_test(self, ref: SecretRef) -> str | None:
        code: str | None = None
        try:
            code = self._test(ref)
        except Exception:  # a test that raises is a failed test, never a crash with a value
            code = "test_error"
        return code

    def _failed(
        self, ref: SecretRef, connection: str, code: str, *, by: str, via: str
    ) -> KeyChange:
        with self.store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=self.stream,
                    type=EventType.MODEL_KEY_TEST_FAILED,
                    actor=by,
                    payload={
                        "connection": connection,
                        "key_id": ref.key_id,
                        "version": ref.version,
                        "code": code,
                        "via": via,
                    },
                )
            )
        try:
            self.secrets.disable(ref)
        except SecretStoreError:
            pass  # the fold already refuses it; the sweeper repairs the store
        return KeyChange(ref.key_id, ref.version, "failed", False, failure_code=code)

    def _activate(
        self,
        ref: SecretRef,
        connection: str,
        *,
        by: str,
        via: str,
        mfa: bool,
        grace_seconds: int,
        on_first_activation: Callable[[UnitOfWork, str], None] | None,
        reason_hash_for: Callable[[UnitOfWork], str] | None = None,
    ) -> KeyChange:
        previous: int | None = None
        pending, erased = True, False
        with self.store.transaction() as uow:
            fold, _ = self._read(uow)
            now = self._clock()
            if uow.key_tombstone(self.org_id) is not None:
                erased = True  # a shred landed after key_stored: nothing is activated (review d1)
            elif fold.state(ref.key_id, ref.version, now) != "pending":
                pending = False
            else:
                key = fold.key(ref.key_id)
                previous = None if key is None else key.current_version
                payload: dict[str, Any] = {
                    "connection": connection,
                    "key_id": ref.key_id,
                    "version": ref.version,
                    "grace_seconds": grace_seconds,
                    "activated_at": now.isoformat(),
                    "via": via,
                }
                if previous is not None:
                    payload["previous_version"] = previous
                    if reason_hash_for is not None:  # a rotate's optional reason (hash only)
                        payload["reason_hash"] = reason_hash_for(uow)
                if mfa:
                    payload["mfa"] = True
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_ACTIVATED,
                        actor=by,
                        payload=payload,
                    )
                )
                if previous is None and on_first_activation is not None:
                    on_first_activation(uow, ref.key_id)
        if erased:
            raise KeyRefused("org_erased")
        if not pending:
            raise KeyRefused("version_not_pending")
        return KeyChange(ref.key_id, ref.version, "current", previous is None, None, previous)

    # -- revoke ----------------------------------------------------------------------------

    def revoke(
        self,
        connection: str,
        key_id: str,
        *,
        by: str,
        reason_hash: str | None = None,
        version: int | None = None,
        via: str = "api",
        mfa: bool = False,
        reason_hash_for: Callable[[UnitOfWork], str] | None = None,
    ) -> Revoked:
        """Revoke one version, or every version (`version=None`). Idempotent: revoking what is
        already revoked writes nothing. A version with no `key_stored` is `unknown_version`."""
        if not is_human(by):
            raise KeyRefused("forbidden")
        revoked: list[int] = []
        with self.store.transaction() as uow:
            fold, _ = self._read(uow)
            key = fold.key(key_id)
            if key is None or not key.versions:
                raise KeyRefused("unknown_key" if version is None else "unknown_version")
            if version is not None and version not in key.versions:
                raise KeyRefused("unknown_version")
            wanted = [version] if version is not None else sorted(key.versions)
            revoked = [n for n in wanted if not key.versions[n].revoked]
            if revoked:
                # the reason is captured in this transaction, so a failed revoke leaves no row
                kept = reason_hash_for(uow) if reason_hash_for is not None else reason_hash
                payload: dict[str, Any] = {
                    "connection": connection,
                    "key_id": key_id,
                    "version": "all" if version is None else version,
                    "reason_hash": kept,
                    "via": via,
                }
                if mfa:
                    payload["mfa"] = True
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_REVOKED,
                        actor=by,
                        payload=payload,
                    )
                )
            destroyed = set(key.destroyed_versions)
        for number in revoked:
            if number not in destroyed:
                try:
                    self.secrets.disable(
                        SecretRef(org_id=self.org_id, key_id=key_id, version=number)
                    )
                except SecretStoreError:
                    pass  # the fold refuses it already; the sweeper repairs the store
        return Revoked(key_id, tuple(revoked))
