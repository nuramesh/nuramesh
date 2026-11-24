"""Pins for `nuramesh.core.token_view`: returned values and the unknown-stays-unknown branches."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nuramesh.core.costs import SpendCall
from nuramesh.core.token_view import (
    RecordedRates,
    TokenCall,
    TokenUsage,
    known_saving,
    round6_away,
    token_rows,
)

D = Decimal
ONE = D("1")
RATES = RecordedRates(input=D(3), cached_input=D("0.3"), cache_write=D("3.75"))


def _call(
    *,
    cost: Decimal | None = ONE,
    role: str | None = "dev",
    agent: str = "a1",
    provider: str = "p",
    endpoint_id: str | None = None,
    model: str = "m",
) -> SpendCall:
    return SpendCall(
        stream="s",
        seq=1,
        run="r",
        task=None,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        agent=agent,
        role=role,
        provider=provider,
        model=model,
        cost=cost,
        cost_basis=None,
        endpoint_id=endpoint_id,
    )


def _item(
    usage: TokenUsage | None,
    rates: RecordedRates | None = RATES,
    **kw: object,
) -> TokenCall:
    return TokenCall(_call(**kw), usage, rates)  # type: ignore[arg-type]


def test_round6_half_away_and_sign() -> None:
    assert round6_away(D("0.0000005")) == "0.000001"
    assert round6_away(D("-0.0000005")) == "-0.000001"
    assert round6_away(D("0.0000004")) == "0.000000"
    assert round6_away(D("-0.0000004")) == "0.000000"  # no negative zero
    assert round6_away(D("-1.5")) == "-1.500000"
    assert round6_away(D("12.3456785")) == "12.345679"
    assert round6_away(D(0)) == "0.000000"


def test_known_saving_amounts() -> None:
    both = _item(TokenUsage(0, 0, 1_000_000, 1_000_000))
    assert known_saving(both) == (D("2.7"), D("0.75"))
    assert known_saving(_item(TokenUsage(0, 0, 500_000, 0))) == (D("1.35"), D(0))
    assert known_saving(_item(TokenUsage(0, 0, 0, 2_000_000))) == (D(0), D("1.5"))


def test_known_saving_zero_when_nothing_cached_even_without_price() -> None:
    plain = TokenUsage(10, 5, 0, 0)
    assert known_saving(_item(plain, rates=None, cost=None)) == (D(0), D(0))


def test_known_saving_unknown_cases() -> None:
    cached = TokenUsage(1, 1, 10, 0)
    write = TokenUsage(1, 1, 0, 10)
    assert known_saving(_item(None)) is None
    assert known_saving(_item(cached, cost=None)) is None
    assert known_saving(_item(cached, rates=None)) is None
    no_input = RecordedRates(input=None, cached_input=D(1), cache_write=D(1))
    assert known_saving(_item(cached, rates=no_input)) is None
    no_cached = RecordedRates(input=D(3), cached_input=None, cache_write=D(4))
    assert known_saving(_item(cached, rates=no_cached)) is None
    no_write = RecordedRates(input=D(3), cached_input=D(1), cache_write=None)
    assert known_saving(_item(write, rates=no_write)) is None
    # a missing cache-write rate does not matter when nothing was written, and vice versa
    assert known_saving(_item(cached, rates=no_write)) is not None
    assert known_saving(_item(write, rates=no_cached)) is not None


def test_row_full_values() -> None:
    items = [
        _item(TokenUsage(100, 20, 300, 100), cost=D("2")),
        _item(TokenUsage(50, 5, 0, 0), cost=D("1")),
    ]
    (row,) = token_rows(items, {}, "none")
    assert row["group"] == "all"
    assert row["calls"] == 2
    assert row["unknown_calls"] == 0
    assert row["known_total"] == "3"
    assert row["complete"] is True
    assert row["input_tokens"] == 150
    assert row["output_tokens"] == 25
    assert row["cached_input_tokens"] == 300
    assert row["cache_write_tokens"] == 100
    assert row["calls_without_usage"] == 0
    assert row["cache_hit_rate"] == "0.545455"  # 300 / (150 + 300 + 100)
    assert row["cache_saving"] == "0.00081"
    assert row["cache_write_premium"] == "0.000075"
    assert row["net_cache_saving"] == "0.000735"
    assert row["unknown_saving_calls"] == 0
    assert row["saving_complete"] is True
    # 0.000735 / (3 + 0.000735)
    assert row["cache_saving_share"] == "0.000245"


def test_row_unknowns_are_counted_not_zeroed() -> None:
    items = [
        _item(TokenUsage(10, 1, 100, 0), cost=D("1")),
        _item(None, cost=None),
        _item(TokenUsage(10, 1, 100, 0), cost=None),
    ]
    (row,) = token_rows(items, {}, "none")
    assert row["calls"] == 3
    assert row["unknown_calls"] == 2
    assert row["known_total"] == "1"
    assert row["complete"] is False
    assert row["calls_without_usage"] == 1
    assert row["input_tokens"] == 20
    assert row["unknown_saving_calls"] == 2
    assert row["saving_complete"] is False
    assert row["cache_saving_share"] is None
    assert row["cache_saving"] == "0.00027"  # only the one known call


def test_hit_rate_none_without_prompt_tokens() -> None:
    (row,) = token_rows([_item(TokenUsage(0, 4, 0, 0))], {}, "none")
    assert row["cache_hit_rate"] is None
    (row,) = token_rows([_item(None)], {}, "none")
    assert row["cache_hit_rate"] is None
    assert row["calls_without_usage"] == 1


def test_share_none_when_denominator_zero_and_negative_share() -> None:
    (row,) = token_rows([_item(TokenUsage(0, 0, 0, 0), cost=D(0))], {}, "none")
    assert row["cache_saving_share"] is None
    # a write premium with no saving gives a negative net and a negative share
    (row,) = token_rows([_item(TokenUsage(0, 0, 0, 1_000_000), cost=D("10"))], {}, "none")
    assert row["net_cache_saving"] == "-0.75"
    assert row["cache_saving_share"] == "-0.081081"  # -0.75 / 9.25


def test_share_none_when_only_saving_incomplete() -> None:
    items = [_item(TokenUsage(1, 1, 5, 0), rates=None, cost=D("1"))]
    (row,) = token_rows(items, {}, "none")
    assert row["unknown_calls"] == 0
    assert row["unknown_saving_calls"] == 1
    assert row["cache_saving_share"] is None


def test_grouping_keys_and_order() -> None:
    u = TokenUsage(1, 1, 0, 0)
    items = [
        _item(u, role="zed", provider="b", model="x"),
        _item(u, role=None, agent="a1", provider="a", endpoint_id="e", model="y"),
        _item(u, role=None, agent="a2", provider="a", model="z"),
        _item(u, role="zed", provider="b", model="x"),
    ]
    roles = {"a1": "builder"}
    by_role = token_rows(items, roles, "role")
    assert [(r["group"], r["calls"]) for r in by_role] == [
        ("builder", 1),
        ("unknown", 1),
        ("zed", 2),
    ]
    by_provider = token_rows(items, roles, "provider")
    assert [(r["group"], r["calls"]) for r in by_provider] == [("a", 1), ("a/e", 1), ("b", 2)]
    by_model = token_rows(items, roles, "model")
    assert [r["group"] for r in by_model] == ["a/e/y", "a/z", "b/x"]
    assert [r["group"] for r in token_rows(items, roles, "anything")] == ["all"]
    assert token_rows([], roles, "role") == []


def test_prices_are_exact_not_rounded() -> None:
    odd = RecordedRates(input=D("0.0000001"), cached_input=D(0), cache_write=D("0.0000001"))
    (row,) = token_rows([_item(TokenUsage(0, 0, 3, 0), rates=odd)], {}, "none")
    assert row["cache_saving"] == "0.0000000000003"


def test_round6_exact_input_types() -> None:
    from fractions import Fraction

    assert round6_away(Fraction(1, 3)) == "0.333333"
    assert round6_away(Fraction(2, 3)) == "0.666667"


@pytest.mark.parametrize("cached", [1, 7])
def test_zero_cached_zero_write_branch_ignores_rates(cached: int) -> None:
    bad = RecordedRates(input=None, cached_input=None, cache_write=None)
    assert known_saving(_item(TokenUsage(5, 5, 0, 0), rates=bad)) == (D(0), D(0))


def test_share_with_negative_denominator_is_still_computed() -> None:
    # recorded cost below the write premium: net -0.75, denominator 0.5 - 0.75 = -0.25
    (row,) = token_rows([_item(TokenUsage(0, 0, 0, 1_000_000), cost=D("0.5"))], {}, "none")
    assert row["cache_saving_share"] == "3.000000"


def test_single_unknown_call_makes_row_incomplete() -> None:
    (row,) = token_rows([_item(TokenUsage(1, 1, 0, 0), cost=None)], {}, "none")
    assert row["unknown_calls"] == 1
    assert row["complete"] is False
    assert row["known_total"] == "0"
