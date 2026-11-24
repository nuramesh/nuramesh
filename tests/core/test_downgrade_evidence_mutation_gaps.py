"""Behaviour pins for `nuramesh.core.downgrade_evidence` found by mutation testing (window
boundaries, equal cost, zero text, defaults, frozen dataclasses). Fake data only."""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nuramesh.core.downgrade_evidence import (
    CallFact,
    Evidence,
    Recommendation,
    Row,
    TaskFact,
    build_evidence,
    recommend,
)

T0 = datetime(2026, 10, 15, tzinfo=UTC)


def _task(at: datetime | None, key: str = "t") -> TaskFact:
    return TaskFact(
        key=key,
        role="writer",
        calls=(CallFact("m-one", Decimal("1")),),
        first_call_at=at,
        state="done",
        closes=0,
        final_move_closed=False,
        ever_done=True,
    )


def _tasks_in(
    at: datetime | None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
) -> int:
    ev = build_evidence([_task(at)], start=start, end=end, as_of=as_of)
    return ev.rows.get("writer", {}).get("m-one", Row(0, 0, 0, 0, Decimal(0), 0)).tasks


def test_window_start_is_inclusive() -> None:
    assert _tasks_in(T0, start=T0) == 1
    assert _tasks_in(T0 - timedelta(microseconds=1), start=T0) == 0


def test_window_end_is_exclusive() -> None:
    assert _tasks_in(T0, end=T0) == 0
    assert _tasks_in(T0 - timedelta(microseconds=1), end=T0) == 1


def test_as_of_is_inclusive_and_later_first_calls_are_left_out() -> None:
    assert _tasks_in(T0, as_of=T0) == 1
    assert _tasks_in(T0 + timedelta(microseconds=1), as_of=T0) == 0
    assert _tasks_in(T0 + timedelta(days=1), as_of=T0) == 0


def test_a_task_without_a_first_call_time_is_never_cut_by_a_bound() -> None:
    assert _tasks_in(None, start=T0, end=T0 + timedelta(days=1), as_of=T0) == 1


def test_no_bounds_means_no_limit() -> None:
    assert _tasks_in(T0, start=None, end=None, as_of=None) == 1


def test_evidence_excluded_default_is_zero_per_reason_and_not_shared() -> None:
    first = Evidence(rows={})
    assert first.excluded == {"mixed_model": 0, "not_evidence": 0}
    first.excluded["mixed_model"] += 1
    assert Evidence(rows={}).excluded == {"mixed_model": 0, "not_evidence": 0}


def test_call_fact_is_not_a_gate_call_by_default() -> None:
    assert CallFact("m", None).gate is False
    assert CallFact("m", Decimal("1")).gate is False


def test_zero_known_total_is_written_as_plain_zero() -> None:
    for zero in (Decimal(0), Decimal("0.00"), Decimal("0E+3")):
        assert Row(1, 1, 1, 1, zero, 0).to_json()["known_total"] == "0"
    assert Row(1, 1, 1, 1, Decimal("1E+1"), 0).to_json()["known_total"] == "10"
    assert Row(1, 1, 1, 1, Decimal("0.50"), 0).to_json()["known_total"] == "0.5"


def test_equal_cost_per_accepted_is_not_a_recommendation() -> None:
    same = Row(5, 5, 5, 5, Decimal("10"), 0)
    rec = recommend(
        "writer",
        {"a": same, "b": same},
        min_sample=5,
        families={},
        verifier_models=(),
        eligible=None,
    )
    assert rec.status == "none"
    assert rec.candidate is None
    assert rec.current == "a"
    assert rec.refused == ()


def test_slightly_cheaper_candidate_is_recommended() -> None:
    rec = recommend(
        "writer",
        {"a": Row(5, 5, 5, 5, Decimal("10"), 0), "b": Row(5, 5, 5, 5, Decimal("9"), 0)},
        min_sample=5,
        families={},
        verifier_models=(),
        eligible=None,
    )
    assert (rec.status, rec.candidate, rec.saving) == ("recommend", "b", "0.100000")


def test_frozen_instances_refuse_assignment() -> None:
    call = CallFact("m", Decimal("1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.model = "other"  # type: ignore[misc]
    task = _task(T0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = "cancelled"  # type: ignore[misc]
    row = Row(1, 1, 1, 1, Decimal(1), 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.tasks = 2  # type: ignore[misc]
    rec = Recommendation("none", "a", None, ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.status = "recommend"  # type: ignore[misc]
    ev = Evidence(rows={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.rows = {}  # type: ignore[misc]
