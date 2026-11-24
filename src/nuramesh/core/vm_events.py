"""Merge Gate ledger events (spec LA-1, LA-2; Fable fix F-8).

The fifteen `merge.*` event types are additive `EventType` values (`core.ledger`): nothing in
the hashed eleven-key envelope changes, new data lives only inside `payload` (ids, hashes,
states, reason codes and times; never diff text, verdict text, an email or free text, L-02).

`MERGE_EVENT_ACTORS` is the actor-by-event-type table (F-8): a `merge.verdict_issued` entry may
come only from the signer, a `merge.gate_decided` only from the gate, and the signed human acts
only from a `human:<opaque id>` with the signature embedded in the payload. The ledger append
path enforces it (GC-13): `core.ledger.next_event` calls `check_merge_actor` and refuses a
`merge.*` event from any other actor, whatever store writes it. The ledger append path is a trust
boundary for revocation order (SV-7, LA-5): a key thief needs a ledger entry to be counted, so
who may write which entry is defined here and tested. VM-2 (signer) and VM-4a (gate) also call
`check_merge_actor` before every append.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from nuramesh.core.ledger import EventType
from nuramesh.core.people import HUMAN_ID_BODY

GATE_ACTOR = "service:merge-gate"
SIGNER_ACTOR = "service:merge-gate-signer"
COLLECTOR_ACTOR = "service:merge-gate-collector"

HUMAN_ACTORS = frozenset({"human:*"})
"""Any `human:<opaque id>` (the pattern of `core.people`), never an email or a login."""

_HUMAN = re.compile(rf"^human:{HUMAN_ID_BODY}$")

MERGE_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.MERGE_EVIDENCE_RECORDED,
    EventType.MERGE_REVIEW_REQUESTED,
    EventType.MERGE_REVIEW_FAILED,
    EventType.MERGE_REVIEW_COST,
    EventType.MERGE_REVIEW_RESET,
    EventType.MERGE_VERDICT_ISSUED,
    EventType.MERGE_APPROVAL_RECORDED,
    EventType.MERGE_APPROVAL_WITHDRAWN,
    EventType.MERGE_APPROVER_ENROLLED,
    EventType.MERGE_KEY_CHANGED,
    EventType.MERGE_GATE_DECIDED,
    EventType.MERGE_QUEUE_REVERIFIED,
    EventType.MERGE_COMPLETED,
    EventType.MERGE_UNVERIFIED_LANDING,
    EventType.MERGE_OVERRIDE,
)
"""LA-1's list, in its order."""

MERGE_EVENT_ACTORS: Mapping[str, frozenset[str]] = {
    # the collector records bundles (EB-1)
    EventType.MERGE_EVIDENCE_RECORDED.value: frozenset({COLLECTOR_ACTOR}),
    # the signer owns the review path: request (with its requester, F-6), refusal, cost, verdict
    EventType.MERGE_REVIEW_REQUESTED.value: frozenset({SIGNER_ACTOR}),
    EventType.MERGE_REVIEW_FAILED.value: frozenset({SIGNER_ACTOR}),
    EventType.MERGE_REVIEW_COST.value: frozenset({SIGNER_ACTOR}),
    EventType.MERGE_VERDICT_ISSUED.value: frozenset({SIGNER_ACTOR}),
    # signed human acts (6.8 to 6.10, 6.3): the payload carries the signature
    EventType.MERGE_REVIEW_RESET.value: HUMAN_ACTORS,
    EventType.MERGE_APPROVAL_RECORDED.value: HUMAN_ACTORS,
    EventType.MERGE_APPROVAL_WITHDRAWN.value: HUMAN_ACTORS,
    EventType.MERGE_OVERRIDE.value: HUMAN_ACTORS,
    # enrollment by an org admin through an authenticated session (QA-5)
    EventType.MERGE_APPROVER_ENROLLED.value: HUMAN_ACTORS,
    # key changes: `merge init` by an admin (FR-8) or a rotation by the signer (SV-7)
    EventType.MERGE_KEY_CHANGED.value: frozenset({SIGNER_ACTOR, "human:*"}),
    # the gate owns decisions, queue re-verification and the post-merge job (F-10: a service job)
    EventType.MERGE_GATE_DECIDED.value: frozenset({GATE_ACTOR}),
    EventType.MERGE_QUEUE_REVERIFIED.value: frozenset({GATE_ACTOR}),
    EventType.MERGE_COMPLETED.value: frozenset({GATE_ACTOR}),
    EventType.MERGE_UNVERIFIED_LANDING.value: frozenset({GATE_ACTOR}),
}
"""Allowed actors per `merge.*` event type; `human:*` stands for any `human:<opaque id>`."""

SIGNED_HUMAN_EVENTS = frozenset(
    {
        EventType.MERGE_REVIEW_RESET.value,
        EventType.MERGE_APPROVAL_RECORDED.value,
        EventType.MERGE_APPROVAL_WITHDRAWN.value,
        EventType.MERGE_OVERRIDE.value,
    }
)
"""Events whose payload must embed an approver-key signature over the 6.3 or 6.8 to 6.10 payload
(the append path verifies it in VM-2/VM-3; the verifier reports `*_bad_signature`)."""


def actor_allowed(event_type: str, actor: str) -> bool:
    """Whether `actor` may append an event of this `merge.*` type (False for other types)."""
    allowed = MERGE_EVENT_ACTORS.get(event_type)
    if allowed is None:
        return False
    if actor in allowed:
        return True
    return "human:*" in allowed and _HUMAN.fullmatch(actor) is not None


def check_merge_actor(event_type: str, actor: str) -> str | None:
    """None when the append is allowed; else `unknown_event_type` (not a `merge.*` type this
    table knows) or `actor_forbidden`."""
    if event_type not in MERGE_EVENT_ACTORS:
        return "unknown_event_type"
    return None if actor_allowed(event_type, actor) else "actor_forbidden"
