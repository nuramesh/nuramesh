"""A content service per store for tests: reasons are captured into it (T47)."""

import weakref
from typing import TYPE_CHECKING

from nuramesh.engine.content import ContentService, LedgerHashing, MemoryKeyRing
from nuramesh.stores import Store

if TYPE_CHECKING:
    from nuramesh.core.content import LedgerHasher, MarkedLedgerHasher
    from nuramesh.core.prices import PriceOverride

_SERVICES: "weakref.WeakKeyDictionary[Store, ContentService]" = weakref.WeakKeyDictionary()


def content_for(store: Store) -> ContentService:
    """The same service (and so the same keys) every time for one store."""
    service = _SERVICES.get(store)
    if service is None:
        service = ContentService(store, MemoryKeyRing())
        _SERVICES[store] = service
    return service


def keyed(override: "PriceOverride", org_id: str = "acme") -> "PriceOverride":
    """The override with its reason keyed under a fixed test master key (T47)."""
    from nuramesh.core.content import ContentKind, keyed_hash

    return override.with_reason_hash(
        keyed_hash(b"\x07" * 32, org_id, ContentKind.REASON, override.reason)
    )


PH_ORG = "acme"
"""The organization the PH-KEY helpers bind to when a test calls a function that has no org."""


def hashing_for(store: Store) -> "LedgerHashing":
    """The same ledger hashing (and so the same keys) every time for one store (PH-KEY)."""
    return content_for(store).ledger_hashing


def hasher_for(store: Store, org: object = PH_ORG) -> "MarkedLedgerHasher":
    """A hasher bound to `org` (an organization graph or its id) for one call."""
    org_id = org if isinstance(org, str) else str(getattr(org, "id"))  # noqa: B009
    return hashing_for(store).bind(None, org_id)


def pure_hasher(master: bytes = b"\x01" * 32, org_id: str = PH_ORG) -> "LedgerHasher":
    """A plain keyed hasher for the pure core functions (fixed, obviously fake key)."""
    from nuramesh.core.content import LedgerHashKind, ledger_hash

    def hasher(kind: LedgerHashKind, data: bytes) -> str:
        return ledger_hash(master, org_id, kind, data)

    return hasher


_DEFAULT_SERVICES: "weakref.WeakKeyDictionary[Store, ContentService]" = weakref.WeakKeyDictionary()


def default_content_for(store: Store) -> ContentService:
    """A content service over the DEFAULT file key ring (`NURAMESH_KEY_DIR`), the one a CLI run in
    the same test uses, so keyed ledger rows written by a real run and content captured later
    share one key (PH-KEY R5b)."""
    from nuramesh.engine.content import default_content_service

    service = _DEFAULT_SERVICES.get(store)
    if service is None:
        service = default_content_service(store)
        _DEFAULT_SERVICES[store] = service
    return service
