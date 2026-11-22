"""Money: an exact `Decimal` amount with its currency (decision A5). Never float.

USD is the only accepted currency for now, but every amount carries its currency so others can be
added without a data migration. Arithmetic between different currencies is an error. Amounts keep
full precision (model prices are fractions of a cent): arithmetic that would need rounding raises
`decimal.Inexact` instead of rounding silently. Rounding happens only for display and at the
payments boundary.
"""

from decimal import Decimal, Inexact, Rounded, localcontext
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from nuramesh.core.canonical import decimal_text

MAX_DIGITS = 60
MAX_SCALE = 30


class Currency(StrEnum):
    """ISO 4217 codes accepted today."""

    USD = "USD"


class CurrencyMismatchError(ValueError):
    """Raised when combining amounts in different currencies."""


class Money(BaseModel):
    """An exact amount of money. Immutable; +, -, comparison and `sum` within one currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: Currency = Currency.USD

    @field_validator("amount", mode="before")
    @classmethod
    def _no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("money must not be a float; pass a Decimal, int or string")
        if isinstance(value, bool):
            raise ValueError("money must not be a boolean")
        return value

    @field_validator("amount")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("money must be a finite number")
        sign, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)  # finite
        if len(digits) > MAX_DIGITS or not -MAX_SCALE <= exponent <= MAX_SCALE:
            raise ValueError(f"money is limited to {MAX_DIGITS} digits and scale ±{MAX_SCALE}")
        return value

    @field_serializer("amount")
    def _amount_as_text(self, value: Decimal) -> str:
        return decimal_text(value)

    @classmethod
    def zero(cls, currency: Currency = Currency.USD) -> Self:
        """No money."""
        return cls(amount=Decimal(0), currency=currency)

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: Currency = Currency.USD) -> Self:
        """Shorthand: `Money.of("1.25")`."""
        return cls(amount=Decimal(amount), currency=currency)

    def _same(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError(f"can't combine Money with {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    @staticmethod
    def _exact(op: str, a: Decimal, b: Decimal) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = 60
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            return a + b if op == "+" else a - b

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(amount=self._exact("+", self.amount, other.amount), currency=self.currency)

    def __radd__(self, other: object) -> "Money":
        if type(other) is int and other == 0:  # lets sum() start from 0
            return self
        if isinstance(other, Money):
            return other + self
        return NotImplemented

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(amount=self._exact("-", self.amount, other.amount), currency=self.currency)

    def __neg__(self) -> "Money":
        return Money(amount=-self.amount, currency=self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount >= other.amount

    @property
    def is_negative(self) -> bool:
        """True below zero."""
        return self.amount < 0

    def to_json(self) -> dict[str, str]:
        """`{"amount": "1.25", "currency": "USD"}`; the form hashed into the ledger."""
        return {"amount": decimal_text(self.amount), "currency": str(self.currency)}

    def __str__(self) -> str:
        return f"{decimal_text(self.amount)} {self.currency}"
