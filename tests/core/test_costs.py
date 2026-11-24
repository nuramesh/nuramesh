"""The pure cost arithmetic (spec/costs section 2, 3.3, 5.4)."""

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from itertools import islice

import pytest

from nuramesh.core.costs import (
    Cell,
    Placed,
    Row,
    SpendCall,
    iter_periods,
    money_text,
    percentile,
    round6,
    series_periods,
    summarize,
)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Fraction(1, 3), "0.333333"),
        (Fraction(2, 3), "0.666667"),
        (Fraction(1, 2000000), "0.000001"),  # exactly half a micro rounds up
        (Fraction(1, 4000000), "0.000000"),  # below half
        (Fraction(3, 4000000), "0.000001"),  # above half
        (Fraction(5), "5.000000"),
        (Fraction(0), "0.000000"),
        (Fraction(1234567891, 10**9), "1.234568"),
    ],
)
def test_round6_is_half_up_with_six_places(value: Fraction, text: str) -> None:
    assert round6(value) == text


def test_round6_does_not_lose_precision_to_decimal_context() -> None:
    # 30+ significant digits: a Decimal division would round before we do
    assert round6(Fraction(10**40 + 5, 10**46)) == "0.000001"  # 1.0000..05e-6, above half
    assert round6(Fraction(5, 10**7) - Fraction(1, 10**40)) == "0.000000"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("0"), "0"),
        (Decimal("0.30"), "0.3"),
        (Decimal("1E+2"), "100"),
        (Decimal("0.000000001"), "0.000000001"),
        (Decimal("12.50"), "12.5"),
    ],
)
def test_money_text_is_plain_exact_decimal_text(value: Decimal, text: str) -> None:
    assert money_text(value) == text


def test_percentile_interpolates_linearly_and_exactly() -> None:
    xs = [Decimal(v) for v in ("1", "2", "3", "4", "5")]
    assert [percentile(xs, p) for p in (Fraction(1, 4), Fraction(1, 2), Fraction(3, 4))] == [
        Fraction(2),
        Fraction(3),
        Fraction(4),
    ]
    ys = [Decimal(v) for v in ("1", "2", "4", "8", "16", "32")]
    assert percentile(ys, Fraction(1, 4)) == Fraction(5, 2)  # h=1.25: 2 + 0.25 * (4 - 2)
    assert percentile([Decimal("7")], Fraction(3, 4)) == Fraction(7)


def test_series_periods_cut_at_both_ends() -> None:
    start = datetime(2026, 1, 30, 12, tzinfo=UTC)
    end = datetime(2026, 2, 2, 6, tzinfo=UTC)
    days = series_periods(start, end, "day")
    assert days[0] == (start, datetime(2026, 1, 31, tzinfo=UTC))
    assert days[-1] == (datetime(2026, 2, 2, tzinfo=UTC), end)
    assert len(days) == 4
    months = series_periods(start, end, "month")
    assert months == [
        (start, datetime(2026, 2, 1, tzinfo=UTC)),
        (datetime(2026, 2, 1, tzinfo=UTC), end),
    ]
    assert series_periods(start, end, "none") == [(start, end)]


# -- review fixes ------------------------------------------------------------------------------


def _call(cost: str | None) -> "SpendCall":
    return SpendCall(
        stream="run:r1",
        seq=1,
        run="r1",
        task=None,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        agent="a",
        role=None,
        provider="p",
        model="m",
        cost=None if cost is None else Decimal(cost),
        cost_basis="actual",
    )


def test_sums_and_money_text_are_exact_beyond_28_digits() -> None:
    tiny = "0.000000000000000000000000000001"  # 1e-30
    cell = Cell()
    cell.add(Placed(_call("1000"), "accepted", None, datetime(2026, 1, 1, tzinfo=UTC), True))
    cell.add(Placed(_call(tiny), "accepted", None, datetime(2026, 1, 1, tzinfo=UTC), True))
    assert cell.known == Decimal("1000.000000000000000000000000000001")
    assert money_text(cell.known) == "1000.000000000000000000000000000001"
    assert cell.retry_known == cell.known
    assert money_text(Decimal("1000.000000000000000000000000000001")) == (
        "1000.000000000000000000000000000001"
    )
    assert money_text(Decimal("1.5000000000000000000000000000000000")) == "1.5"


def test_a_row_total_is_exact_beyond_28_digits() -> None:
    row = Row()
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    row.add(Placed(_call("1000"), "accepted", None, moment, False), "config")
    row.add(
        Placed(_call("0.000000000000000000000000000001"), "pending", None, moment, False), "config"
    )
    assert row.total_known == Decimal("1000.000000000000000000000000000001")


def test_share_divides_exactly_not_in_decimal_context() -> None:
    summary = summarize(
        [
            Placed(_call("1"), "pending", None, datetime(2026, 1, 1, tzinfo=UTC), False),
        ],
        [],
        {},
        group_by="provider",
        start=None,
        end=None,
        share=True,
    )
    assert summary.rows[0]["share"] == "1.000000"


def test_series_periods_stop_at_a_limit_and_survive_year_9999() -> None:
    start = datetime(1, 1, 1, tzinfo=UTC)
    end = datetime(9999, 12, 31, tzinfo=UTC)
    assert len(list(islice(iter_periods(start, end, "day"), 1001))) == 1001  # stops, no 3M list
    last = series_periods(
        datetime(9999, 12, 30, tzinfo=UTC), datetime(9999, 12, 31, 12, tzinfo=UTC), "day"
    )
    assert last[-1][1] == datetime(9999, 12, 31, 12, tzinfo=UTC)
    month = series_periods(
        datetime(9999, 11, 1, tzinfo=UTC), datetime(9999, 12, 31, 12, tzinfo=UTC), "month"
    )
    assert len(month) == 2
