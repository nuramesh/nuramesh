"""MONTH-window pins for core/budgets.py: the year rollover and the first-of-month start."""

from datetime import UTC, datetime

from nuramesh.core.budgets import (
    Budget,
    BudgetBalance,
    BudgetScope,
    BudgetWindow,
    roll,
    window_end,
    window_start,
)
from nuramesh.core.money import Money

MONTH = BudgetWindow.MONTH


def utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def month_budget() -> Budget:
    return Budget(
        id="m",
        org_id="acme",
        scope=BudgetScope.ORG,
        scope_id="acme",
        limit=Money.of("10"),
        window=MONTH,
    )


def test_month_window_start_is_the_first_of_the_month_at_midnight() -> None:
    assert window_start(MONTH, datetime(2026, 10, 17, 13, 45, 9, 123, tzinfo=UTC)) == utc(
        2026, 10, 1
    )
    assert window_start(MONTH, utc(2026, 10, 1)) == utc(2026, 10, 1)


def test_month_window_end_rolls_december_into_january_of_the_next_year() -> None:
    assert window_end(MONTH, utc(2026, 12, 1)) == utc(2027, 1, 1)
    assert window_end(MONTH, utc(2026, 10, 1)) == utc(2026, 11, 1)


def test_rolling_out_of_a_december_window_closes_it_at_the_first_of_january() -> None:
    balance = BudgetBalance.fresh(month_budget(), utc(2026, 12, 20, 5))
    assert balance.window_start == utc(2026, 12, 1)
    rolled = roll(balance, utc(2027, 1, 3, 2))
    assert rolled is not None
    assert rolled.closed_start == utc(2026, 12, 1)
    assert rolled.closed_end == utc(2027, 1, 1)
    assert rolled.balance.window_start == utc(2027, 1, 1)
    assert roll(balance, utc(2026, 12, 31, 23)) is None
