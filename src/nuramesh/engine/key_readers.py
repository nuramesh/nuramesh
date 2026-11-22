"""One `StoredKeyReader` per organization (R1.14c wiring; spec 6 step 8, section 7).

`CredentialSource` takes ONE org-bound reader, and a `ConnectionRecord` carries no org id, so
whoever serves several organizations must hold one reader per org: a shared reader would check
another org's fold. `KeyReaders` builds them on demand (one per org id, kept) from the secret
store, the content service's ledger hashing, ONE clock and the cache setting
(`NURAMESH_KEY_CACHE_SECONDS`). The same clock goes to the reader AND to the ledger service
(`ModelKeys`), so a grace window is measured by one clock on both sides.

Nothing here reads a value: the reader checks the ledger first, refuses with a fixed code, and
never falls back to any other credential.
"""

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime

from nuramesh.core.secret_store import SecretStore, SecretStoreError
from nuramesh.engine.azure_openai import TokenProvider
from nuramesh.engine.content import LedgerHashing
from nuramesh.engine.credentials import CredentialSource
from nuramesh.engine.key_file import IsAgentUser
from nuramesh.engine.model_keys import HasherFor, ModelKeys
from nuramesh.engine.stored_key_read import StoredKeyReader, cache_seconds_from
from nuramesh.stores import Store

HasherForOrg = Callable[[str], HasherFor]
"""`org_id -> hasher_for`, for example `lambda org: lambda uow: hashing.bind(uow, org)`."""


class KeyReaders:
    """The readers of the stored keys, one per organization, over one secret store."""

    def __init__(
        self,
        store: Store,
        secrets_store: SecretStore,
        hasher_for_org: HasherForOrg,
        *,
        clock: Callable[[], datetime] | None = None,
        cache_seconds: int = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store, self._secrets, self._hasher_for_org = store, secrets_store, hasher_for_org
        self._clock = clock if clock is not None else store.now
        self._cache_seconds, self._sleep = cache_seconds, sleep
        self._readers: dict[str, StoredKeyReader] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        store: Store,
        secrets_store: SecretStore,
        hasher_for_org: HasherForOrg,
        env: Mapping[str, str],
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "KeyReaders":
        """As the constructor, with the cache setting read from `NURAMESH_KEY_CACHE_SECONDS`
        (absent, malformed or negative is 0 = off; above 60 is 60)."""
        return cls(
            store,
            secrets_store,
            hasher_for_org,
            clock=clock,
            cache_seconds=cache_seconds_from(env),
            sleep=sleep,
        )

    def reader_for(self, org_id: str) -> StoredKeyReader:
        """The one reader of `org_id` (built at the first ask)."""
        with self._lock:
            found = self._readers.get(org_id)
            if found is None:
                keys = ModelKeys(
                    self._store,
                    org_id,
                    self._secrets,
                    self._hasher_for_org(org_id),
                    clock=self._clock,
                )
                found = StoredKeyReader(
                    keys, clock=self._clock, sleep=self._sleep, cache_seconds=self._cache_seconds
                )
                self._readers[org_id] = found
            return found

    def key_current(self, org_id: str, key_ref: str, connection: str) -> bool:
        """Whether the key fold of `org_id` says the stored key of `connection` is `current` now
        (`spec/real-models` R10 check 4). The ledger fold only, never a secret-store call (R11)."""
        from nuramesh.engine.admission import key_fold_current

        fold = self.reader_for(org_id).fold()
        return key_fold_current(fold, key_ref, connection, self._clock())

    def source_for(
        self, org_id: str, *, entra: TokenProvider | None = None, agent: IsAgentUser | None = None
    ) -> CredentialSource:
        """A `CredentialSource` whose `stored` kind reads through the reader of `org_id`."""
        return CredentialSource(entra=entra, agent=agent, stored=self.reader_for(org_id))


STORED_OFF_REASONS = ("no_secret_dir", "no_ledger_hashing", "secret_dir_refused")


def key_readers_with_reason(
    store: Store,
    hashing: LedgerHashing | None,
    env: Mapping[str, str] | None = None,
    *,
    agent: IsAgentUser | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[KeyReaders | None, str | None]:
    """The readers for a process that names a secret store (`NURAMESH_SECRET_STORE`, or a
    directory in `NURAMESH_SECRET_DIR`; `nuramesh.engine.secret_select`), or None
    and a fixed reason word (one of `STORED_OFF_REASONS`: no paths, no secrets) when none is
    named (`no_secret_dir`), the ledger hashing is not available (`no_ledger_hashing`) or the
    directory is refused (`secret_dir_refused`). A `stored` connection is then refused with
    `auth_kind` and that reason: fail closed, nothing else is tried. `sleep` is the wait before
    the one retry of a read (the light runner passes `call_signal.retry_wait`, R38.10)."""
    import os

    from nuramesh.engine.secret_file import file_store_from_env
    from nuramesh.engine.secret_select import select_secret_store

    if hashing is None:
        return None, "no_ledger_hashing"
    try:
        secrets_store = (
            select_secret_store(os.environ) if agent is None else file_store_from_env(agent=agent)
        )
    except SecretStoreError as exc:
        named = os.environ.get("NURAMESH_SECRET_DIR") or os.environ.get("NURAMESH_SECRET_STORE")
        if exc.detail == "hosted_needs_real_store" and not named:
            return None, "no_secret_dir"  # hosted with no store named: stored keys are off
        return None, "secret_dir_refused"
    if secrets_store is None:
        return None, "no_secret_dir"
    readers = KeyReaders.from_env(
        store,
        secrets_store,
        lambda org: lambda uow: hashing.bind(uow, org),
        os.environ if env is None else env,
        sleep=sleep,
    )
    return readers, None


def key_readers_from_env(
    store: Store,
    hashing: LedgerHashing | None,
    env: Mapping[str, str] | None = None,
    *,
    agent: IsAgentUser | None = None,
) -> KeyReaders | None:
    """`key_readers_with_reason` without the reason."""
    return key_readers_with_reason(store, hashing, env, agent=agent)[0]
