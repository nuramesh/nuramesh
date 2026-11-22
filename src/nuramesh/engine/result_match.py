"""Find a gate result among recorded `gate.completed` rows, legacy or keyed (PH-KEY R10).

A row with `hash_scheme` carries the keyed `gate_result` digest; a row without it (written before
PH-KEY) carries a plain SHA-256 of the same canonical bytes, which is compared here and never
written. The keyed digest is computed lazily, only when a candidate row is keyed, so a store with
only legacy rows never touches the key ring. A keyed row that can't be attributed to the loaded
key (no `hash_org`, another organization, another key id) fails closed with the key error: it is
never a non-match, or the same result would be recorded twice.
"""

from collections.abc import Iterable

from nuramesh.core.content import (
    KeyedRowMismatch,
    LedgerHashKind,
    MarkedLedgerHasher,
    legacy_aware_result_match,
)
from nuramesh.core.gates import GateResult, result_bytes
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.engine.content import OrphanedContentError

_MARKER_FIELDS = ("hash_scheme", "hash_key_id", "hash_org")


def recorded_hash_of(
    events: Iterable[LedgerEvent], result: GateResult, hasher: MarkedLedgerHasher, org_id: str
) -> str | None:
    """The recorded `result_hash` of the first `gate.completed` row that matches `result`, or
    None. Raises the key error if a keyed row exists and the key gate refuses."""
    data = result_bytes(result)
    keyed: list[str] = []

    def keyed_digest() -> str:
        if not keyed:
            keyed.append(hasher(LedgerHashKind.GATE_RESULT, data))
        return keyed[0]

    for event in events:
        if event.type != EventType.GATE_COMPLETED:
            continue
        marker = {k: event.payload[k] for k in _MARKER_FIELDS if k in event.payload}
        if "hash_scheme" in marker and marker.get("hash_org") != org_id:
            raise OrphanedContentError(org_id, mismatch=True)  # forged or foreign: can't attribute
        recorded = event.payload.get("result_hash")
        try:
            hit = legacy_aware_result_match(
                recorded,
                marker,
                data,
                keyed_digest,
                key_id_of_loaded=lambda: hasher.marker()["hash_key_id"],
            )
        except KeyedRowMismatch:
            raise OrphanedContentError(org_id, mismatch=True) from None
        if hit and isinstance(recorded, str):
            return recorded
    return None
