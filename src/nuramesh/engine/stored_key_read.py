"""The read path of a stored model key (spec r114c 6 step 8 and 7; VK03, VK10, VK17, VK18,
VK21 to VK24, VK57).

A call PINS `(key_id, version)` from the fold once, at reservation (`pin`), and every read of
that call, including the single retry, asks the store for exactly that version (`read`). The
ledger decides: before any store call the fold must allow the pinned version (a `current` one, or
a `grace` one only for a call that started before the successor's activation); a revoke, an
expired grace, a tombstone (`key_destroyed`), an erased org, a version the ledger does not know
or a key that belongs to another connection is refused without touching the store. After the
read the value is checked against the keyed fingerprint of `model.key_stored`; a store that
answers with other material is refused (`key_mismatch`). Every refusal is a
`CredentialUnavailable` with a fixed code; nothing falls back to any other credential.

Reads are consistent without a lock: the fold is a prefix of an append-only stream and a version
in the store is immutable, so a reader during a rotation sees the old state or the new one, and a
pinned version stays readable until the fold retires it. No store call runs inside a database
transaction. The optional process cache (`NURAMESH_KEY_CACHE_SECONDS`, 0 = off, at most 60) is
consulted only AFTER the fold allowed the read, and dropped when the fold refuses.

The reveal of the value stays in `engine/model_keys.py` (fingerprint check); this file never
reveals it.
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from nuramesh.core.model_keys import KeyFold
from nuramesh.core.secret_store import SecretRef, SecretStoreError
from nuramesh.core.secrets_text import Redacted
from nuramesh.engine.model_keys import ModelKeys

RETRY_SECONDS = 2.0
MAX_CACHE_SECONDS = 60
CACHE_ENV = "NURAMESH_KEY_CACHE_SECONDS"


def cache_seconds_from(env: Mapping[str, str]) -> int:
    """The cache setting: absent, malformed or negative is 0 (off); above 60 is 60."""
    raw = env.get(CACHE_ENV, "0").strip()
    if not (raw.isascii() and raw.isdigit()):  # '²' is a digit to isdigit() but not to int()
        return 0
    return min(int(raw), MAX_CACHE_SECONDS)


@dataclass(frozen=True)
class Pin:
    """What a call resolved at reservation: the version it will read, and when it started."""

    key_id: str
    version: int
    started_at: datetime
    connection: str
    """The fold's key of the connection the pin was taken for; the binding is never skipped."""


class StoredKeyRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """A read refused with a fixed code (the credential seam turns it into
    `CredentialUnavailable`). No value, no path, no cause."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StoredKeyReader:
    """Reads the stored keys of ONE organization (the one `keys` belongs to)."""

    def __init__(
        self,
        keys: ModelKeys,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
        cache_seconds: int = 0,
    ) -> None:
        self._keys = keys
        self._clock = clock
        self._sleep = sleep
        self._cache_seconds = max(0, min(cache_seconds, MAX_CACHE_SECONDS))
        self._cache: dict[tuple[str, int], tuple[datetime, Redacted]] = {}
        self._cache_lock = threading.Lock()

    # -- the ledger's answer ---------------------------------------------------------------

    def fold(self) -> KeyFold:
        """The key fold of this reader's organization, as the ledger has it now (no store call)."""
        return self._keys.fold()

    @staticmethod
    def _fold_says(
        fold: KeyFold,
        key_id: str,
        version: int | None,
        started_at: datetime | None,
        now: datetime,
        connection: str,
    ) -> str | None:
        """None when the fold allows the read, else the fixed code. Pure, no store call."""
        key = fold.key(key_id)
        if fold.erasure_last is not None or key is None or not key.versions:
            return "credential_not_active"
        if not connection or key.connection != connection:
            return "credential_not_active"
        if version is None:
            return fold.resolve(key_id, now)[1]
        if version in key.destroyed_versions or version not in key.versions:
            return "credential_not_active"
        if key.versions[version].destroyed:
            return "credential_not_active"
        assert started_at is not None
        return fold.pinned_problem(key_id, version, started_at, now)

    def pin(self, key_id: str, *, connection: str) -> Pin:
        """Resolve the version a NEW call will use (at reservation). Raises `StoredKeyRefused`
        (`credential_revoked`, `credential_not_active`); never reads the store. The connection is
        required (an empty one is refused): the binding is never skipped."""
        if not connection:
            raise StoredKeyRefused("credential_not_active")
        now = self._clock()
        fold = self._keys.fold()
        problem = self._fold_says(fold, key_id, None, None, now, connection)
        if problem is None:
            current = fold.resolve(key_id, now)[0]
            if current is None:
                problem = "credential_not_active"
            else:
                problem = self._fold_says(fold, key_id, current, now, now, connection)
                if problem is None:
                    return Pin(key_id, current, now, connection)
        self._drop(key_id)
        raise StoredKeyRefused(problem)

    # -- the read --------------------------------------------------------------------------

    def read(self, pin: Pin) -> Redacted:
        """The value of the pinned version. The fold is checked first (and again before the one
        retry); the store is asked for exactly `pin.version`."""
        self._allow(pin)
        cached = self._cached(pin)
        if cached is not None:
            return cached
        outcome = self._get(pin)
        if outcome == "unavailable":
            self._sleep(RETRY_SECONDS)
            self._allow(pin)  # a revoke or the end of the grace refuses the retry
            outcome = self._get(pin)
        if isinstance(outcome, Redacted):
            if self._cache_seconds:
                with self._cache_lock:
                    self._cache[(pin.key_id, pin.version)] = (self._clock(), outcome)
            return outcome
        raise StoredKeyRefused("credential_unavailable")

    def _allow(self, pin: Pin) -> None:
        problem = self._fold_says(
            self._keys.fold(),
            pin.key_id,
            pin.version,
            pin.started_at,
            self._clock(),
            pin.connection,
        )
        if problem is not None:
            self._drop(pin.key_id)
            raise StoredKeyRefused(problem)

    def _get(self, pin: Pin) -> Redacted | str:
        """The value, or `unavailable` (retry) or `failed` (no retry); records what the owner
        needs to see. Raising happens in `read`, outside any `except` block."""
        ref = SecretRef(org_id=self._keys.org_id, key_id=pin.key_id, version=pin.version)
        code = ""
        value: Redacted | None = None
        try:
            value = self._keys.secrets.get(ref)
        except SecretStoreError as exc:
            code = exc.code
        except Exception:
            code = "failed"  # any other error of the adapter: no retry, no text, no cause (VK33)
        fold = self._keys.fold()  # fresh: after the store answered
        if value is None:
            if code == "unavailable":
                return "unavailable"
            if code in ("not_found", "disabled"):
                self._refuse_if_gone(pin, fold)  # a revoke or sweep raced the read: its code
                self._failed(pin, "key_missing")
            return "failed"  # denied, conflict, invalid: no retry, no event
        self._refuse_if_gone(pin, fold)  # a revoke committed before this point is honoured
        known = fold.key(pin.key_id)
        found = None if known is None else known.versions.get(pin.version)
        verdict = (
            "mismatch"
            if found is None
            else self._keys.check_value(value, found.fingerprint, found.hash_key_id)
        )
        if verdict == "unavailable":
            return "failed"  # the ledger key is missing: refuse, no key_mismatch is recorded
        if verdict == "mismatch":
            self._failed(pin, "key_mismatch")
            return "failed"
        return value

    def _refuse_if_gone(self, pin: Pin, fold: KeyFold) -> None:
        """Raise the fold's refusal on a fresh fold (the same check as `_allow`)."""
        problem = self._fold_says(
            fold, pin.key_id, pin.version, pin.started_at, self._clock(), pin.connection
        )
        if problem is not None:
            self._drop(pin.key_id)
            raise StoredKeyRefused(problem)

    def _failed(self, pin: Pin, code: str) -> None:
        try:
            self._keys.record_read_failed(pin.key_id, pin.version, code)
        except Exception:  # noqa: S110 (the ledger is down: the call is refused anyway)
            pass

    # -- the optional cache ----------------------------------------------------------------

    def _cached(self, pin: Pin) -> Redacted | None:
        if not self._cache_seconds:
            return None
        with self._cache_lock:
            found = self._cache.get((pin.key_id, pin.version))
            if found is None:
                return None
            stored_at, value = found
            if self._clock() - stored_at >= timedelta(seconds=self._cache_seconds):
                self._cache.pop((pin.key_id, pin.version), None)
                return None
            return value

    def _drop(self, key_id: str) -> None:
        with self._cache_lock:
            for entry in [k for k in list(self._cache) if k[0] == key_id]:
                self._cache.pop(entry, None)
