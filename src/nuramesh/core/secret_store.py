"""The secret-store port for stored model keys (spec r114c 4.1, ND362).

Pure (stdlib and pydantic). A store keeps one immutable VERSION of a key per `SecretRef`; the value
travels only as `Redacted`. The port has no listing across organizations: `list_versions` takes
one organization id. `SecretStoreError` has a fixed code and a fixed text, and is raised outside
any `except` block so it never carries a cause chain (the value could ride on one).

`FakeSecretStore` is the in-memory test double with fault injection; the managed file store is
`nuramesh.engine.secret_file`, the Azure adapter is enterprise.
"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nuramesh.core.secrets_text import Redacted

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""The fake's clock unless a test injects one: fixed, never the wall clock (ND243)."""
KEY_ID_PATTERN = r"^mk_[0-9a-f]{20}$"
ERROR_CODES = ("unavailable", "not_found", "disabled", "denied", "conflict", "invalid")
_TEXTS = {
    "unavailable": "the secret store is not available",
    "not_found": "the secret was not found",
    "disabled": "the secret is disabled",
    "denied": "the secret store refused access",
    "conflict": "the secret version already exists",
    "invalid": "the secret request is not valid",
}


def org_tag(org_id: str) -> str:
    """The first 16 lowercase hex characters of SHA-256 of the organization id: the only form of
    the organization in a store name (it never holds the id itself)."""
    return hashlib.sha256(org_id.encode()).hexdigest()[:16]


class SecretRef(BaseModel):
    """One version of one key of one organization. Built only by the server from the
    authenticated organization and a server-generated key id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1, max_length=200)
    key_id: str = Field(pattern=KEY_ID_PATTERN)
    version: int = Field(ge=1, le=1_000_000)

    @property
    def name(self) -> str:
        """The store name `nm-<org_tag>-<key_hex>-v<n>`: only `[0-9a-z-]`, so it is a valid Key
        Vault secret name and a file name (the underscore of `mk_` is never in it)."""
        return f"nm-{org_tag(self.org_id)}-{self.key_id[3:]}-v{self.version}"


class SecretMeta(BaseModel):
    """What a store says about a version, never the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: SecretRef
    created_at: datetime
    """The STORE's clock (the file store: the mtime at the exclusive create)."""
    state: Literal["enabled", "disabled"]
    expires_at: datetime | None = None


class SecretStoreError(Exception):
    """A store refusal with a fixed code and text. Raise it OUTSIDE an `except` block."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        if code not in ERROR_CODES:
            code = "invalid"
        self.code = code
        self.detail = detail
        """A fixed reason from the adapter's own closed list (never a path or a value)."""
        super().__init__(_TEXTS[code])


class SecretStore(Protocol):
    """The port. Every method is org-scoped through `SecretRef`; a version is immutable."""

    def put(
        self, ref: SecretRef, value: Redacted, *, expires_at: datetime | None = None
    ) -> SecretMeta:
        """Create-only: an existing version (even a disabled one) is `conflict`."""
        ...

    def get(self, ref: SecretRef) -> Redacted:
        """A disabled version is `disabled`, an absent one `not_found`."""
        ...

    def disable(self, ref: SecretRef) -> SecretMeta:
        """Idempotent; the value is unusable at once and is removed by `destroy`."""
        ...

    def destroy(self, ref: SecretRef) -> None:
        """Delete (and purge where the backend allows it); idempotent."""
        ...

    def versions(self, org_id: str, key_id: str) -> list[SecretMeta]:
        """Metadata of the versions of one key, by version; never a value."""
        ...

    def list_versions(self, org_id: str) -> list[SecretMeta]:
        """Every version of ONE organization, metadata only (how the sweeper finds an orphan)."""
        ...

    def health(self) -> bool:
        """Cheap; reads no value."""
        ...


class FakeSecretStore:
    """In-memory `SecretStore` for tests; never holds a real key.

    Fault injection: `fail[method] = code` raises that error from the method; `reserved` is a set
    of refs whose `put` answers `conflict` although nothing is listed (an Azure soft-deleted name,
    or a writer outside the fold); `soft_delete` makes a destroyed name disappear from
    `versions()` but keep refusing a `put` of it; `before_put` is a hook (a barrier) called at
    the start of `put`; `clock` is the store's clock. `calls` counts every method call. Concurrent
    puts are allowed (VK76)."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        soft_delete: bool = False,
    ) -> None:
        self._clock = clock or (lambda: _EPOCH)
        self._lock = threading.Lock()
        self._items: dict[SecretRef, tuple[Redacted, SecretMeta]] = {}
        self._gone: set[SecretRef] = set()
        self.soft_delete = soft_delete
        self.fail: dict[str, str] = {}
        self.reserved: set[SecretRef] = set()
        self.before_put: Callable[[SecretRef], None] | None = None
        self.calls: Counter[str] = Counter()
        self.in_transaction: Callable[[], bool] | None = None
        """A probe the engine tests set: True while a database transaction is open."""
        self.seen_in_transaction = 0

    def _enter(self, method: str) -> None:
        self.calls[method] += 1
        if self.in_transaction is not None and self.in_transaction():
            self.seen_in_transaction += 1
        code = self.fail.get(method) or self.fail.get("*")
        if code is not None:
            raise SecretStoreError(code)

    def put(
        self, ref: SecretRef, value: Redacted, *, expires_at: datetime | None = None
    ) -> SecretMeta:
        self._enter("put")
        if self.before_put is not None:
            self.before_put(ref)
        with self._lock:
            if ref in self._items or ref in self._gone or ref in self.reserved:
                conflict = True
            else:
                conflict = False
                meta = SecretMeta(
                    ref=ref, created_at=self._clock(), state="enabled", expires_at=expires_at
                )
                self._items[ref] = (value, meta)
        if conflict:
            raise SecretStoreError("conflict")
        return meta

    def get(self, ref: SecretRef) -> Redacted:
        self._enter("get")
        with self._lock:
            found = self._items.get(ref)
        if found is None:
            raise SecretStoreError("not_found")
        if found[1].state == "disabled":
            raise SecretStoreError("disabled")
        return found[0]

    def disable(self, ref: SecretRef) -> SecretMeta:
        self._enter("disable")
        with self._lock:
            found = self._items.get(ref)
            if found is None:
                raise SecretStoreError("not_found")
            meta = found[1].model_copy(update={"state": "disabled"})
            self._items[ref] = (found[0], meta)
        return meta

    def destroy(self, ref: SecretRef) -> None:
        self._enter("destroy")
        with self._lock:
            if self._items.pop(ref, None) is not None and self.soft_delete:
                self._gone.add(ref)

    def versions(self, org_id: str, key_id: str) -> list[SecretMeta]:
        self._enter("versions")
        with self._lock:
            found = [
                meta
                for ref, (_v, meta) in self._items.items()
                if ref.org_id == org_id and ref.key_id == key_id
            ]
        return sorted(found, key=lambda m: m.ref.version)

    def list_versions(self, org_id: str) -> list[SecretMeta]:
        self._enter("list_versions")
        with self._lock:
            found = [meta for ref, (_v, meta) in self._items.items() if ref.org_id == org_id]
        return sorted(found, key=lambda m: (m.ref.key_id, m.ref.version))

    def health(self) -> bool:
        self._enter("health")
        return True

    def stored_values(self) -> Mapping[SecretRef, Redacted]:
        """Test-only view of what the fake holds."""
        with self._lock:
            return {ref: v for ref, (v, _m) in self._items.items()}
