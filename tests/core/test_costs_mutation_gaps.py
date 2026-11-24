"""Mutation pins for `nuramesh.core.costs` (grouping, closed attempts, unknown cost, quotes).

Every expected amount is written out by hand in a comment; none is computed by the code under
test. An unknown cost stays unknown: it is counted as an unknown call and never added as zero.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact

import pytest

from nuramesh.core.costs import (
    Cell,
    Placed,
    Row,
    RunFacts,
    SpendCall,
    TaskMove,
    add_exact,
    effective_role,
    group_key,
    place_calls,
    quote,
    role_source_of,
    summarize,
    task_states,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=1)
END = T0 + timedelta(days=30)


def call(
    seq: int = 1,
    *,
    cost: str | None = "1",
    task: str | None = "t1",
    role: str | None = "worker",
    agent: str = "ag",
    provider: str = "prov",
    model: str = "mod",
    endpoint_id: str | None = None,
    provisional: bool = False,
    at: datetime = T0,
    price_version_at: datetime | None = None,
) -> SpendCall:
    return SpendCall(
        stream="s",
        seq=seq,
        run="r1",
        task=task,
        at=at,
        agent=agent,
        role=role,
        provider=provider,
        model=model,
        cost=None if cost is None else Decimal(cost),
        cost_basis=None,
        provisional=provisional,
        price_version_at=price_version_at,
        endpoint_id=endpoint_id,
    )


def move(
    seq: int, to: str, *, closed: bool = False, actor: str = "system:engine", at: datetime = T0
) -> TaskMove:
    return TaskMove(
        run="r1",
        task="t1",
        seq=seq,
        at=at,
        to=to,
        closed_attempt=closed,
        actor=actor,
        assignee=None,
    )


def placed_for(calls: Sequence[SpendCall], moves: Sequence[TaskMove]) -> list[Placed]:
    return place_calls(calls, task_states(moves, END), END)


def only(calls: Sequence[SpendCall], moves: Sequence[TaskMove]) -> Placed:
    result = placed_for(calls, moves)
    assert len(result) == 1
    return result[0]


# -- exact sums ----------------------------------------------------------------------------------


def test_add_exact_raises_rather_than_round_past_a_thousand_digits() -> None:
    # 10**999 + 1 is a 1000-digit integer: exact. 10**1000 + 1 needs 1001 digits: refused.
    assert add_exact(Decimal(10) ** 999, Decimal(1)) == Decimal("1" + "0" * 998 + "1")
    with pytest.raises(Inexact):
        add_exact(Decimal(10) ** 1000, Decimal(1))


# -- grouping -------------------------------------------------------------------------------------


def test_effective_role_ledger_wins_and_config_fills_the_gap() -> None:
    roles = {"ag": "from-config"}
    assert effective_role(call(role="from-ledger"), roles) == ("from-ledger", "ledger")
    assert effective_role(call(role=None), roles) == ("from-config", "config")
    assert effective_role(call(role=None, agent="stranger"), roles) == ("unknown", "config")


def test_group_key_picks_the_dimension() -> None:
    c = call(role="worker", provider="prov", model="mod")
    roles = {"ag": "other"}
    assert group_key(c, roles, "role") == "worker"
    assert group_key(c, roles, "provider") == "prov"
    assert group_key(c, roles, "model") == "prov/mod"
    assert group_key(c, roles, "none") == ""


def test_group_key_model_keeps_two_endpoints_apart() -> None:
    a = call(provider="openai-compatible", model="m", endpoint_id="ep1")
    b = call(provider="openai-compatible", model="m", endpoint_id="ep2")
    assert group_key(a, {}, "model") == "openai-compatible/ep1/m"
    assert group_key(b, {}, "model") == "openai-compatible/ep2/m"


def test_summarize_groups_rows_by_each_dimension_sorted_by_key() -> None:
    items = placed_for(
        [
            call(1, cost="0.10", role="zeta", provider="pb", model="m2", task=None),
            call(2, cost="0.25", role="alpha", provider="pa", model="m1", task=None),
            call(3, cost="0.05", role="zeta", provider="pb", model="m2", task=None),
        ],
        [],
    )
    by_role = summarize(items, [], {}, group_by="role", start=None, end=None)
    # zeta 0.10 + 0.05 = 0.15 ; alpha 0.25 ; rows sorted by key
    assert [r["group"] for r in by_role.rows] == ["alpha", "zeta"]
    assert [r["total"]["known_total"] for r in by_role.rows] == ["0.25", "0.15"]  # type: ignore[index]
    by_provider = summarize(items, [], {}, group_by="provider", start=None, end=None)
    assert [r["group"] for r in by_provider.rows] == ["pa", "pb"]
    assert [r["total"]["known_total"] for r in by_provider.rows] == ["0.25", "0.15"]  # type: ignore[index]
    by_model = summarize(items, [], {}, group_by="model", start=None, end=None)
    assert [r["group"] for r in by_model.rows] == ["pa/m1", "pb/m2"]
    none = summarize(items, [], {}, group_by="none", start=None, end=None)
    assert none.rows == []
    assert none.overall["total"]["known_total"] == "0.4"  # type: ignore[index]  # 0.10+0.25+0.05


# -- closed attempts and buckets ------------------------------------------------------------------


def test_accepted_call_before_a_closing_move_is_retry_spend() -> None:
    item = only(
        [call(3)],
        [move(5, "in_progress", closed=True), move(7, "done")],
    )
    assert (item.bucket, item.in_retry) == ("accepted", True)


def test_accepted_call_after_the_last_closing_move_is_not_retry_spend() -> None:
    item = only([call(6)], [move(5, "in_progress", closed=True), move(7, "done")])
    assert (item.bucket, item.in_retry) == ("accepted", False)


def test_closing_move_with_the_same_seq_is_not_after_the_call() -> None:
    # strict `>`: a move at the call's own seq does not count as a later closing move
    item = only([call(5)], [move(5, "in_progress", closed=True), move(7, "done")])
    assert item.in_retry is False


def test_a_move_that_did_not_close_an_attempt_is_not_retry_spend() -> None:
    item = only([call(3)], [move(5, "in_progress", closed=False), move(7, "done")])
    assert item.in_retry is False


def test_open_task_with_a_later_closing_move_is_rejected_gate_failed() -> None:
    item = only([call(3)], [move(5, "in_progress", closed=True)])
    assert (item.bucket, item.reason) == ("rejected", "gate_failed")


def test_open_task_with_an_earlier_closing_move_is_pending() -> None:
    item = only([call(6)], [move(5, "in_progress", closed=True)])
    assert (item.bucket, item.reason) == ("pending", None)


def test_open_task_with_a_later_non_closing_move_is_pending() -> None:
    item = only([call(3)], [move(5, "in_progress", closed=False)])
    assert (item.bucket, item.reason) == ("pending", None)


def test_cancelled_task_reason_follows_the_last_cancelling_actor() -> None:
    human = only([call(3)], [move(5, "cancelled", actor="human:alice")])
    assert (human.bucket, human.reason) == ("rejected", "cancelled_by_human")
    system = only([call(3)], [move(5, "cancelled", actor="system:engine")])
    assert (system.bucket, system.reason) == ("rejected", "cancelled_by_system")
    # the LAST cancelling move decides
    last = only(
        [call(3)],
        [move(4, "cancelled", actor="human:alice"), move(5, "cancelled", actor="system:x")],
    )
    assert last.reason == "cancelled_by_system"


def test_a_call_with_no_task_is_unattributed_and_never_retry() -> None:
    item = only([call(3, task=None)], [move(5, "done", closed=True)])
    assert (item.bucket, item.reason, item.in_retry) == ("unattributed", None, False)
    assert item.period_time == T0


def test_a_call_made_after_as_of_is_left_out() -> None:
    late = call(3, at=END + timedelta(seconds=1))
    assert placed_for([late], []) == []
    on_time = call(4, at=END)
    assert len(placed_for([on_time], [])) == 1


def test_a_move_after_as_of_is_not_seen() -> None:
    moves = [move(5, "done", at=END + timedelta(seconds=1))]
    item = only([call(3)], moves)
    assert item.bucket == "pending"


def test_accepted_period_time_is_the_last_done_time() -> None:
    item = only([call(3)], [move(5, "done", at=LATER)])
    assert item.period_time == LATER
    pending = only([call(3)], [])
    assert pending.period_time == T0


# -- unknown cost ---------------------------------------------------------------------------------


def item_of(c: SpendCall, bucket: str = "accepted", *, retry: bool = False) -> Placed:
    return Placed(c, bucket, None, T0, retry)  # type: ignore[arg-type]


def test_unknown_cost_is_counted_as_unknown_never_as_zero_or_provisional() -> None:
    cell = Cell()
    cell.add(item_of(call(1, cost=None, provisional=True), retry=True))
    assert cell.calls == 1
    assert cell.unknown == 1
    assert cell.known == Decimal(0)
    assert cell.provisional == 0  # an unknown call is not a provisional known one
    assert cell.retry_known == Decimal(0)


def test_known_cost_adds_exactly_and_counts_provisional_and_retry() -> None:
    cell = Cell()
    cell.add(item_of(call(1, cost="0.10", provisional=True), retry=True))
    cell.add(item_of(call(2, cost="0.20")))
    cell.add(item_of(call(3, cost="0.0000001")))
    # known = 0.10 + 0.20 + 0.0000001 = 0.3000001 ; retry = 0.10 ; provisional = 1 ; calls = 3
    assert cell.known == Decimal("0.3000001")
    assert cell.retry_known == Decimal("0.10")
    assert cell.provisional == 1
    assert (cell.calls, cell.unknown) == (3, 0)


def test_row_with_an_unknown_call_reports_at_least_not_a_per_result_figure() -> None:
    row = Row()
    row.results = 2
    row.add(item_of(call(1, cost="1.00")), "ledger")
    row.add(item_of(call(2, cost="0.50")), "ledger")
    row.add(item_of(call(3, cost=None)), "ledger")
    acc = row.to_json()["accepted"]
    # known 1.00 + 0.50 = 1.50 ; 1.50 / 2 results = 0.75 ; one unknown -> only a lower bound
    assert acc["complete"] is False  # type: ignore[index]
    assert acc["per_result"] is None  # type: ignore[index]
    assert acc["per_result_at_least"] == "0.750000"  # type: ignore[index]
    assert acc["known_total"] == "1.5"  # type: ignore[index]
    assert acc["unknown_calls"] == 1  # type: ignore[index]
    total = row.to_json()["total"]
    assert total["complete"] is False  # type: ignore[index]
    assert total["unknown_calls"] == 1  # type: ignore[index]


def test_row_with_every_cost_known_reports_per_result_and_no_lower_bound() -> None:
    row = Row()
    row.results = 4
    row.add(item_of(call(1, cost="1.00")), "ledger")
    row.add(item_of(call(2, cost="0.50")), "ledger")
    acc = row.to_json()["accepted"]
    # 1.50 / 4 = 0.375
    assert acc["complete"] is True  # type: ignore[index]
    assert acc["per_result"] == "0.375000"  # type: ignore[index]
    assert acc["per_result_at_least"] is None  # type: ignore[index]


def test_row_without_results_has_no_ratio() -> None:
    row = Row()
    row.add(item_of(call(1, cost="1.00")), "ledger")
    acc = row.to_json()["accepted"]
    assert acc["per_result"] is None  # type: ignore[index]
    assert acc["per_result_at_least"] is None  # type: ignore[index]


def test_unknown_in_another_bucket_makes_the_total_incomplete_but_not_accepted() -> None:
    row = Row()
    row.results = 1
    row.add(item_of(call(1, cost="2")), "ledger")
    row.add(item_of(call(2, cost=None), "pending"), "ledger")
    data = row.to_json()
    assert data["accepted"]["complete"] is True  # type: ignore[index]
    assert data["total"]["complete"] is False  # type: ignore[index]
    assert row.total_complete is False
    assert row.total_known == Decimal(2)


# -- row bookkeeping ------------------------------------------------------------------------------


def test_row_counts_reasons_and_role_sources() -> None:
    row = Row()
    rejected = Placed(call(1, cost="0.3"), "rejected", "gate_failed", T0, False)
    row.add(rejected, "ledger")
    row.add(item_of(call(2)), "config")
    row.add(item_of(call(3)), "config")
    assert (row.ledger_roles, row.config_roles) == (1, 2)
    assert role_source_of(row) == "mixed"
    by_reason = row.to_json()["rejected"]["by_reason"]  # type: ignore[index]
    assert by_reason == {"gate_failed": {"known_total": "0.3", "calls": 1, "unknown_calls": 0}}


def test_role_source_of_names_the_single_source() -> None:
    ledger_only = Row()
    ledger_only.add(item_of(call(1)), "ledger")
    assert role_source_of(ledger_only) == "ledger"
    config_only = Row()
    config_only.add(item_of(call(1)), "config")
    assert role_source_of(config_only) == "config"
    assert role_source_of(Row()) == "config"


def test_summarize_share_is_the_group_part_of_the_overall_known_total() -> None:
    items = placed_for(
        [
            call(1, cost="0.75", role="a", task=None),
            call(2, cost="0.25", role="b", task=None),
        ],
        [],
    )
    out = summarize(items, [], {}, group_by="role", start=None, end=None, share=True)
    # 0.75 / 1.00 = 0.75 ; 0.25 / 1.00 = 0.25
    assert [r["share"] for r in out.rows] == ["0.750000", "0.250000"]
    off = summarize(items, [], {}, group_by="role", start=None, end=None)
    assert all("share" not in r for r in off.rows)
    zero = summarize(
        placed_for([call(1, cost="0", role="a", task=None)], []),
        [],
        {},
        group_by="role",
        start=None,
        end=None,
        share=True,
    )
    assert zero.rows[0]["share"] is None


def test_summarize_marks_the_overall_total_approximate_when_a_cost_is_unknown() -> None:
    known = summarize(
        placed_for([call(1, cost="1", task=None)], []),
        [],
        {},
        group_by="none",
        start=None,
        end=None,
    )
    assert known.approximate is False
    unknown = summarize(
        placed_for([call(1, cost=None, task=None)], []),
        [],
        {},
        group_by="none",
        start=None,
        end=None,
    )
    assert unknown.approximate is True
    assert unknown.overall["total"]["known_total"] == "0"  # type: ignore[index]


def test_summarize_results_count_distinct_accepted_tasks_per_group() -> None:
    moves = [move(9, "done")]
    items = placed_for([call(1, cost="1"), call(2, cost="1")], moves)
    done = [(("r1", "t1"), T0)]
    out = summarize(items, done, {}, group_by="role", start=None, end=None)
    assert out.overall["accepted"]["results"] == 1  # type: ignore[index]
    assert out.rows[0]["accepted"]["results"] == 1  # type: ignore[index]  # one task, two calls
    # a pending task (not accepted) adds no result to its group
    pending = placed_for([call(1, cost="1")], [])
    out2 = summarize(pending, [], {}, group_by="role", start=None, end=None)
    assert out2.rows[0]["accepted"]["results"] == 0  # type: ignore[index]


def test_summarize_period_is_half_open_and_uses_period_time() -> None:
    items = [
        Placed(call(1, cost="1"), "accepted", None, T0, False),
        Placed(call(2, cost="2"), "accepted", None, LATER, False),
    ]
    out = summarize(items, [], {}, group_by="none", start=T0, end=LATER)
    assert out.overall["total"]["known_total"] == "1"  # type: ignore[index]  # start in, end out


# -- quotes ---------------------------------------------------------------------------------------


PV = datetime(2026, 1, 1, tzinfo=UTC)


def run_facts(
    name: str,
    cost: str | None,
    *,
    ended: datetime = T0,
    aborted: bool = False,
    all_done: bool = True,
    price_at: datetime | None = PV,
    assignees: tuple[str | None, ...] = ("ag",),
) -> RunFacts:
    return RunFacts(
        run=name,
        ended_at=ended,
        aborted=aborted,
        task_assignees=assignees,
        all_done=all_done,
        calls=(call(1, cost=cost, price_version_at=price_at),),
    )


def test_quote_needs_five_comparable_runs() -> None:
    four = [run_facts(f"r{i}", str(i + 1)) for i in range(4)]
    result = quote(["worker"], {"prov/mod"}, four, {"ag": "worker"}, {"prov/mod": PV}, END)
    assert (result.status, result.sample_size, result.low) == ("not_enough_history", 4, None)
    five = [run_facts(f"r{i}", str(i + 1)) for i in range(5)]
    result = quote(["worker"], {"prov/mod"}, five, {"ag": "worker"}, {"prov/mod": PV}, END)
    # totals 1,2,3,4,5 -> quartiles at ranks 1, 2, 3 -> 2, 3, 4
    assert result.status == "ok"
    assert (result.low, result.median, result.high) == ("2.000000", "3.000000", "4.000000")
    assert result.sample_size == 5


def test_quote_excludes_runs_and_counts_each_reason() -> None:
    runs = [
        run_facts("a", "1", aborted=True),
        run_facts("b", "1", all_done=False),
        run_facts("c", None),
        run_facts("d", "1", price_at=None),
        run_facts("e", "1", price_at=datetime(2025, 1, 1, tzinfo=UTC)),
        run_facts("f", "1"),
    ]
    result = quote(["worker"], {"prov/mod"}, runs, {"ag": "worker"}, {"prov/mod": PV}, END)
    assert dict(result.excluded) == {"not_clean": 2, "unknown_cost": 1, "price_changed": 2}
    assert result.sample_size == 1


def test_quote_ignores_unended_future_and_non_matching_runs() -> None:
    other_role = run_facts("x", "1", assignees=("stranger",))
    unassigned = run_facts("y", "1", assignees=(None,))
    unended = RunFacts("z", None, False, ("ag",), True, (call(1, price_version_at=PV),))
    future = run_facts("w", "1", ended=END + timedelta(seconds=1))
    ended_now = run_facts("v", "1", ended=END)
    runs = [other_role, unassigned, unended, future, ended_now]
    result = quote(["worker"], {"prov/mod"}, runs, {"ag": "worker"}, {"prov/mod": PV}, END)
    assert result.sample_size == 1
    assert dict(result.excluded) == {"not_clean": 0, "unknown_cost": 0, "price_changed": 0}
    # a missing assignee counts as the `unknown` role
    result = quote(["unknown"], {"prov/mod"}, [unassigned], {}, {"prov/mod": PV}, END)
    assert result.sample_size == 1
    # a run whose models differ from the wanted set is not comparable
    result = quote(["worker"], {"prov/other"}, [ended_now], {"ag": "worker"}, {}, END)
    assert result.sample_size == 0


def test_quote_stops_at_twenty_runs_and_keeps_the_newest() -> None:
    # 25 runs, newest first; run i costs i+1 USD; only the newest 20 (costs 1..20) are kept
    runs = [run_facts(f"r{i:02d}", str(i + 1), ended=END - timedelta(hours=i)) for i in range(25)]
    walked: list[str] = []

    def lazy() -> object:
        for r in runs:
            walked.append(r.run)
            yield r

    result = quote(["worker"], {"prov/mod"}, lazy(), {"ag": "worker"}, {"prov/mod": PV}, END)  # type: ignore[arg-type]
    assert result.sample_size == 20
    assert len(walked) == 20  # the walk stops at the 20th comparable run
    # totals 1..20: rank h = 19*k/4 -> 4.75, 9.5, 14.25 ; values are rank+1 -> 5.75, 10.5, 15.25
    assert (result.low, result.median, result.high) == ("5.750000", "10.500000", "15.250000")
