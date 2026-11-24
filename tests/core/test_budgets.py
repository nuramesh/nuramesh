from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import (
    Budget,
    BudgetBalance,
    BudgetExceededError,
    BudgetScope,
    CallContext,
    Reservation,
    ReservationStatus,
    applicable_budgets,
    release,
    reserve,
    settle,
    shortfalls,
)
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind

M = Money.of


def org() -> OrgGraph:
    return OrgGraph(
        id="acme",
        name="Acme",
        roles=(Role(id="cos", name="CoS", kind=RoleKind.CHIEF_OF_STAFF), Role(id="w", name="W")),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            Agent(id="lead", name="Lead", role="w", reports_to="boss"),
            Agent(id="dev", name="Dev", role="w", reports_to="lead"),
            Agent(id="other", name="Other", role="w", reports_to="boss"),
        ),
    )


def budget(id: str, scope: BudgetScope, scope_id: str, limit: str) -> Budget:
    return Budget(id=id, org_id="acme", scope=scope, scope_id=scope_id, limit=M(limit))


ALL = (
    budget("run", BudgetScope.RUN, "r1", "10"),
    budget("other-run", BudgetScope.RUN, "r2", "10"),
    budget("task", BudgetScope.TASK, "t1", "5"),
    budget("dev", BudgetScope.AGENT, "dev", "2"),
    budget("lead-team", BudgetScope.TEAM, "lead", "3"),
    budget("boss-team", BudgetScope.TEAM, "boss", "8"),
    budget("other-team", BudgetScope.TEAM, "other", "1"),
)


def test_applicable_budgets_cover_every_scope_and_team_membership() -> None:
    dev = CallContext(run_id="r1", task_id="t1", agent_id="dev")
    assert [b.id for b in applicable_budgets(ALL, dev, org())] == [
        "boss-team",
        "dev",
        "lead-team",
        "run",
        "task",
    ]
    lead = CallContext(run_id="r1", agent_id="lead")  # no task
    assert [b.id for b in applicable_budgets(ALL, lead, org())] == ["boss-team", "lead-team", "run"]


def test_a_budget_of_another_organization_never_applies_whatever_the_ids() -> None:
    foreign = tuple(b.model_copy(update={"org_id": "globex"}) for b in ALL)
    unowned = tuple(b.model_copy(update={"org_id": None}) for b in ALL)
    dev = CallContext(run_id="r1", task_id="t1", agent_id="dev")
    assert applicable_budgets(foreign, dev, org()) == ()
    assert applicable_budgets(unowned, dev, org()) == ()
    both = (*foreign, *ALL[:1])
    assert [b.id for b in applicable_budgets(both, dev, org())] == ["run"]


def test_reserve_takes_from_every_budget() -> None:
    balances = [BudgetBalance.fresh(b) for b in ALL[:1] + ALL[2:4]]
    after = reserve(balances, M("1.5"))
    assert [b.reserved for b in after] == [M("1.5")] * 3
    # dev budget: 2 - 1.5 = 0.5 left
    assert after[2].available == M("0.5")


def test_hard_stop_when_any_budget_lacks_room() -> None:
    balances = [BudgetBalance.fresh(ALL[0]), BudgetBalance.fresh(ALL[3])]  # 10 and 2
    with pytest.raises(BudgetExceededError) as caught:
        reserve(balances, M("2.000001"))
    (only,) = caught.value.shortfalls
    assert (only.budget_id, only.available, only.requested) == ("dev", M("2"), M("2.000001"))
    assert reserve(balances, M("2"))[1].available == Money.zero()  # exactly the limit is fine


def test_settle_releases_the_unused_part() -> None:
    held = reserve([BudgetBalance.fresh(ALL[0])], M("1"))
    (result,) = settle(held, M("1"), M("0.25"))
    assert (result.balance.spent, result.balance.reserved) == (M("0.25"), Money.zero())
    assert (result.released, result.overrun) == (M("0.75"), Money.zero())
    assert result.balance.available == M("9.75")


def test_settle_records_overruns_and_then_everything_is_refused() -> None:
    held = reserve([BudgetBalance.fresh(ALL[3])], M("2"))  # dev, limit 2
    (result,) = settle(held, M("2"), M("2.5"))
    assert (result.overrun, result.became_overspent) == (M("0.5"), True)
    assert result.balance.available == M("-0.5")
    with pytest.raises(BudgetExceededError):
        reserve([result.balance], Money.zero() + M("0.000001"))
    assert shortfalls([result.balance], Money.zero())  # even a zero reservation is refused


def test_release_returns_the_reservation() -> None:
    held = reserve([BudgetBalance.fresh(ALL[0])], M("3"))
    (back,) = release(held, M("3"))
    assert back == BudgetBalance.fresh(ALL[0])
    with pytest.raises(ValueError, match="doesn't hold"):
        release(list(back for _ in [0]), M("3"))


def test_validation_rules() -> None:
    with pytest.raises(ValidationError):
        budget("b", BudgetScope.RUN, "r1", "-1")
    with pytest.raises(ValueError):
        shortfalls([BudgetBalance.fresh(ALL[0])], M("-1"))
    with pytest.raises(ValueError):
        settle([BudgetBalance.fresh(ALL[0])], Money.zero(), M("-1"))
    ctx = CallContext(run_id="r1", agent_id="dev")
    base: dict[str, Any] = {"id": "res", "context": ctx, "budget_ids": ("a",), "amount": M("1")}
    with pytest.raises(ValidationError):
        Reservation(**{**base, "budget_ids": ("a", "a")})
    with pytest.raises(ValidationError):
        Reservation(**{**base, "status": ReservationStatus.SETTLED})  # no charged amount
    with pytest.raises(ValidationError):
        Reservation(**base, charged=M("1"))  # open with a charge
