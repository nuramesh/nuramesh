"""Scorecards (E14): the cases S1 to S54 of `spec/autonomy/README.md` sections 9.1 to 9.3."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from autonomy_world import SUITES, T, World, d

from nuramesh.core.autonomy import Entry
from nuramesh.core.ledger import CostBasis, format_time
from nuramesh.core.money import Money
from nuramesh.core.scorecard import (
    ScorecardError,
    make_period,
    scorecard,
)

AS_OF = T


def card(
    w: World,
    kind: str = "agent",
    subject: str = "a1",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime = AS_OF,
    min_results: int = 20,
    evals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    period, minimum = make_period(start, end, as_of, min_results)
    return scorecard(
        w.entries,
        kind=kind,
        subject=subject,
        period=period,
        min_results=minimum,
        evals=evals or [],
        required_suites=SUITES,
    )


def world() -> World:
    w = World()
    w.config(d(-60))
    return w


_n = {"t": 0}


def results(
    w: World,
    outcomes: list[tuple[str, bool]],
    *,
    at: datetime | None = None,
    agent: str = "a1",
    kind: str = "test_command",
) -> None:
    """One verdict per (kind, passed) on its own task, assigned to `agent`."""
    at = at or d(-5)
    for i, (gate_kind, passed) in enumerate(outcomes):
        _n["t"] += 1
        task = f"t{_n['t']}"
        when = at + timedelta(seconds=i)
        w.add(
            "run:r1", "task.transitioned", when, {"to": "verifying", "assignee": agent}, task=task
        )
        w.add(
            "run:r1",
            "gate.completed",
            when,
            {"gate": "g1", "kind": gate_kind, "outcome": "passed" if passed else "failed"},
            task=task,
        )


def passed(n: int, kind: str = "test_command") -> list[tuple[str, bool]]:
    return [(kind, True)] * n


def failed(n: int, kind: str = "test_command") -> list[tuple[str, bool]]:
    return [(kind, False)] * n


@pytest.mark.parametrize(
    ("good", "bad", "rate", "sufficient"),
    [
        (20, 0, "1.0000", True),  # S1
        (19, 0, None, False),  # S2
        (15, 5, "0.7500", True),  # S3
        (14, 7, "0.6667", True),  # S4
        (1, 31, "0.0312", True),  # S5: 1/32 = 0.03125, half to even
        (3, 29, "0.0938", True),  # S6: 3/32 = 0.09375, half to even
    ],
)
def test_pass_rate_rounds_half_even_once(
    good: int, bad: int, rate: str | None, sufficient: bool
) -> None:
    w = world()
    results(w, passed(good) + failed(bad))
    c = card(w)
    assert (c["verdicts"], c["passed"], c["pass_rate"], c["sufficient"]) == (
        good + bad,
        good,
        rate,
        sufficient,
    )


def test_s7_verifier_and_gate_verdicts_are_counted_separately() -> None:
    w = world()
    results(w, passed(10) + passed(10, "verifier") + failed(2, "verifier"))
    c = card(w)
    assert c["gates"]["gate"] == {"passed": 10, "failed": 0, "error": 0}
    assert c["gates"]["verifier"] == {"passed": 10, "failed": 2, "error": 0}
    assert (c["verdicts"], c["passed"], c["pass_rate"]) == (22, 20, "0.9091")


def test_s8_s9_s10_errors_human_and_other_outcomes_are_not_verdicts() -> None:
    w2 = world()
    results(w2, passed(20))
    _n["t"] += 1
    for i in range(5):
        w2.add("run:r1", "task.transitioned", d(-3, i), {"to": "x", "assignee": "a1"}, task="te")
        w2.add(
            "run:r1",
            "gate.completed",
            d(-3, i),
            {"kind": "test_command", "outcome": "error"},
            task="te",
        )
    for i in range(10):
        w2.add(
            "run:r1", "gate.completed", d(-2, i), {"kind": "human", "outcome": "failed"}, task="te"
        )
    for i in range(3):
        w2.add(
            "run:r1",
            "gate.completed",
            d(-1, i),
            {"kind": "test_command", "outcome": "skipped"},
            task="te",
        )
    c = card(w2)
    assert c["gates"]["gate"]["error"] == 5 and c["gates"]["human_ignored"] == 10
    assert (c["verdicts"], c["pass_rate"]) == (20, "1.0000")


def test_s11_s12_the_period_start_is_inclusive() -> None:
    w = world()
    results(w, passed(19))
    results(w, passed(1), at=d(-30))
    assert card(w)["verdicts"] == 20
    w = world()
    results(w, passed(19))
    results(w, passed(1), at=d(-30, -1))
    c = card(w)
    assert (c["verdicts"], c["pass_rate"]) == (19, None)


def test_s13_s15_the_end_is_exclusive_unless_omitted() -> None:
    w = world()
    results(w, passed(19), at=d(-3))
    results(w, passed(1), at=d(-1))
    assert card(w, end=d(-1))["verdicts"] == 19
    w = world()
    results(w, passed(19), at=d(-3))
    results(w, passed(1), at=T)
    c = card(w)
    assert (c["verdicts"], c["pass_rate"]) == (20, "1.0000")


def test_s14_s22_nothing_after_as_of_exists() -> None:
    w = world()
    results(w, passed(20), at=d(-9))
    results(w, failed(5), at=d(-4))
    c = card(w, as_of=d(-5))
    assert (c["verdicts"], c["pass_rate"]) == (20, "1.0000")
    w = world()
    results(w, passed(20), at=d(-9))
    results(w, passed(5), at=d(1))
    assert card(w, end=d(10))["verdicts"] == 20


def test_s16_a_verdict_belongs_to_the_assignee_at_that_point() -> None:
    w = world()
    w.add("run:r1", "task.transitioned", d(-5), {"to": "verifying", "assignee": "a1"}, task="t1")
    w.add(
        "run:r1",
        "gate.completed",
        d(-5, 1),
        {"kind": "test_command", "outcome": "passed"},
        task="t1",
    )
    w.add("run:r1", "task.transitioned", d(-5, 2), {"to": "verifying", "assignee": "a2"}, task="t1")
    w.add(
        "run:r1",
        "gate.completed",
        d(-5, 3),
        {"kind": "test_command", "outcome": "failed"},
        task="t1",
    )
    a1 = card(w, min_results=1)
    a2 = card(w, subject="a2", min_results=1)
    role = card(w, "role", "dev", min_results=1)
    assert (a1["passed"], a1["verdicts"]) == (1, 1)
    assert (a2["passed"], a2["verdicts"]) == (0, 1)
    assert (role["passed"], role["verdicts"]) == (1, 2)


def test_s17_s19_roles_sum_their_agents_and_small_samples_have_no_rate() -> None:
    w = world()
    results(w, passed(12))
    results(w, failed(8), agent="a2")
    role = card(w, "role", "dev")
    assert (role["verdicts"], role["pass_rate"]) == (20, "0.6000")
    assert card(w)["pass_rate"] is None
    w = world()
    results(w, passed(1))
    one = card(w, min_results=1)
    assert (one["pass_rate"], one["sufficient"]) == ("1.0000", True)


def test_s18_other_organizations_runs_are_not_counted() -> None:
    # the reader gives the core only this organization's streams; a foreign stream never reaches it
    w = world()
    assert card(w)["verdicts"] == 0


@pytest.mark.parametrize(("minimum", "ok"), [(0, False), (1001, False), (1000, True), (1, True)])
def test_s20_min_results_bounds(minimum: int, ok: bool) -> None:
    if ok:
        make_period(None, None, AS_OF, minimum)
    else:
        with pytest.raises(ScorecardError) as caught:
            make_period(None, None, AS_OF, minimum)
        assert caught.value.status == 400


def test_s21_bad_periods_are_refused() -> None:
    with pytest.raises(ScorecardError):
        make_period(T, T - timedelta(days=1), AS_OF, 20)
    with pytest.raises(ScorecardError):
        make_period(T - timedelta(days=400), T, AS_OF, 20)


def test_s48_an_unknown_subject_is_not_found() -> None:
    w = world()
    with pytest.raises(ScorecardError) as caught:
        card(w, subject="zz")
    assert caught.value.status == 404


def test_s51_s53_s54_version_and_recomputation() -> None:
    w = world()
    results(w, passed(20))
    before = len(w.entries)
    first, second = card(w), card(w)
    assert first["scorecard_version"] == 1 and first == second
    assert len(w.entries) == before


# -- cost per verified result (9.2) ----------------------------------------------------------


def call(
    w: World,
    task: str,
    cost: str | None,
    *,
    at: datetime | None = None,
    agent: str = "a1",
    outcome: str = "ok",
    basis: CostBasis | None = CostBasis.ACTUAL,
    role: str | None = None,
) -> Entry:
    at = at or d(-5)
    payload: dict[str, Any] = {"agent": agent, "provider": "p", "model": "m1", "outcome": outcome}
    if role is not None:
        payload["role"] = role
    return w.add(
        "run:r1",
        "model.called",
        at,
        payload,
        task=task,
        cost=None if cost is None else Money.of(cost),
        cost_basis=basis,
    )


def move(w: World, task: str, to: str, at: datetime, assignee: str = "a1", **extra: Any) -> Entry:
    return w.add(
        "run:r1", "task.transitioned", at, {"to": to, "assignee": assignee, **extra}, task=task
    )


def done(w: World, *tasks: str, at: datetime | None = None, assignee: str = "a1") -> None:
    at = at or d(-1)
    for i, task in enumerate(tasks):
        move(w, task, "done", at + timedelta(seconds=i), assignee)


@pytest.mark.parametrize(
    ("costs", "per_result", "known"),
    [
        (["0.012"], "0.012000", "0.012"),  # S23
        (["0.10", "0.20"], "0.150000", "0.30"),  # S24
        (["0.0000015"], "0.000002", "0.0000015"),  # S26
        (["0.0000025"], "0.000002", "0.0000025"),  # S27
        (["0.0000035"], "0.000004", "0.0000035"),  # S28
    ],
)
def test_per_result_rounds_half_even(costs: list[str], per_result: str, known: str) -> None:
    w = world()
    tasks = [f"t{i}" for i in range(len(costs))]
    for task, cost in zip(tasks, costs, strict=True):
        call(w, task, cost)
    done(w, *tasks)
    c = card(w)["cost"]
    assert (c["results"], c["per_result"], Decimal(c["known_total"])) == (
        len(costs),
        per_result,
        Decimal(known),
    )


def test_s25_one_call_over_three_results() -> None:
    w = world()
    call(w, "t1", "0.01")
    done(w, "t1", "t2", "t3")
    assert card(w)["cost"]["per_result"] == "0.003333"


def test_s29_retries_are_part_of_the_cost_of_the_result() -> None:
    w = world()
    call(w, "t1", "0.05")
    move(w, "t1", "running", d(-4), closed_attempt=True)
    call(w, "t1", "0.05", at=d(-3))
    done(w, "t1")
    c = card(w)["cost"]
    assert (Decimal(c["known_total"]), Decimal(c["retry_known_total"]), c["per_result"]) == (
        Decimal("0.10"),
        Decimal("0.05"),
        "0.100000",
    )


def test_s30_an_unknown_call_makes_the_per_result_a_lower_bound() -> None:
    w = world()
    call(w, "t1", "0.01")
    call(w, "t1", None, outcome="settle_failed", basis=CostBasis.UNKNOWN)
    done(w, "t1")
    c = card(w)["cost"]
    assert (c["unknown_calls"], c["per_result"], c["per_result_at_least"]) == (1, None, "0.010000")


def test_s31_s38_spend_on_unfinished_work_is_never_a_result_cost() -> None:
    w = world()
    move(w, "t1", "needs_human", d(-2))
    call(w, "t1", "0.50", at=d(-2))
    c = card(w)["cost"]
    assert (c["results"], c["per_result"], c["per_result_at_least"]) == (0, None, None)
    assert Decimal(c["unaccepted_known_total"]) == Decimal("0.50")
    assert card(world())["cost"]["per_result"] is None


def test_s32_other_agents_calls_are_not_the_workers_cost() -> None:
    w = world()
    done(w, "t1")
    call(w, "t1", "0.04", agent="a2", role="rev")
    call(w, "t1", "0.06")
    c = card(w)["cost"]
    assert (Decimal(c["known_total"]), c["per_result"]) == (Decimal("0.06"), "0.060000")


def test_s33_s34_accepted_spend_follows_the_done_time() -> None:
    w = world()
    call(w, "t1", "0.01", at=d(-3))
    move(w, "t1", "done", d(-1))
    assert card(w, end=d(-1))["cost"]["results"] == 0
    w = world()
    call(w, "t1", "0.02", at=d(-35))
    move(w, "t1", "done", d(-30))
    c = card(w, start=d(-30))["cost"]
    assert (c["results"], Decimal(c["known_total"])) == (1, Decimal("0.02"))


def test_s35_s36_s37_no_spend_provisional_and_no_calls() -> None:
    w = world()
    done(w, "t1")
    call(w, "t1", None, outcome="error", basis=None)
    c = card(w)["cost"]
    assert (Decimal(c["known_total"]), c["unknown_calls"]) == (Decimal(0), 0)
    w = world()
    done(w, "t1")
    call(w, "t1", "0.03", basis=CostBasis.PROVISIONAL)
    c = card(w)["cost"]
    assert (Decimal(c["known_total"]), c["provisional_calls"], c["per_result"]) == (
        Decimal("0.03"),
        1,
        "0.030000",
    )
    w = world()
    done(w, "t1")
    assert card(w)["cost"]["per_result"] == "0.000000"


def test_s39_s40_role_and_agent_cost_views() -> None:
    w = world()
    done(w, "t1")
    call(w, "t1", "0.01", role="dev")
    call(w, "t1", "0.02", role="dev")
    call(w, "t1", "0.04", agent="a2")
    assert Decimal(card(w, "role", "dev")["cost"]["known_total"]) == Decimal("0.07")
    assert card(w)["cost"]["per_result"] == "0.030000"
    w = world()
    done(w, "t1")
    done(w, "t2", assignee="a2")
    call(w, "t1", "0.02")
    call(w, "t2", "0.04", agent="a2")
    assert card(w)["cost"]["per_result"] == "0.020000"
    assert card(w, subject="a2")["cost"]["per_result"] == "0.040000"
    role = card(w, "role", "dev")["cost"]
    assert (role["results"], Decimal(role["known_total"]), role["per_result"]) == (
        2,
        Decimal("0.06"),
        "0.030000",
    )


# -- approvals, safety, evals (9.3) ----------------------------------------------------------


def no_solo(share: str | None, **counts: int) -> dict[str, Any]:
    """The `approvals` object of entries that carry no `approval_mode` (written before ND396):
    never solo, counted in the totals (spec/rbac/solo-approval.md SA-18 item 1)."""
    return {**counts, "approved_solo": 0, "denied_solo": 0, "solo_share": share}


def test_s41_s42_s43_approvals_follow_their_own_times() -> None:
    w = world()
    for i, answer in enumerate([True, True, False]):
        w.add("run:r1", "approval.requested", d(-6, i), {"agent": "a1", "approval": f"p{i}"})
        w.add(
            "run:r1",
            "approval.decided",
            d(-5, i),
            {"agent": "a1", "approval": f"p{i}", "approved": answer},
        )
    c = card(w)["approvals"]
    assert c == no_solo("0.0000", requested=3, approved=2, denied=1, pending=0)
    w = world()
    for i, when in enumerate([d(-1), d(1)]):
        w.add("run:r1", "approval.requested", d(-6, i), {"agent": "a1", "approval": f"p{i}"})
        w.add(
            "run:r1",
            "approval.decided",
            when,
            {"agent": "a1", "approval": f"p{i}", "approved": True},
        )
    assert card(w)["approvals"] == no_solo("0.0000", requested=2, approved=1, denied=0, pending=1)
    w = world()
    w.add("run:r1", "approval.requested", d(-40), {"agent": "a1", "approval": "p0"})
    w.add("run:r1", "approval.decided", d(-5), {"agent": "a1", "approval": "p0", "approved": True})
    assert card(w)["approvals"] == no_solo("0.0000", requested=0, approved=1, denied=0, pending=0)


def test_s44_s45_safety_counts_exclude_stops() -> None:
    w = world()
    w.add("run:r1", "model.blocked", d(-3), {"agent": "a1"})
    w.add("run:r1", "model.blocked", d(-3, 1), {"agent": "a1"})
    w.add("run:r1", "budget.breached", d(-3, 2), {"agent": "a1"})
    w.add("run:r1", "model.blocked", d(-3, 3), {"agent": "a2"})
    w.add("run:r1", "run.stop_requested", d(-3, 4), {"by": "human:o"})
    assert card(w)["safety"] == {"model_blocked": 2, "budget_breached": 1}
    assert card(w, "role", "dev")["safety"] == {"model_blocked": 3, "budget_breached": 1}


def test_s46_s47_eval_rows_come_from_the_caller() -> None:
    w = world()
    rows = [{"suite": "smoke", "status": "blocked", "reason": "latest result failed"}]
    assert card(w, evals=rows)["evals"] == rows
    assert card(w)["evals"] == []


def test_the_scorecard_lists_active_grants_and_the_last_demotion() -> None:
    w = world()
    w.ceiling(d(-60, 1), "allow")
    w.grant("g1", subject=("agent", "a1"), at=d(-10))
    assert card(w)["autonomy"] == {"active_grants": ["g1"], "last_demotion": None}
    w.add("org:o1", "budget.breached", d(-3), {"agent": "a1"})
    c = card(w)["autonomy"]
    assert c["active_grants"] == [] and c["last_demotion"]["trigger"] == "budget_breached"
    assert c["last_demotion"]["at"] == format_time(d(-3))
