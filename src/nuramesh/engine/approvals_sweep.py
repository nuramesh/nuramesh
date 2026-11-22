"""The approvals sweep (ND195 a; spec/auth/token-lifecycle.md Part C): decided and consumed
approvals are deleted 13 months (396 days, `APPROVAL_RETENTION`) after their last status
change. Pending approvals are never deleted and the ledger is never edited.

Same shape as the content expiry (`ContentService.expire`): batches, one transaction per batch, a
conditional delete inside it, and one ledger entry per organization and batch with **counts only**
(`approvals.swept`, actor `system`). An approval carries no organization of its own; it is the
`org` of the first event of its run stream. One whose organization is unknown is kept and counted.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC

from nuramesh.core.ledger import EventType, format_time
from nuramesh.core.policy import APPROVAL_RETENTION, ApprovalStatus
from nuramesh.stores.base import Clock, EventDraft, Store, StoredApproval, run_stream

DEFAULT_BATCH = 500


@dataclass(frozen=True)
class SweepResult:
    """What one sweep did: approvals deleted, and due ones kept because their organization is
    unknown."""

    deleted: int
    unscoped: int


def run_org(store: Store, run_id: str) -> str | None:
    """The organization of a run: the `org` of its first event, when that is `run.started`."""
    first = list(store.read_stream(run_stream(run_id), after_seq=0, limit=1))
    if first and first[0].type == EventType.RUN_STARTED.value:
        org = first[0].payload.get("org")
        return org if isinstance(org, str) and org else None
    return None


def sweep_approvals(
    store: Store, *, clock: Clock | None = None, batch: int = DEFAULT_BATCH
) -> SweepResult:
    """Delete every approval whose retention is over (see the module docstring). Idempotent: a
    second run right after deletes nothing and writes nothing."""
    if batch < 1:
        raise ValueError("batch must be at least 1")
    now = (clock or store.now)().astimezone(UTC).replace(microsecond=0)
    orgs: dict[str, str | None] = {}
    deleted = unscoped = 0
    cursor = ""
    while True:
        due = store.due_approvals(now, after_id=cursor, limit=batch)
        if not due:
            return SweepResult(deleted, unscoped)
        cursor = due[-1].approval.id
        by_org: dict[str, list[StoredApproval]] = {}
        for stored in due:
            run = stored.approval.run_id
            if run not in orgs:
                orgs[run] = run_org(store, run)
            org = orgs[run]
            if org is None:
                unscoped += 1
            else:
                by_org.setdefault(org, []).append(stored)
        with store.transaction() as uow:
            for org in sorted(by_org):
                gone = [
                    s
                    for s in by_org[org]
                    if uow.delete_approval_if_due(s.approval.id, now, expected_version=s.version)
                ]
                if not gone:
                    continue
                counts = Counter(s.approval.status.value for s in gone)
                uow.append(
                    EventDraft(
                        stream=f"org:{org}",
                        type=EventType.APPROVALS_SWEPT,
                        actor="system",
                        payload={
                            "count": len(gone),
                            "statuses": {
                                s.value: counts[s.value]
                                for s in (
                                    ApprovalStatus.APPROVED,
                                    ApprovalStatus.DENIED,
                                    ApprovalStatus.CONSUMED,
                                )
                            },
                            "cutoff": format_time(now - APPROVAL_RETENTION),
                        },
                    )
                )
                deleted += len(gone)
        if len(due) < batch:
            return SweepResult(deleted, unscoped)
