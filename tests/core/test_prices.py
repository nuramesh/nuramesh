from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from content_helpers import keyed
from pydantic import ValidationError

from nuramesh.core.money import Money
from nuramesh.core.prices import (
    Price,
    PriceBasis,
    PriceBook,
    PriceOverride,
    TokenUsage,
    UnknownPriceError,
    cost_of,
    resolve_price,
    worst_case_cost,
)

JAN = datetime(2026, 1, 1, tzinfo=UTC)
JUN = datetime(2026, 6, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 20, tzinfo=UTC)


def price(**overrides: Any) -> Price:
    data: dict[str, Any] = {
        "provider": "acme",
        "model": "m-1",
        "input_per_million": "3",
        "output_per_million": "15",
        "effective_from": JAN,
        "source": "https://acme.example/pricing (2026-01-01)",
    }
    data.update(overrides)
    return Price.model_validate(data)


def resolved(**overrides: Any) -> Any:
    return resolve_price(PriceBook(prices=(price(**overrides),)), "acme", "m-1", OCT)


def test_basic_cost_is_exact() -> None:
    # 1,200 × 3 / 1M = 0.0036; 300 × 15 / 1M = 0.0045; total 0.0081
    cost = cost_of(TokenUsage(input_tokens=1200, output_tokens=300), resolved())
    assert cost == Money.of("0.0081")


def test_cache_components() -> None:
    # 1,000 × 3 = 0.003; 50,000 × 0.30 = 0.015; 2,000 × 3.75 = 0.0075; 500 × 15 = 0.0075 → 0.033
    p = resolved(cached_input_per_million="0.30", cache_write_per_million="3.75")
    usage = TokenUsage(
        input_tokens=1000, cached_input_tokens=50000, cache_write_tokens=2000, output_tokens=500
    )
    assert cost_of(usage, p).amount == Decimal("0.033")


def test_tiny_amounts_are_not_rounded() -> None:
    # 1 token × 0.075 / 1M = 0.000000075
    p = resolved(input_per_million="0.075")
    assert cost_of(TokenUsage(input_tokens=1), p).amount == Decimal("0.000000075")


def test_huge_usage_is_exact() -> None:
    # 10^12 tokens × 123.456789 / 1M = 123,456,789
    p = resolved(input_per_million="123.456789")
    assert cost_of(TokenUsage(input_tokens=10**12), p).amount == Decimal("123456789")


def test_unpriced_component_is_unknown_not_guessed() -> None:
    with pytest.raises(UnknownPriceError, match="cached input"):
        cost_of(TokenUsage(input_tokens=10, cached_input_tokens=5), resolved())
    with pytest.raises(UnknownPriceError, match="cache write"):
        cost_of(TokenUsage(cache_write_tokens=1), resolved())
    assert cost_of(TokenUsage(), resolved()) == Money.zero()


def test_lookup_uses_the_price_in_effect() -> None:
    book = PriceBook(prices=(price(), price(input_per_million="2", effective_from=JUN)))
    assert book.lookup("acme", "m-1", datetime(2026, 5, 31, tzinfo=UTC)).input_per_million == 3  # type: ignore[union-attr]
    assert book.lookup("acme", "m-1", JUN).input_per_million == 2  # type: ignore[union-attr]
    assert book.lookup("acme", "m-1", datetime(2025, 12, 31, tzinfo=UTC)) is None
    assert book.lookup("acme", "other", OCT) is None


def test_duplicate_price_versions_are_refused() -> None:
    with pytest.raises(ValidationError, match="two prices"):
        PriceBook(prices=(price(), price(input_per_million="9")))


def override(**overrides: Any) -> PriceOverride:
    data: dict[str, Any] = {
        "provider": "acme",
        "model": "new-model",
        "input_per_million": "1.10",
        "output_per_million": "4.40",
        "set_by": "human:owner",
        "set_at": OCT,
        "reason": "Launch-week model, price not published yet",
    }
    data.update(overrides)
    return keyed(PriceOverride.model_validate(data))


def test_unknown_price_blocks_unless_overridden() -> None:
    book = PriceBook(prices=(price(),))
    with pytest.raises(UnknownPriceError, match="blocked"):
        resolve_price(book, "acme", "new-model", OCT)
    r = resolve_price(book, "acme", "new-model", OCT, overrides=(override(),))
    assert r.basis is PriceBasis.PROVISIONAL and r.override is not None
    # 1,000,000 × 1.10 / 1M + 250,000 × 4.40 / 1M = 1.10 + 1.10 = 2.20
    assert cost_of(TokenUsage(input_tokens=1_000_000, output_tokens=250_000), r).amount == Decimal(
        "2.20"
    )
    payload = r.ledger_payload()
    assert payload["override_set_by"] == "human:owner"
    assert payload["basis"] == "provisional"


def test_known_price_wins_over_an_override() -> None:
    book = PriceBook(prices=(price(),))
    r = resolve_price(book, "acme", "m-1", OCT, overrides=(override(model="m-1"),))
    assert r.basis is PriceBasis.KNOWN and r.input_per_million == 3


def test_override_needs_both_rates_and_a_human() -> None:
    for bad in (
        {"input_per_million": None},
        {"output_per_million": None},
        {"set_by": "agent:dev-1"},
        {"set_by": "system"},
        {"reason": ""},
        {"input_per_million": 0.5},
        {"input_per_million": "-1"},
        {"set_at": datetime(2026, 1, 1)},
    ):
        with pytest.raises(ValidationError):
            override(**bad)
    with pytest.raises(ValueError, match="more than one override"):
        resolve_price(PriceBook(), "acme", "new-model", OCT, overrides=(override(), override()))


def test_worst_case_uses_highest_input_rate_and_max_output() -> None:
    # 10,000 × 3.75 / 1M = 0.0375; 4,096 × 15 / 1M = 0.06144 → 0.09894
    p = resolved(cached_input_per_million="0.30", cache_write_per_million="3.75")
    assert worst_case_cost(10_000, 4_096, p).amount == Decimal("0.09894")
    # without cache rates: 10,000 × 3 / 1M = 0.03 + 0.06144 = 0.09144
    assert worst_case_cost(10_000, 4_096, resolved()).amount == Decimal("0.09144")
    with pytest.raises(ValueError):
        worst_case_cost(-1, 0, resolved())


@pytest.mark.parametrize(
    "split", [(10_000, 0, 0), (0, 10_000, 0), (0, 0, 10_000), (4000, 3000, 3000)]
)
def test_worst_case_is_an_upper_bound_for_any_prompt_split(split: tuple[int, int, int]) -> None:
    p = resolved(cached_input_per_million="0.30", cache_write_per_million="3.75")
    fresh, cached, written = split
    actual = cost_of(
        TokenUsage(
            input_tokens=fresh,
            cached_input_tokens=cached,
            cache_write_tokens=written,
            output_tokens=4096,
        ),
        p,
    )
    assert actual <= worst_case_cost(10_000, 4_096, p)


@pytest.mark.parametrize(
    "bad",
    [
        {"input_per_million": 0.1},
        {"output_per_million": True},
        {"input_per_million": "-0.01"},
        {"input_per_million": "NaN"},
        {"effective_from": datetime(2026, 1, 1)},
        {"source": ""},
        {"model": "has space"},
        {"provider": "Acme"},
    ],
)
def test_price_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        price(**bad)


def test_negative_token_counts_are_refused() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1)


def test_ledger_payload_keeps_exact_decimals_and_source() -> None:
    from nuramesh.core.canonical import canonical_json

    payload = resolved(input_per_million="0.0000001").ledger_payload()
    assert payload["source"].startswith("https://acme.example")
    assert b'"input_per_million":"0.0000001"' in canonical_json(payload)


@pytest.mark.parametrize(
    "rate",
    ["1000000001", "1E-19", "1.00000000000000000000001", "0.0000000000000000001"],
)
def test_rate_bounds(rate: str) -> None:
    with pytest.raises(ValidationError):
        price(input_per_million=rate)


def test_largest_tokens_times_largest_rate_is_exact() -> None:
    # 10^12 × 999,999,999.999999999999999 (24 digits) / 1M, exact
    big = "999999999.999999999999999"
    p = resolved(input_per_million=big, output_per_million=big)
    cost = cost_of(TokenUsage(input_tokens=10**12, output_tokens=10**12), p)
    assert cost.amount == Decimal(big) * 2 * 10**6
    tiny = resolved(input_per_million="1E-18")
    assert cost_of(TokenUsage(input_tokens=1), tiny).amount == Decimal("1E-24")


@pytest.mark.parametrize("zero", ["-0.00", "0E-40", "-0E-100", "0." + "0" * 34])
def test_every_zero_rate_is_plain_zero_and_costs_work(zero: str) -> None:
    p = price(input_per_million=zero)
    assert str(p.input_per_million) == "0"
    r = resolve_price(PriceBook(prices=(p,)), "acme", "m-1", OCT)
    assert cost_of(TokenUsage(input_tokens=10**12), r) == Money.zero()
    assert worst_case_cost(10**12, 0, r) == Money.zero()


def test_naive_times_are_refused() -> None:
    book = PriceBook(prices=(price(),))
    with pytest.raises(ValueError, match="timezone"):
        book.lookup("acme", "m-1", datetime(2026, 10, 20))
    with pytest.raises(ValueError, match="timezone"):
        resolve_price(book, "acme", "m-1", datetime(2026, 10, 20))


def test_override_applies_only_from_set_at_and_before_a_known_price() -> None:
    later = PriceBook(prices=(price(model="new-model", effective_from=JUN),))
    early_call = datetime(2026, 3, 1, tzinfo=UTC)
    ov = override(set_at=JAN)
    # before the model's first known price: the override applies
    assert resolve_price(later, "acme", "new-model", early_call, overrides=(ov,)).basis is (
        PriceBasis.PROVISIONAL
    )
    # after it: the known price wins
    assert resolve_price(later, "acme", "new-model", OCT, overrides=(ov,)).basis is PriceBasis.KNOWN
    # an override set after the call time doesn't apply
    with pytest.raises(UnknownPriceError):
        resolve_price(PriceBook(), "acme", "new-model", early_call, overrides=(override(),))


def test_override_with_cached_usage_is_unknown() -> None:
    r = resolve_price(PriceBook(), "acme", "new-model", OCT, overrides=(override(),))
    with pytest.raises(UnknownPriceError, match="cached input"):
        cost_of(TokenUsage(input_tokens=10, cached_input_tokens=10), r)


def test_cost_basis_mapping_and_payload_time() -> None:
    from nuramesh.core.ledger import CostBasis

    assert resolved().cost_basis is CostBasis.ACTUAL
    r = resolve_price(PriceBook(), "acme", "new-model", OCT, overrides=(override(),))
    assert r.cost_basis is CostBasis.PROVISIONAL
    assert r.ledger_payload()["at"] == OCT.isoformat()


@pytest.mark.parametrize("bad", [True, 10**12 + 1, -1, 1.5])
def test_worst_case_token_count_validation(bad: object) -> None:
    with pytest.raises(ValueError):
        worst_case_cost(bad, 0, resolved())  # type: ignore[arg-type]


def test_the_ledger_gets_a_keyed_hash_of_the_override_reason_and_never_an_unkeyed_override() -> (
    None
):
    from nuramesh.core.content import ContentKind, keyed_hash

    r = resolve_price(PriceBook(), "acme", "new-model", OCT, overrides=(override(),))
    payload = r.ledger_payload()
    expected = keyed_hash(
        b"\x07" * 32, "acme", ContentKind.REASON, "Launch-week model, price not published yet"
    )
    assert payload["override_reason_hash"] == expected
    assert "Launch-week" not in str(payload)
    unkeyed = PriceOverride.model_validate(override().model_dump())  # a fresh copy has no key
    r2 = resolve_price(PriceBook(), "acme", "new-model", OCT, overrides=(unkeyed,))
    with pytest.raises(ValueError, match="keyed"):
        r2.ledger_payload()


def test_a_reason_hash_must_be_a_keyed_looking_hash_never_the_text() -> None:
    plain = PriceOverride.model_validate(override().model_dump())
    for bad in ("", "y", plain.reason, "A" * 64):
        with pytest.raises(ValueError, match="64-digit"):
            plain.with_reason_hash(bad)
