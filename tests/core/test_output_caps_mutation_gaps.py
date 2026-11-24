"""Pins for core/output_caps.py found by operator-mutant replay (#1351). Fake data only."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import Budget, BudgetBalance, BudgetScope
from nuramesh.core.money import CurrencyMismatchError, Money
from nuramesh.core.output_caps import CapBinding, NoRoom, OutputCap, decide_cap
from nuramesh.core.prices import Price, PriceBook, ResolvedPrice, resolve_price


def price(
    inp: str = "2", out: str = "10", cached: str | None = None, write: str | None = None
) -> ResolvedPrice:
    book = PriceBook(
        prices=(
            Price(
                provider="p",
                model="m",
                input_per_million=Decimal(inp),
                output_per_million=Decimal(out),
                cached_input_per_million=None if cached is None else Decimal(cached),
                cache_write_per_million=None if write is None else Decimal(write),
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                source="test",
            ),
        )
    )
    return resolve_price(book, "p", "m", datetime(2026, 10, 1, tzinfo=UTC))


def balance(bid: str, limit: str, *, blocked: bool = False) -> BudgetBalance:
    return BudgetBalance(
        budget=Budget(id=bid, scope=BudgetScope.RUN, scope_id="r1", limit=Money.of(limit)),
        spent=Money.of("0"),
        reserved=Money.of("0"),
        blocked=blocked,
    )


def test_the_binding_labels_are_the_documented_words() -> None:
    assert [b.value for b in CapBinding] == ["task", "role", "budget"]


def test_lowered_is_true_only_when_the_size_went_down() -> None:
    down = decide_cap(1000, 400, None, [], 10, price())
    same = decide_cap(1000, 1000, None, [], 10, price())
    assert isinstance(down, OutputCap) and down.lowered is True
    assert isinstance(same, OutputCap) and same.lowered is False
    assert same.binding is None
    nr = NoRoom(
        requested=1000, configured=400, role_cap=400, task_cap=None, binding=CapBinding.ROLE
    )
    keep = NoRoom(requested=400, configured=400, role_cap=400, task_cap=None, binding=None)
    assert nr.lowered is True and keep.lowered is False


def test_a_decision_is_frozen() -> None:
    d = decide_cap(1000, 400, None, [], 10, price())
    assert isinstance(d, OutputCap)
    with pytest.raises(ValidationError):
        d.effective = 1
    nr = NoRoom(requested=5, configured=5, role_cap=None, task_cap=None, binding=None)
    with pytest.raises(ValidationError):
        nr.requested = 1


def test_a_task_cap_alone_still_caps_and_is_labelled_task() -> None:
    d = decide_cap(1000, None, 300, [], 10, price())
    assert isinstance(d, OutputCap)
    assert d.effective == 300 and d.binding is CapBinding.TASK
    assert d.role_cap is None and d.task_cap == 300
    b = decide_cap(1000, None, 300, [balance("b", "1")], 10, price())
    assert isinstance(b, OutputCap) and b.effective == 300 and b.binding is CapBinding.TASK


@pytest.mark.parametrize(
    ("args", "name"),
    [((0, None, None), "requested"), ((10, 0, None), "role_cap"), ((10, None, 0), "task_cap")],
)
def test_each_bad_value_is_named_in_the_refusal(args: Any, name: str) -> None:
    with pytest.raises(ValueError, match=f"^{name} must be an integer of 1 or more$"):
        decide_cap(args[0], args[1], args[2], [], 1, price())


def test_one_token_is_a_valid_size_everywhere() -> None:
    d = decide_cap(1, 1, 1, [], 1, price())
    assert isinstance(d, OutputCap) and d.effective == 1 and d.lowered is False
    e = decide_cap(10, 1, None, [], 1, price())
    assert isinstance(e, OutputCap) and e.effective == 1
    f = decide_cap(10, None, 1, [], 1, price())
    assert isinstance(f, OutputCap) and f.effective == 1


def test_a_foreign_currency_budget_is_refused_even_when_blocked() -> None:
    eur = Money.model_construct(amount=Decimal("1"), currency="EUR")
    foreign = BudgetBalance.model_construct(
        budget=Budget.model_construct(id="b", limit=eur),
        spent=Money.of("0"),
        reserved=Money.of("0"),
        blocked=True,
    )
    with pytest.raises(CurrencyMismatchError):
        decide_cap(1000, 500, None, [foreign], 100, price())


def test_exactly_one_output_token_of_room_is_a_decision_not_no_room() -> None:
    # 100 prompt tokens cost 200 micro-units; 210 leaves 10, exactly one output token at rate 10
    d = decide_cap(1000, 1000, None, [balance("b", "0.00021")], 100, price())
    assert isinstance(d, OutputCap)
    assert d.effective == 1 and d.budget_cap == 1 and d.binding is CapBinding.BUDGET
    assert d.limiting_budget_id == "b" and d.available == Money.of("0.00021")
    n = decide_cap(1000, 1000, None, [balance("b", "0.000209")], 100, price())
    assert isinstance(n, NoRoom) and n.configured == 1000 and n.binding is None


def test_an_output_rate_of_one_still_divides() -> None:
    d = decide_cap(1000, 1000, None, [balance("b", "0.0003")], 100, price(out="1"))
    assert isinstance(d, OutputCap) and d.effective == 100 and d.budget_cap == 100


def test_a_zero_output_rate_with_too_little_money_is_no_room() -> None:
    free_out = price().model_copy(update={"output_per_million": Decimal(0)})
    r = decide_cap(1000, 500, None, [balance("b", "0.0001")], 100, free_out)
    assert isinstance(r, NoRoom)
    assert r.configured == 500 and r.binding is CapBinding.ROLE and r.role_cap == 500


def test_prompt_cost_uses_the_highest_input_side_rate() -> None:
    # available 5200 micro-units, 100 prompt tokens, output rate 10
    w = decide_cap(1000, 1000, None, [balance("b", "0.0052")], 100, price("2", "10", "3", "7"))
    c = decide_cap(1000, 1000, None, [balance("b", "0.0052")], 100, price("2", "10", "9", "7"))
    i = decide_cap(1000, 1000, None, [balance("b", "0.0052")], 100, price("8", "10", "3", "7"))
    assert isinstance(w, OutputCap) and w.effective == 450
    assert isinstance(c, OutputCap) and c.effective == 430
    assert isinstance(i, OutputCap) and i.effective == 440


def test_no_room_when_the_prompt_alone_costs_more_than_is_available() -> None:
    r = decide_cap(1000, 1000, None, [balance("b", "0.0001")], 100, price())
    assert isinstance(r, NoRoom) and r.configured == 1000 and r.requested == 1000
    assert r.lowered is False


def test_a_fractional_remainder_below_one_micro_unit_still_buys_a_token() -> None:
    # available 0.9 micro-units, 1 prompt token at 0.1, output 0.5: n = 0.8, 0.8 // 0.5 = 1
    d = decide_cap(1000, 1000, None, [balance("b", "0.0000009")], 1, price("0.1", "0.5"))
    assert isinstance(d, OutputCap)
    assert d.effective == 1 and d.binding is CapBinding.BUDGET
