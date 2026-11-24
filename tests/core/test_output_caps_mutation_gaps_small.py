"""Mutation pins for core/output_caps.py (#1278): cap resolution, no-room results, rates.

Price used: input 2 and output 10 per million (the worst case of 100 prompt + 500 output tokens
is 0.0052).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.budgets import Budget, BudgetBalance, BudgetScope
from nuramesh.core.money import Money
from nuramesh.core.output_caps import (
    CapBinding,
    NoRoom,
    OutputCap,
    configured_size,
    decide_cap,
)
from nuramesh.core.prices import Price, PriceBook, resolve_price

PRICE = resolve_price(
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


def price(**over: Any) -> Any:
    return PRICE.model_construct(**{**PRICE.model_dump(), **over})


def bal(bid: str, limit: str, *, blocked: bool = False, spent: str = "0") -> BudgetBalance:
    return BudgetBalance(
        budget=Budget(id=bid, scope=BudgetScope.RUN, scope_id="r1", limit=Money.of(limit)),
        spent=Money.of(spent),
        reserved=Money.of("0"),
        blocked=blocked,
    )


def cap(*args: Any, **kw: Any) -> OutputCap:
    result = decide_cap(*args, **kw)
    assert isinstance(result, OutputCap)
    return result


def room(*args: Any, **kw: Any) -> NoRoom:
    result = decide_cap(*args, **kw)
    assert isinstance(result, NoRoom)
    return result


# ------------------------------------------------------------ resolution order and labels


def test_configured_size_is_the_minimum_of_those_present() -> None:
    assert configured_size(1000, None, None) == 1000
    assert configured_size(1000, 300, None) == 300
    assert configured_size(1000, None, 200) == 200
    assert configured_size(1000, 300, 200) == 200
    assert configured_size(1000, 200, 300) == 200
    assert configured_size(100, 300, 200) == 100


def test_no_budget_label_task_role_or_none() -> None:
    task = cap(1000, 300, 200, [], 100, PRICE)
    assert (task.effective, task.binding, task.role_cap, task.task_cap) == (
        200,
        CapBinding.TASK,
        300,
        200,
    )
    role = cap(1000, 200, 300, [], 100, PRICE)
    assert (role.effective, role.binding) == (200, CapBinding.ROLE)
    both = cap(1000, 200, 200, [], 100, PRICE)
    assert both.binding is CapBinding.TASK  # the task cap wins a tie
    assert cap(1000, None, 200, [], 100, PRICE).binding is CapBinding.TASK
    assert cap(1000, 200, None, [], 100, PRICE).binding is CapBinding.ROLE
    at_request = cap(1000, 1000, None, [], 100, PRICE)
    assert (at_request.effective, at_request.binding, at_request.lowered) == (1000, None, False)
    above = cap(1000, 5000, 7000, [], 100, PRICE)
    assert (above.effective, above.binding, above.lowered) == (1000, None, False)


def test_a_cap_below_the_request_marks_it_lowered_and_none_does_not() -> None:
    assert cap(1000, 999, None, [], 1, PRICE).lowered is True
    assert cap(1000, 1000, None, [], 1, PRICE).lowered is False


def test_no_caps_means_nothing_happens_even_with_blocked_budgets() -> None:
    d = cap(1000, None, None, [bal("b", "0", blocked=True)], 100, PRICE)
    assert d == OutputCap(
        requested=1000,
        effective=1000,
        role_cap=None,
        task_cap=None,
        budget_cap=None,
        binding=None,
        limiting_budget_id=None,
        available=None,
    )


def test_fits_without_budgets_carries_the_caps_and_no_budget_fields() -> None:
    d = cap(1000, 300, 400, [], 100, PRICE)
    assert d == OutputCap(
        requested=1000,
        effective=300,
        role_cap=300,
        task_cap=400,
        budget_cap=None,
        binding=CapBinding.ROLE,
        limiting_budget_id=None,
        available=None,
    )


@pytest.mark.parametrize("field", ["role_cap", "task_cap", "requested"])
def test_validation_names_the_bad_argument(field: str) -> None:
    args = {"requested": 10, "role_cap": 10, "task_cap": 10}
    args[field] = 0
    with pytest.raises(ValueError, match=f"^{field} must be an integer of 1 or more$"):
        decide_cap(args["requested"], args["role_cap"], args["task_cap"], [], 1, PRICE)
    args[field] = 1  # the lower limit is accepted
    assert cap(args["requested"], args["role_cap"], args["task_cap"], [], 1, PRICE).effective == 1


@pytest.mark.parametrize("bad", [True, 1.0, "5", -3])
def test_bad_caps_are_refused(bad: Any) -> None:
    with pytest.raises(ValueError):
        decide_cap(10, bad, None, [], 1, PRICE)
    with pytest.raises(ValueError):
        decide_cap(10, None, bad, [], 1, PRICE)


# ------------------------------------------------------------ the budget binds or not


def test_worst_case_equal_to_available_fits_and_one_unit_less_binds() -> None:
    fits = cap(1000, 500, None, [bal("b", "0.0052")], 100, PRICE)
    assert (fits.effective, fits.binding, fits.budget_cap) == (500, CapBinding.ROLE, None)
    assert fits.limiting_budget_id is None and fits.available is None
    binds = cap(1000, 500, None, [bal("b", "0.005199")], 100, PRICE)
    assert (binds.effective, binds.binding, binds.budget_cap) == (499, CapBinding.BUDGET, 499)
    assert binds.limiting_budget_id == "b"
    assert binds.available == Money.of("0.005199")
    assert binds.role_cap == 500 and binds.task_cap is None and binds.requested == 1000
    assert binds.lowered is True


def test_smallest_available_across_budgets_limits() -> None:
    d = cap(1000, 1000, None, [bal("a", "1"), bal("b", "0.0052"), bal("c", "0.5")], 100, PRICE)
    assert (d.limiting_budget_id, d.effective) == ("b", 500)
    assert d.available == Money.of("0.0052")


def test_spent_money_is_not_available() -> None:
    d = cap(1000, 1000, None, [bal("b", "1", spent="0.9948")], 100, PRICE)
    assert (d.limiting_budget_id, d.effective, d.available) == ("b", 500, Money.of("0.0052"))


def test_a_blocked_budget_counts_zero_among_others() -> None:
    r = room(1000, 400, None, [bal("a", "5"), bal("b", "5", blocked=True)], 100, PRICE)
    assert r.configured == 400 and r.binding is CapBinding.ROLE


# ------------------------------------------------------------ no room


def test_no_room_when_available_is_less_than_the_prompt() -> None:
    r = room(1000, 300, 200, [bal("b", "0.0001")], 100, PRICE)  # prompt alone costs 0.0002
    assert r == NoRoom(
        requested=1000, configured=200, role_cap=300, task_cap=200, binding=CapBinding.TASK
    )
    assert r.lowered is True


def test_no_room_when_available_buys_exactly_nothing() -> None:
    assert isinstance(decide_cap(1000, 500, None, [bal("b", "0.0002")], 100, PRICE), NoRoom)
    assert isinstance(decide_cap(1000, 500, None, [bal("b", "0.000209")], 100, PRICE), NoRoom)


def test_one_token_of_room_is_an_answer() -> None:
    d = cap(1000, 500, None, [bal("b", "0.00021")], 100, PRICE)
    assert (d.effective, d.budget_cap, d.binding) == (1, 1, CapBinding.BUDGET)
    d2 = cap(1000, 500, None, [bal("b", "0.000219")], 100, PRICE)
    assert d2.effective == 1  # rounds down
    assert cap(1000, 500, None, [bal("b", "0.00022")], 100, PRICE).effective == 2


def test_no_room_keeps_the_configured_size_and_not_lowered_when_caps_equal_the_request() -> None:
    r = room(500, 500, None, [bal("b", "0")], 100, PRICE)
    assert r == NoRoom(requested=500, configured=500, role_cap=500, task_cap=None, binding=None)
    assert r.lowered is False
    r2 = room(500, 100, None, [bal("b", "0")], 100, PRICE)
    assert (r2.configured, r2.binding, r2.lowered) == (100, CapBinding.ROLE, True)


def test_a_zero_output_rate_is_no_room_when_it_does_not_fit() -> None:
    free_out = price(output_per_million=Decimal(0))
    r = room(1000, 500, None, [bal("b", "0.0001")], 100, free_out)  # prompt costs 0.0002
    assert r.configured == 500 and r.binding is CapBinding.ROLE
    ok = cap(1000, 500, None, [bal("b", "0.0002")], 100, free_out)  # fits exactly
    assert ok.effective == 500 and ok.budget_cap is None


def test_no_room_for_zero_prompt_tokens_and_empty_budget() -> None:
    r = room(1000, 500, None, [bal("b", "0")], 0, PRICE)
    assert r.configured == 500
    assert cap(1000, 500, None, [bal("b", "0.00001")], 0, PRICE).effective == 1


# ------------------------------------------------------------ highest input-side rate


def test_cache_write_rate_prices_the_prompt_when_higher() -> None:
    p = price(cache_write_per_million=Decimal(4))
    d = cap(1000, 500, None, [bal("b", "0.0052")], 100, p)
    assert d.effective == (5200 - 100 * 4) // 10  # 480


def test_cached_input_rate_prices_the_prompt_when_higher() -> None:
    p = price(cached_input_per_million=Decimal(6))
    assert cap(1000, 500, None, [bal("b", "0.0052")], 100, p).effective == 460


def test_the_highest_of_all_input_rates_wins() -> None:
    both = price(cache_write_per_million=Decimal(4), cached_input_per_million=Decimal(6))
    assert cap(1000, 500, None, [bal("b", "0.0052")], 100, both).effective == 460
    both2 = price(cache_write_per_million=Decimal(6), cached_input_per_million=Decimal(4))
    assert cap(1000, 500, None, [bal("b", "0.0052")], 100, both2).effective == 460


def test_a_lower_cache_rate_never_lowers_the_prompt_price() -> None:
    low = price(cache_write_per_million=Decimal(1), cached_input_per_million=Decimal(1))
    assert cap(1000, 500, None, [bal("b", "0.0051")], 100, low).effective == 490


def test_fractional_rates_and_floor_division() -> None:
    p = price(output_per_million=Decimal("7"))
    # available 0.0052 -> 5200 - 200 = 5000 // 7 = 714
    assert cap(1000, 1000, None, [bal("b", "0.0052")], 100, p).effective == 714


def test_decisions_are_immutable() -> None:
    d = cap(1000, 500, None, [], 1, PRICE)
    with pytest.raises(ValidationError):
        d.effective = 1
    r = room(1000, 500, None, [bal("b", "0")], 100, PRICE)
    with pytest.raises(ValidationError):
        r.configured = 1


def test_a_tie_between_budgets_goes_to_the_lowest_id() -> None:
    d = cap(
        1000,
        1000,
        None,
        [bal("b2", "0.0052"), bal("b1", "0.0052"), bal("b3", "0.0052")],
        100,
        PRICE,
    )
    assert d.limiting_budget_id == "b1"


def test_an_output_rate_of_one_is_an_ordinary_rate() -> None:
    p = price(output_per_million=Decimal(1))
    # worst case 200 + 10000 > 5200 available: effective = (5200 - 200) // 1
    d = cap(10_000, 10_000, None, [bal("b", "0.0052")], 100, p)
    assert (d.effective, d.binding) == (5_000, CapBinding.BUDGET)
