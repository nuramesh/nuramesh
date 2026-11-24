"""R1.42 core pieces: config and task validation (C38, C39), serialisation, decide_cap, C40."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import Budget, BudgetBalance, BudgetScope, CallContext
from nuramesh.core.config import ConfigValidationError, parse_config
from nuramesh.core.money import CurrencyMismatchError, Money
from nuramesh.core.org import Role
from nuramesh.core.output_caps import CapBinding, NoRoom, OutputCap, decide_cap
from nuramesh.core.prices import MAX_TOKENS, Price, PriceBook, resolve_price
from nuramesh.core.tasks import Task

ORG: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "roles": [
        {"id": "cos", "name": "CoS", "kind": "chief_of_staff"},
        {"id": "dev", "name": "Dev"},
    ],
    "agents": [
        {"id": "boss", "name": "Boss", "role": "cos"},
        {"id": "ada", "name": "Ada", "role": "dev", "reports_to": "boss"},
    ],
}
BAD = [0, -1, 10**12 + 1, True, "500", 500.0]


def test_role_cap_limit_matches_the_price_limit() -> None:
    from nuramesh.core import org

    assert org._MAX_TOKENS == MAX_TOKENS


@pytest.mark.parametrize("bad", BAD, ids=repr)
def test_c38_a_bad_role_cap_is_refused_with_its_path(bad: Any) -> None:
    data = json.loads(json.dumps(ORG))
    data["roles"][1]["max_tokens"] = bad
    with pytest.raises(ConfigValidationError) as info:
        parse_config({"org": data})
    assert "$.org.roles[1].max_tokens" in [i.path for i in info.value.issues]


@pytest.mark.parametrize("ok", [1, 10**12])
def test_c38_the_limits_are_accepted(ok: int) -> None:
    data = json.loads(json.dumps(ORG))
    data["roles"][1]["max_tokens"] = ok
    assert parse_config({"org": data}).org.role_of("ada").max_tokens == ok


def _task(**kw: Any) -> Task:
    dod = {
        "criteria": [{"id": "a", "text": "A"}],
        "gates": [{"id": "g", "kind": "verifier", "checks": ["a"]}],
    }
    return Task.model_validate(
        {"id": "t1", "run_id": "r1", "title": "T", "definition_of_done": dod, **kw}
    )


@pytest.mark.parametrize("bad", BAD, ids=repr)
def test_c39_a_bad_task_cap_is_refused(bad: Any) -> None:
    with pytest.raises(ValidationError) as info:
        _task(max_tokens=bad)
    assert info.value.errors()[0]["loc"] == ("max_tokens",)


@pytest.mark.parametrize("ok", [1, 10**12])
def test_c39_the_limits_are_accepted(ok: int) -> None:
    assert _task(max_tokens=ok).max_tokens == ok


def test_planned_task_has_no_cap_field_of_its_own() -> None:
    from nuramesh.engine.run import PlannedTask

    assert "max_tokens" not in PlannedTask.__dataclass_fields__


def test_an_unset_cap_leaves_serialisation_byte_identical() -> None:
    task = _task()
    dod = json.dumps(task.model_dump(mode="json")["definition_of_done"], separators=(",", ":"))
    pre_r142 = (
        '{"id":"t1","run_id":"r1","parent_id":null,"title":"T","instructions":"",'
        f'"definition_of_done":{dod},"assignee":null,"status":"proposed","attempt":0,'
        '"max_attempts":3,"attempt_closed":false,"submission":0}'
    ).encode()
    assert task.model_dump_json().encode() == pre_r142
    assert "max_tokens" not in task.model_dump()
    assert _task(max_tokens=5).model_dump()["max_tokens"] == 5
    assert Task.model_validate_json(_task(max_tokens=5).model_dump_json()).max_tokens == 5
    ctx = CallContext(run_id="r1", agent_id="a")
    assert ctx.model_dump_json().encode() == b'{"run_id":"r1","task_id":null,"agent_id":"a"}'
    full = CallContext(run_id="r1", agent_id="a", task_max_tokens=7, cap_exempt=True)
    assert full.model_dump()["task_max_tokens"] == 7 and full.model_dump()["cap_exempt"] is True
    assert Role(id="r", name="R").max_tokens is None


@pytest.mark.parametrize(
    ("model", "field"), [(Task, "max_tokens"), (CallContext, "task_max_tokens")]
)
def test_the_serialisation_json_schema_keeps_its_fields(model: Any, field: str) -> None:
    for mode in ("validation", "serialization"):
        assert field in model.model_json_schema(mode=mode)["properties"]


PA = resolve_price(
    PriceBook(
        prices=(
            Price(
                provider="p",
                model="m",
                input_per_million=Decimal("2"),
                output_per_million=Decimal("10"),
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                source="test",
            ),
        )
    ),
    "p",
    "m",
    datetime(2026, 10, 1, tzinfo=UTC),
)


def balance(bid: str, limit: str, *, blocked: bool = False) -> BudgetBalance:
    return BudgetBalance(
        budget=Budget(id=bid, scope=BudgetScope.RUN, scope_id="r1", limit=Money.of(limit)),
        spent=Money.of("0"),
        reserved=Money.of("0"),
        blocked=blocked,
    )


def test_decide_cap_basic_and_invariant() -> None:
    d = decide_cap(1000, 1000, None, [balance("b", "0.0052")], 100, PA)
    assert isinstance(d, OutputCap) and d.effective == 500 and d.binding is CapBinding.BUDGET
    assert d.available == Money.of("0.0052")
    for limit in ("0.005199", "0.0052", "0.00521", "0.0060"):
        r = decide_cap(1000, 1000, None, [balance("b", limit)], 100, PA)
        assert isinstance(r, OutputCap)
        a = Decimal(limit) * 10**6
        assert 200 + r.effective * 10 <= a < 200 + (r.effective + 1) * 10


def test_decide_cap_no_cap_and_empty_budgets_never_raise() -> None:
    d = decide_cap(1000, None, None, [], 100, PA)
    assert isinstance(d, OutputCap) and d.effective == 1000 and d.binding is None
    e = decide_cap(1000, 500, None, [], 100, PA)
    assert isinstance(e, OutputCap) and e.effective == 500 and e.binding is CapBinding.ROLE


def test_decide_cap_blocked_counts_as_zero_and_ties_by_id() -> None:
    r = decide_cap(1000, 1000, None, [balance("b", "1", blocked=True)], 100, PA)
    assert isinstance(r, NoRoom)
    t = decide_cap(1000, 1000, None, [balance("z", "0.0052"), balance("a", "0.0052")], 100, PA)
    assert isinstance(t, OutputCap) and t.limiting_budget_id == "a"


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_decide_cap_rejects_non_integers(bad: Any) -> None:
    with pytest.raises(ValueError):
        decide_cap(bad, None, None, [], 1, PA)


def test_c40_a_currency_mismatch_is_refused() -> None:
    eur = Money.model_construct(amount=Decimal("1"), currency="EUR")  # not constructible today
    foreign = BudgetBalance.model_construct(
        budget=Budget.model_construct(id="b", limit=eur),
        spent=Money.of("0"),
        reserved=Money.of("0"),
        blocked=False,
    )
    with pytest.raises(CurrencyMismatchError):
        decide_cap(1000, 500, None, [foreign], 100, PA)


def test_c19_free_price_fits_with_nothing_available() -> None:
    free = PA.model_construct(
        **{**PA.model_dump(), "input_per_million": Decimal(0), "output_per_million": Decimal(0)}
    )
    d = decide_cap(1000, 500, None, [balance("b", "0")], 100, free)
    assert isinstance(d, OutputCap) and d.effective == 500 and d.binding is CapBinding.ROLE
    assert d.budget_cap is None


def test_c34_huge_available_never_reaches_the_division() -> None:
    tiny = PA.model_construct(
        **{
            **PA.model_dump(),
            "input_per_million": Decimal(1),
            "output_per_million": Decimal("1E-18"),
        }
    )
    for big in (10**37, 10**59):
        d = decide_cap(1000, 1000, None, [balance("b", str(big))], 100, tiny)
        assert isinstance(d, OutputCap) and d.effective == 1000 and d.budget_cap is None
