"""Approval retention (ND195 a, spec/auth/token-lifecycle.md Part C R25 to R29): 396 days from
the last change."""

from datetime import UTC, datetime, timedelta

from nuramesh.core.policy import (
    APPROVAL_RETENTION,
    Action,
    ActionKind,
    Approval,
    ApprovalStatus,
)

A = datetime(2025, 9, 30, 12, 0, 0, tzinfo=UTC)


def make(
    status: ApprovalStatus, *, decided: datetime | None = A, consumed: datetime | None = None
) -> Approval:
    extra: dict[str, object] = {}
    if status is not ApprovalStatus.PENDING:
        extra.update(decided_by="human:ann", decided_at=decided, decision_reason="ok")
    if consumed is not None:
        extra["consumed_at"] = consumed
    return Approval(
        id="a1",
        run_id="r1",
        agent_id="dev",
        action=Action(kind=ActionKind.MERGE, target="main"),
        status=status,
        requested_at=A - timedelta(days=1),
        **extra,  # type: ignore[arg-type]
    )


def test_retention_is_396_days() -> None:
    assert APPROVAL_RETENTION == timedelta(days=396)


def test_the_boundary_is_to_the_second_in_utc() -> None:
    approval = make(ApprovalStatus.APPROVED)
    end = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)  # 2025-09-30 + 396 days
    assert approval.retention_ends() == end
    assert not approval.retention_over(end - timedelta(seconds=1))
    assert approval.retention_over(end)
    assert approval.retention_over(end + timedelta(seconds=1))


def test_microseconds_of_the_anchor_are_dropped() -> None:
    approval = make(ApprovalStatus.DENIED, decided=A.replace(microsecond=900_000))
    assert approval.retention_ends() == datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def test_a_consumed_approval_counts_from_its_consumption() -> None:
    consumed = datetime(2026, 6, 1, tzinfo=UTC)
    approval = make(
        ApprovalStatus.CONSUMED, decided=datetime(2025, 1, 1, tzinfo=UTC), consumed=consumed
    )
    assert approval.retention_ends() == datetime(2027, 7, 2, 0, 0, 0, tzinfo=UTC)


def test_a_time_with_an_offset_is_compared_in_utc() -> None:
    from datetime import timezone

    plus2 = timezone(timedelta(hours=2))
    approval = make(ApprovalStatus.APPROVED, decided=datetime(2025, 9, 30, 14, 0, 0, tzinfo=plus2))
    assert approval.retention_ends() == datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def test_a_pending_approval_never_ends() -> None:
    approval = make(ApprovalStatus.PENDING)
    assert approval.retention_ends() is None
    assert not approval.retention_over(A + timedelta(days=5000))


def test_ap20_an_expired_approval_never_ends() -> None:
    approval = Approval(
        id="a1",
        run_id="r1",
        agent_id="dev",
        action=Action(kind=ActionKind.MERGE, target="main"),
        status=ApprovalStatus.EXPIRED,
        requested_at=A - timedelta(days=1),
        decided_by="human:ann",
        decided_at=A,
        decision_reason="ok",
        deadline=A + timedelta(days=1),
        expired_at=A + timedelta(days=2),
    )
    assert approval.retention_ends() is None
