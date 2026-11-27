"""The Merge Gate ledger seam (LA-2): `core.vm_signer.Ledger` over a
`Store`. It lives outside `engine/vm_keys.py` on purpose: it imports the store, and the store's
import closure must not enter the trust roots (TC-176 a)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nuramesh.core.vm_signer import LedgerRef
from nuramesh.stores.base import EventDraft, Store


@dataclass(frozen=True)
class StoreLedger:
    """`core.vm_signer.Ledger` over a `Store`: one org stream `org:<id>`, the actor the signer
    core passes (F-8 is checked before this is reached)."""

    store: Store
    org_id: str

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> LedgerRef:
        draft = EventDraft(
            stream=f"org:{self.org_id}", type=event_type, actor=actor, payload=payload
        )
        with self.store.transaction() as unit:
            event = unit.append(draft)
        return LedgerRef(seq=event.seq, at=event.at)
