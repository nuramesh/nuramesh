from decimal import Decimal

import pytest
from pydantic import ValidationError

from nuramesh.core.money import Currency, CurrencyMismatchError, Money


def test_exact_arithmetic() -> None:
    a = Money.of("0.10")
    b = Money.of("0.20")
    assert (a + b).amount == Decimal("0.30")
    assert (b - a) == Money.of("0.1")
    assert sum([a, b, a]) == Money.of("0.40")
    assert -a == Money.of("-0.10")
    assert (a - b).is_negative


def test_tiny_amounts_keep_full_precision() -> None:
    per_token = Money.of("0.000000075")
    assert (per_token + per_token).amount == Decimal("0.000000150")


def test_comparisons() -> None:
    assert Money.of("1") < Money.of("1.01") <= Money.of("1.010")
    assert Money.of(2) > Money.zero() >= Money.of("0")


@pytest.mark.parametrize("bad", [0.1, True, "abc", "NaN", "Infinity", None])
def test_rejects_floats_bools_and_non_numbers(bad: object) -> None:
    with pytest.raises(ValidationError):
        Money(amount=bad)  # type: ignore[arg-type]


def test_rejects_unsupported_currency() -> None:
    with pytest.raises(ValidationError):
        Money.model_validate({"amount": "1", "currency": "CAD"})


def test_currency_is_always_stored_and_serialized_as_text() -> None:
    m = Money.of("1.50")
    assert m.currency is Currency.USD
    assert m.model_dump(mode="json") == {"amount": "1.50", "currency": "USD"}
    assert m.to_json() == {"amount": "1.50", "currency": "USD"}
    assert Money.model_validate_json('{"amount": "1.50", "currency": "USD"}') == m


def test_mixing_with_other_types_fails() -> None:
    with pytest.raises(TypeError):
        Money.of(1) + 1  # type: ignore[operator]


def test_currency_mismatch_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    usd = Money.of(1)
    other = Money.model_construct(amount=Decimal(1), currency="CAD")  # simulates a future currency
    same = Money.model_construct(amount=Decimal(1), currency="USD")  # plain str, not the enum
    assert (usd + same).amount == 2
    with pytest.raises(CurrencyMismatchError):
        usd + other
    with pytest.raises(CurrencyMismatchError):
        usd < other  # noqa: B015


def test_arithmetic_never_rounds_silently() -> None:
    from decimal import Inexact

    big = Money.of("1" * 60)
    with pytest.raises(Inexact):
        big + Money.of("0.1")
    assert (Money.of("1" * 30) + Money.of("0.1")).amount == Decimal("1" * 30 + ".1")


def test_sum_only_starts_from_integer_zero() -> None:
    with pytest.raises(TypeError):
        False + Money.of(1)


@pytest.mark.parametrize(
    ("given", "text"),
    [("1E-7", "0.0000001"), ("1E+3", "1000"), ("-0", "0"), ("-0.00", "0.00"), ("1.10", "1.10")],
)
def test_amount_text_is_plain_notation(given: str, text: str) -> None:
    m = Money.of(given)
    assert m.to_json()["amount"] == text
    assert m.model_dump(mode="json")["amount"] == text


@pytest.mark.parametrize("huge", ["1E+100000", "1" * 61, "1E-31"])
def test_amount_size_is_capped(huge: str) -> None:
    with pytest.raises(ValidationError):
        Money.of(huge)
