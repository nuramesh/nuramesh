"""Stored model keys: the fold of their ledger events (spec r114c 4.3, 6, 7).

Key state is NOT a table: it is the fold of the `model.key_*` entries on the organization's stream
(`org:<org>`), like connections (ND92). The fold is a pure function of (events, now): it holds ids,
versions, times, a keyed fingerprint and states, never a value. Only `human:` actors change state
(`key_stored`, `key_test_failed`, `key_activated`, `key_revoked`); `key_destroyed`,
`key_read_failed`, `key_erased` and the erasure records are records, never authority (the sweeper
writes some of them as `system:key-sweeper`).

Per version: `pending` -> `current` -> `grace` (until the successor's `activated_at` +
`grace_seconds`, the instant itself is `expired`) -> `expired`; `pending` -> `failed` or
`abandoned` (still pending more than 600 s after its `key_stored`; exactly 600 s is `pending`);
`revoked` from any state. An activation of a version that is not pending at that moment is
ignored. Pure Python: stdlib and pydantic only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.people import is_human_id

ABANDON_AFTER: Final = timedelta(seconds=600)
"""A pending version older than this (strictly) is `abandoned`; an orphan is measured the same."""
READ_FAILED_WINDOW: Final = timedelta(seconds=60)
SWEEPER_ACTOR: Final = "system:key-sweeper"
MAX_GRACE_SECONDS: Final = 7 * 24 * 3600
DEFAULT_GRACE_SECONDS: Final = 24 * 3600

VersionState = Literal["pending", "current", "grace", "expired", "failed", "abandoned", "revoked"]
ENABLED_STATES: Final = frozenset({"pending", "current", "grace"})

KEY_PAYLOAD_KEYS: Final[dict[str, frozenset[str]]] = {
    "model.key_stored": frozenset(
        {
            "connection",
            "key_id",
            "version",
            "fingerprint",
            "hash_scheme",
            "hash_key_id",
            "hash_org",
            "via",
            "mfa",
        }
    ),
    "model.key_test_failed": frozenset({"connection", "key_id", "version", "code", "via"}),
    "model.key_activated": frozenset(
        {
            "connection",
            "key_id",
            "version",
            "previous_version",
            "grace_seconds",
            "activated_at",
            "reason_hash",
            "via",
            "mfa",
        }
    ),
    "model.key_revoked": frozenset(
        {"connection", "key_id", "version", "reason_hash", "via", "mfa", "cause"}
    ),
    "model.key_destroyed": frozenset({"key_id", "version", "via", "cause"}),
    "model.key_read_failed": frozenset({"connection", "key_id", "version", "code"}),
    "model.key_erased": frozenset({"count", "via"}),
    "model.key_erasure_verified": frozenset({"via"}),
    "model.key_erasure_pending": frozenset({"via"}),
}
"""The closed payload keys of each key entry. None can hold free text or a value; a test appends
through the service and checks it."""

_KEY_EVENTS: Final = frozenset(KEY_PAYLOAD_KEYS)


def is_human(actor: str) -> bool:
    return is_human_id(actor)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class KeyVersion:
    """What the ledger says about one version of one key."""

    version: int
    stored_seq: int
    stored_at: datetime
    fingerprint: str | None
    hash_key_id: str | None
    stored_by: str
    failed: bool = False
    revoked: bool = False
    destroyed: bool = False
    destroyed_cause: str | None = None
    activated_seq: int | None = None
    activated_at: datetime | None = None
    grace_seconds: int = 0
    previous_version: int | None = None
    superseded_at: datetime | None = None
    superseded_grace: int = 0
    last_read_failed_at: datetime | None = None

    def state(self, now: datetime) -> VersionState:
        if self.revoked:
            return "revoked"
        if self.failed:
            return "failed"
        if self.activated_seq is None:
            return "abandoned" if now - self.stored_at > ABANDON_AFTER else "pending"
        if self.superseded_at is None:
            return "current"
        if now < self.superseded_at + timedelta(seconds=self.superseded_grace):
            return "grace"
        return "expired"

    @property
    def grace_until(self) -> datetime | None:
        if self.superseded_at is None:
            return None
        return self.superseded_at + timedelta(seconds=self.superseded_grace)


@dataclass
class StoredKey:
    """One key id (one connection's key): its versions and the counters of the numbering rule."""

    key_id: str
    connection: str
    versions: dict[int, KeyVersion] = field(default_factory=dict)
    stored_count: int = 0
    """A of the numbering rule: every `key_stored` ever written for the key id."""
    destroyed_max: int = 0
    """C of the numbering rule: the highest version of any `key_destroyed` entry."""
    destroyed_versions: set[int] = field(default_factory=set)
    last_seq: int = 0
    """The sequence of the last `stored`, `activated` or `revoked` entry (conditional append)."""
    current_version: int | None = None


@dataclass
class KeyFold:
    """The key state of one organization."""

    keys: dict[str, StoredKey] = field(default_factory=dict)
    erasure_last: str | None = None
    """The last of `key_erased`, `key_erasure_pending`, `key_erasure_verified` in the stream."""
    seq: int = 0

    def apply(self, event: LedgerEvent) -> KeyFold:
        """Apply one entry (in place). Other entries are skipped; a malformed key entry is
        skipped and never raises."""
        if event.type not in _KEY_EVENTS:
            return self
        self.seq = max(self.seq, event.seq)
        payload: Mapping[str, Any] = event.payload
        kind = event.type
        if kind in (
            EventType.MODEL_KEY_ERASED.value,
            EventType.MODEL_KEY_ERASURE_PENDING.value,
            EventType.MODEL_KEY_ERASURE_VERIFIED.value,
        ):
            self.erasure_last = kind
            return self
        key_id = payload.get("key_id")
        if not isinstance(key_id, str):
            return self
        version = payload.get("version")
        if kind == EventType.MODEL_KEY_STORED.value:
            self._stored(event, key_id, version, payload)
        elif kind == EventType.MODEL_KEY_ACTIVATED.value:
            self._activated(event, key_id, version, payload)
        elif kind == EventType.MODEL_KEY_TEST_FAILED.value:
            self._test_failed(event, key_id, version)
        elif kind == EventType.MODEL_KEY_REVOKED.value:
            self._revoked(event, key_id, version)
        elif kind == EventType.MODEL_KEY_DESTROYED.value:
            self._destroyed(key_id, version, payload)
        elif kind == EventType.MODEL_KEY_READ_FAILED.value:
            found = self._version(key_id, version)
            if found is not None:
                found.last_read_failed_at = event.at
        return self

    def apply_all(self, events: Iterable[LedgerEvent]) -> KeyFold:
        for event in events:
            self.apply(event)
        return self

    def _version(self, key_id: str, version: object) -> KeyVersion | None:
        key = self.keys.get(key_id)
        number = _int(version)
        if key is None or number is None:
            return None
        return key.versions.get(number)

    def _stored(
        self, event: LedgerEvent, key_id: str, version: object, p: Mapping[str, Any]
    ) -> None:
        number = _int(version)
        if not is_human(event.actor) or number is None:
            return
        connection = p.get("connection")
        key = self.keys.setdefault(
            key_id, StoredKey(key_id, connection if isinstance(connection, str) else "")
        )
        key.stored_count += 1
        key.last_seq = event.seq
        if number in key.versions:
            return  # a number is never reused: the first entry stays
        fingerprint, hash_key_id = p.get("fingerprint"), p.get("hash_key_id")
        key.versions[number] = KeyVersion(
            version=number,
            stored_seq=event.seq,
            stored_at=event.at,
            fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            hash_key_id=hash_key_id if isinstance(hash_key_id, str) else None,
            stored_by=event.actor,
        )

    def _test_failed(self, event: LedgerEvent, key_id: str, version: object) -> None:
        found = self._version(key_id, version)
        if found is not None and is_human(event.actor) and found.activated_seq is None:
            found.failed = True

    def _activated(
        self, event: LedgerEvent, key_id: str, version: object, p: Mapping[str, Any]
    ) -> None:
        found = self._version(key_id, version)
        if found is None or not is_human(event.actor):
            return
        # only a version that is pending AT THIS MOMENT can be activated; anything else is ignored
        if found.activated_seq is not None or found.failed or found.revoked:
            return
        if event.at - found.stored_at > ABANDON_AFTER:
            return
        key = self.keys[key_id]
        grace = _int(p.get("grace_seconds")) or 0
        grace_seconds = max(0, min(grace, MAX_GRACE_SECONDS))
        found.activated_seq, found.activated_at = event.seq, event.at
        found.grace_seconds = grace_seconds
        if key.current_version is not None:
            previous = key.versions[key.current_version]
            previous.superseded_at, previous.superseded_grace = event.at, grace_seconds
            found.previous_version = previous.version
        key.current_version = found.version
        key.last_seq = event.seq

    def _revoked(self, event: LedgerEvent, key_id: str, version: object) -> None:
        key = self.keys.get(key_id)
        # A connection removal revokes its key whoever removed it (cause `connection_removed`).
        removal = event.payload.get("cause") == "connection_removed"
        if key is None or not (is_human(event.actor) or removal):
            return
        if version == "all":
            for item in key.versions.values():
                item.revoked = True
        else:
            found = self._version(key_id, version)
            if found is None:
                return
            found.revoked = True
        if key.current_version is not None and key.versions[key.current_version].revoked:
            key.current_version = None  # no current version: the next activation is a first one
        key.last_seq = event.seq

    def _destroyed(self, key_id: str, version: object, p: Mapping[str, Any]) -> None:
        number = _int(version)
        if number is None:
            return
        key = self.keys.setdefault(key_id, StoredKey(key_id, ""))
        if number in key.destroyed_versions:
            return  # the fold keeps the first one per (key_id, version)
        key.destroyed_versions.add(number)
        key.destroyed_max = max(key.destroyed_max, number)
        found = key.versions.get(number)
        if found is not None:
            found.destroyed = True
            cause = p.get("cause")
            found.destroyed_cause = cause if isinstance(cause, str) else None

    # -- questions the engine asks ---------------------------------------------------------

    def key(self, key_id: str) -> StoredKey | None:
        return self.keys.get(key_id)

    def state(self, key_id: str, version: int, now: datetime) -> VersionState | None:
        found = self._version(key_id, version)
        return None if found is None else found.state(now)

    def next_version(self, key_id: str, listed_max: int) -> int:
        """The numbering rule: `max(A, B, C) + 1` (A: every `key_stored` ever, B: the highest the
        store lists, C: the highest destroyed). A number is never reused."""
        key = self.keys.get(key_id)
        a, c = (0, 0) if key is None else (key.stored_count, key.destroyed_max)
        return max(a, listed_max, c) + 1

    def enabled_fingerprints(self, hash_key_id: str, now: datetime) -> set[str]:
        """Fingerprints of the enabled (pending, current, grace) versions of any connection that
        were made under `hash_key_id`; rows of another key id are skipped, not an error."""
        found: set[str] = set()
        for key in self.keys.values():
            for item in key.versions.values():
                if (
                    item.fingerprint is not None
                    and item.hash_key_id == hash_key_id
                    and item.state(now) in ENABLED_STATES
                ):
                    found.add(item.fingerprint)
        return found

    def has_live_pending(self, key_id: str, now: datetime) -> bool:
        key = self.keys.get(key_id)
        return key is not None and any(v.state(now) == "pending" for v in key.versions.values())

    def append_refused(
        self, key_id: str, head_seq: int, now: datetime, version: int | None = None
    ) -> bool:
        """The decisive check of a `key_stored` append (spec 6 step 6), from the fold only: (i)
        another non-abandoned pending version exists, or (ii)/(iii) a stored, activated or
        revoked entry of the key was written after the ledger head the request read, or (iv) the
        request's `version` was already destroyed (a stalled put the sweeper removed)."""
        key = self.keys.get(key_id)
        if key is None:
            return False
        if version is not None and version in key.destroyed_versions:
            return True
        return self.has_live_pending(key_id, now) or key.last_seq > head_seq

    def resolve(self, key_id: str, now: datetime) -> tuple[int | None, str | None]:
        """For a NEW call: `(version, None)` of the current version, or `(None, code)` with
        `credential_revoked` / `credential_not_active` (never a store read)."""
        key = self.keys.get(key_id)
        if key is None or not key.versions:
            return None, "credential_not_active"
        if key.current_version is not None:
            state = key.versions[key.current_version].state(now)
            if state == "current":
                return key.current_version, None
            if state == "revoked":
                return None, "credential_revoked"
        if any(v.state(now) == "revoked" for v in key.versions.values()):
            return None, "credential_revoked"
        return None, "credential_not_active"

    def pinned_problem(
        self, key_id: str, version: int, started_at: datetime, now: datetime
    ) -> str | None:
        """For a call that pinned `version` at reservation (`started_at`) and reads again (the
        retry): None when it may read, else `credential_revoked` / `credential_not_active`."""
        found = self._version(key_id, version)
        if found is None:
            return "credential_not_active"
        state = found.state(now)
        if state == "revoked":
            return "credential_revoked"
        if state == "current":
            return None
        if (
            state == "grace"
            and found.superseded_at is not None
            and started_at < found.superseded_at
        ):
            return None
        return "credential_not_active"

    def read_failed_due(self, key_id: str, version: int, now: datetime) -> bool:
        """Whether a `key_read_failed` may be written: at most one per window of 60 s."""
        found = self._version(key_id, version)
        if found is None:
            return False
        last = found.last_read_failed_at
        return last is None or now - last >= READ_FAILED_WINDOW
