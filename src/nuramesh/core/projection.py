"""Which ledger entries the autonomy projection indexes (E13, E14, decision T136).

The projection is a derived, rebuildable index over the append-only ledger: one row per entry
whose **kind** the earned-autonomy reads look at. It is maintained by the stores in the same
transaction as the entry and can always be rebuilt from the ledger alone. The kind of a
`decision.recorded` entry is its payload `kind`; any other entry is indexed by its event type.
A run's organization is read from its `run.started` entry (`org`), so a read for one
organization needs no scan of other organizations' runs.
"""

from typing import NamedTuple

from nuramesh.core.autonomy import AUTONOMY_KINDS, KIND_PROMOTION
from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_epoch import KIND_FLOOR

DECISION_KINDS = frozenset(AUTONOMY_KINDS | {KIND_PROMOTION, KIND_FLOOR})
"""The `decision.recorded` kinds the overlay reads."""

RUN_STARTED = "run.started"
EVIDENCE_TYPES = frozenset(
    {
        RUN_STARTED,
        "approval.requested",
        "approval.decided",
        "gate.completed",
        "task.transitioned",
        "eval.scored",
        "budget.breached",
        "policy.decided",
        "model.blocked",
        "model.called",
    }
)
"""Event types that grants, demotions and scorecards read."""

ALL_KINDS = frozenset(DECISION_KINDS | EVIDENCE_TYPES)


class ProjectionKey(NamedTuple):
    """The index entry of one ledger entry: its kind and, for `run.started`, its organization."""

    kind: str
    org: str | None


def projection_key(event: LedgerEvent) -> ProjectionKey | None:
    """The index entry of `event`, or None if the projection does not hold it."""
    if event.type == "decision.recorded":
        kind = event.payload.get("kind")
        if isinstance(kind, str) and kind in DECISION_KINDS:
            return ProjectionKey(kind, None)
        return None
    if event.type == RUN_STARTED:
        org = event.payload.get("org")
        return ProjectionKey(RUN_STARTED, org if isinstance(org, str) else "")
    if event.type in EVIDENCE_TYPES:
        return ProjectionKey(event.type, None)
    return None
