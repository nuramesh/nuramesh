"""The shipped default price list (#1450).

Prices are versioned data: `nuramesh/data/default_prices.json` ships in the package and is read
here, never by `nuramesh.core` (which does no I/O). Rows are append-only (a change is a new row
with a later `effective_from`), rates are decimal text, no rate is zero, and nothing is fetched
from a network. An invalid file fails closed: `ShippedPriceError` carries one fixed error text.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from typing import Any

from pydantic import ValidationError

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.prices import (
    E_OPERATOR_SOURCE,
    Price,
    PriceBasis,
    PriceBook,
    PriceOverride,
    ResolvedPrice,
    UnknownPriceError,
    operator_source_problem,
    resolve_price,
)

FORMAT = 1
ALLOWED_PROVIDERS = frozenset({"anthropic"})
STALE_AFTER_DAYS = 90

E_DECIMAL = "rates must be decimal text"
E_ZERO = "zero price"
E_VERSION = "list_version mismatch"
E_DUPLICATE = "duplicate row"
E_BACKDATED = "back-dated row"
E_MISSING = "missing field"
E_UNKNOWN = "unknown field"
E_FORMAT = "unsupported format"
E_PROVIDER = "provider not allowed"
E_INVALID = "invalid row"
E_SOURCE = E_OPERATOR_SOURCE

_REQUIRED = (
    "provider",
    "model",
    "currency",
    "input_per_million",
    "output_per_million",
    "effective_from",
    "retrieved_on",
    "source",
)
_OPTIONAL = ("source_url", "cached_input_per_million", "cache_write_per_million")
_RATES = ("input_per_million", "output_per_million")
_OPTIONAL_RATES = ("cached_input_per_million", "cache_write_per_million")
_TOP = frozenset({"format", "list_version", "prices"})


class ShippedPriceError(ValueError):
    """The shipped list (or a row of it) is invalid; the message is one of the fixed texts."""


class _JsonNumber:
    """A JSON number, kept apart from text so rates given as numbers can be refused."""

    def __init__(self, text: str) -> None:
        self.text = text


@dataclass(frozen=True)
class ShippedRow:
    """One row of the file: the price plus what the file says about where it came from."""

    price: Price
    retrieved_on: date
    source_url: str | None = None


@dataclass(frozen=True)
class ShippedList:
    """A loaded, validated list."""

    list_version: str
    rows: tuple[ShippedRow, ...]

    @property
    def prices(self) -> tuple[Price, ...]:
        return tuple(r.price for r in self.rows)

    def row_of(self, price: Price) -> ShippedRow | None:
        return next((r for r in self.rows if r.price is price), None)

    def age_days(self, provider: str, model: str, today: date) -> int | None:
        """Days from the newest row's `retrieved_on` to `today`; None when the key has no row."""
        mine = [r for r in self.rows if r.price.provider == provider and r.price.model == model]
        if not mine:
            return None
        newest = max(mine, key=lambda r: r.price.effective_from)
        return (today - newest.retrieved_on).days

    def stale(self, provider: str, model: str, today: date) -> bool:
        """More than 90 days old: day 90 is fresh, day 91 is stale."""
        age = self.age_days(provider, model, today)
        return age is not None and age > STALE_AFTER_DAYS

    def apply(self, book: PriceBook) -> PriceBook:
        """`book` with the shipped prices attached (they never enter `book.prices`)."""
        return book.with_shipped(self.prices)


def _no_numbers() -> dict[str, Any]:
    return {"parse_int": _JsonNumber, "parse_float": _JsonNumber}


def _check_no_number_rates(rows: list[Any]) -> None:
    for row in rows:
        if isinstance(row, dict):
            for name in (*_RATES, *_OPTIONAL_RATES):
                if isinstance(row.get(name), _JsonNumber):
                    raise ShippedPriceError(E_DECIMAL)


# Canonical decimal text only: no sign, space, underscore, exponent, unicode digit or leading zero.
_PLAIN_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")


def _row(raw: Any) -> ShippedRow:
    if not isinstance(raw, dict):
        raise ShippedPriceError(E_MISSING)
    if any(name not in raw for name in _REQUIRED):
        raise ShippedPriceError(E_MISSING)
    if any(name not in _REQUIRED and name not in _OPTIONAL for name in raw):
        raise ShippedPriceError(E_UNKNOWN)
    if not isinstance(raw["provider"], str) or raw["provider"] not in ALLOWED_PROVIDERS:
        raise ShippedPriceError(E_PROVIDER)
    rates: dict[str, Decimal] = {}
    for name in (*_RATES, *_OPTIONAL_RATES):
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, str):
            raise ShippedPriceError(E_DECIMAL)
        try:
            rates[name] = Decimal(value)
        except InvalidOperation:
            raise ShippedPriceError(E_DECIMAL) from None
        if _PLAIN_DECIMAL.fullmatch(value) is None:
            raise ShippedPriceError(E_INVALID)
    if any(rate.is_finite() and rate == 0 for rate in rates.values()):
        raise ShippedPriceError(E_ZERO)
    try:
        retrieved = date.fromisoformat(raw["retrieved_on"])
        moment = datetime.fromisoformat(raw["effective_from"])
        price = Price.model_validate(
            {
                "provider": raw["provider"],
                "model": raw["model"],
                "currency": raw["currency"],
                "effective_from": moment,
                "source": raw["source"],
                **rates,
            }
        )
        url = raw.get("source_url")
        if url is not None and not isinstance(url, str):
            raise ValueError("source_url must be text")
    except (ValueError, TypeError, ValidationError):
        raise ShippedPriceError(E_INVALID) from None
    return ShippedRow(price=price, retrieved_on=retrieved, source_url=url)


def _order_checks(rows: list[ShippedRow]) -> None:
    """Duplicate and back-dated rows, checked in file order (duplicate first)."""
    for i, row in enumerate(rows):
        key = (row.price.provider, row.price.model)
        earlier_same = [r for r in rows[:i] if (r.price.provider, r.price.model) == key]
        if any(r.price.effective_from == row.price.effective_from for r in earlier_same):
            raise ShippedPriceError(E_DUPLICATE)
        mine = [
            (r.retrieved_on, j)
            for j, r in enumerate(rows)
            if (r.price.provider, r.price.model) == key
        ]
        before = [rows[j] for retrieved, j in mine if (retrieved, j) < (row.retrieved_on, i)]
        if any(r.price.effective_from >= row.price.effective_from for r in before):
            raise ShippedPriceError(E_BACKDATED)


def parse_shipped(text: str) -> ShippedList:
    """Parse and validate a shipped list; raise `ShippedPriceError` with a fixed text on the first
    failing check (spec 3: parse, format, then per row in file order, then list_version)."""
    try:
        data = json.loads(text, **_no_numbers())
    except ValueError:
        raise ShippedPriceError(E_INVALID) from None
    if not isinstance(data, dict):
        raise ShippedPriceError(E_INVALID)
    raw_rows = data.get("prices")
    if isinstance(raw_rows, list):
        _check_no_number_rates(raw_rows)
    fmt = data.get("format")
    if not isinstance(fmt, _JsonNumber) or fmt.text != str(FORMAT):
        raise ShippedPriceError(E_FORMAT)
    if any(name not in _TOP for name in data):
        raise ShippedPriceError(E_UNKNOWN)
    if not isinstance(raw_rows, list) or not isinstance(data.get("list_version"), str):
        raise ShippedPriceError(E_MISSING)
    rows: list[ShippedRow] = []
    for raw in raw_rows:
        rows.append(_row(raw))
        _order_checks(rows)
    newest = max((r.retrieved_on for r in rows), default=None)
    if newest is None or data["list_version"] != newest.isoformat():
        raise ShippedPriceError(E_VERSION)
    return ShippedList(list_version=data["list_version"], rows=tuple(rows))


def check_baseline(baseline: ShippedList, new: ShippedList) -> bool:
    """True when every baseline row is still in `new`, unchanged (rows are append-only)."""
    return all(any(b == r for r in new.rows) for b in baseline.rows)


@cache
def load_shipped() -> ShippedList:
    """The list that ships in the package. A missing or invalid file raises `ShippedPriceError`
    (the caller refuses to start; it never runs with no rows)."""
    try:
        text = resources.files("nuramesh").joinpath("data/default_prices.json").read_text("utf-8")
    except OSError:
        raise ShippedPriceError(E_MISSING) from None
    return parse_shipped(text)


def today_utc() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class PriceFacts:
    """What the operator is told about one model's price at one moment (spec default-prices 6).
    `origin` is `shipped`, `operator`, `override` or `none`; `status` is `known`, `provisional` or
    `none`; `age_days` is the newest shipped row's age and is set only for a shipped price."""

    origin: str
    status: str
    age_days: int | None
    resolved: ResolvedPrice | None
    retrieved_on: date | None = None
    stale: bool = False


def price_facts(
    book: PriceBook,
    shipped: ShippedList,
    overrides: tuple[PriceOverride, ...],
    provider: str,
    model: str,
    at: datetime,
    endpoint_id: str | None = None,
) -> PriceFacts:
    """Resolve the price as a call at `at` would, and say where it comes from. An unknown price
    is `none`: it is never guessed."""
    try:
        resolved = resolve_price(
            book, provider, model, at, overrides=overrides, endpoint_id=endpoint_id
        )
    except (UnknownPriceError, ValueError):
        return PriceFacts("none", "none", None, None)
    if resolved.basis is not PriceBasis.KNOWN or resolved.price is None:
        return PriceFacts("override", "provisional", None, resolved)
    if book.origin_of(resolved.price) != "shipped":
        return PriceFacts("operator", "known", None, resolved)
    today = at.astimezone(UTC).date()
    row = next((r for r in shipped.rows if r.price == resolved.price), None)
    return PriceFacts(
        "shipped",
        "known",
        shipped.age_days(provider, model, today),
        resolved,
        None if row is None else row.retrieved_on,
        shipped.stale(provider, model, today),
    )


def price_warnings(
    config: NuraMeshConfig,
    shipped: ShippedList,
    today: date,
    folded: PriceBook | None = None,
) -> list[str]:
    """The config-check warnings (spec 6.3), never errors: an override for a model that already
    has a known price, and a shipped key whose newest row is stale. The stale warning is hidden
    only from the organization's earliest own price of the key (T0) on. `folded` is the folded book
    (configuration plus the ledger's `models price` versions, spec 5.2/6.3); default: the
    configuration's prices."""
    now = datetime.combine(today, time(), UTC)
    own_book = config.prices if folded is None else folded
    book = shipped.apply(own_book)
    out: list[str] = []
    for o in config.price_overrides:
        if book.lookup(o.provider, o.model, now, o.endpoint_id) is not None:
            out.append(f"override ignored: {o.provider}/{o.model} already has a known price")
    keys = sorted({(r.price.provider, r.price.model) for r in shipped.rows})
    for provider, model in keys:
        if not shipped.stale(provider, model, today):
            continue
        own = [
            p.effective_from for p in own_book.prices if (p.provider, p.model) == (provider, model)
        ]
        if own and now >= min(own):
            continue
        age = shipped.age_days(provider, model, today)
        out.append(f"shipped price list is {age} days old: {provider}/{model}")
    return out


__all__ = [
    "PriceFacts",
    "price_facts",
    "price_warnings",
    "ShippedList",
    "ShippedPriceError",
    "ShippedRow",
    "check_baseline",
    "load_shipped",
    "operator_source_problem",
    "parse_shipped",
    "today_utc",
]
