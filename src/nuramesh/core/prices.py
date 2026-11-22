"""Model prices and cost metering (decisions A3, A3a, A5; PLAN E.3).

Prices are versioned data with a source, never hard-coded: a `PriceBook` holds every known price
and answers "what did this model cost at this moment?". Costs are exact: a token count times a
price per million tokens, shifted six decimal places, with no rounding.

**Every component a call uses must be priced.** A call that read cached input tokens needs a
cached-input price; if the price doesn't list one, the cost is unknown, never guessed.

**Unknown prices block the call** (A3). A config override may allow it, but only with a
provisional price per 1M input and output tokens (A3a), so budget hard stops still work; every use
is recorded with who set the override, when, and the values. An override applies only from its
`set_at` onwards, and only while the model has no known price at that moment.

Rates are bounded (at most `MAX_RATE` per 1M tokens, `MAX_RATE_DIGITS` significant digits, no
finer than 10^`MIN_RATE_EXPONENT`), so `MAX_TOKENS` × any rate is always exact and fits `Money`.

Worst-case estimates (for budget reservations before a call) assume the whole prompt is billed as
fresh input and the model writes its maximum output.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, Inexact, Rounded, localcontext
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    field_validator,
    model_validator,
)

from nuramesh.core.endpoints import FREE_SOURCE, is_loopback
from nuramesh.core.ledger import CostBasis
from nuramesh.core.money import Currency, Money
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId, Slug
from nuramesh.core.people import HUMAN_ID_PATTERN

PER_MILLION_EXPONENT = -6
MAX_TOKENS = 10**12
MAX_RATE = Decimal(10**9)
MAX_RATE_DIGITS = 24
MIN_RATE_EXPONENT = -18

ModelName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$", strict=True)
]
"""A provider's model identifier, for example `claude-sonnet-5-5` or `gpt-5.1-mini`."""

Tokens = Annotated[int, Field(ge=0, le=MAX_TOKENS)]


@contextmanager
def _exact() -> Iterator[None]:
    """Decimal arithmetic that raises instead of rounding."""
    with localcontext() as ctx:
        ctx.prec = 60
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        yield


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _rate(value: Decimal | None, name: str) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite number of zero or more")
    if value.is_zero():
        return Decimal(0)  # any zero (-0, 0E-40, 0.000…) is stored as plain 0
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    if value > MAX_RATE or len(digits) > MAX_RATE_DIGITS or exponent < MIN_RATE_EXPONENT:
        raise ValueError(
            f"{name} must be at most {MAX_RATE} per 1M tokens, with at most {MAX_RATE_DIGITS} "
            f"significant digits and no finer than 1E{MIN_RATE_EXPONENT}"
        )
    return value


SHIPPED_PREFIX = "shipped list;"
E_OPERATOR_SOURCE = f'source may not start with "{SHIPPED_PREFIX}"'


def operator_source_problem(source: str) -> str | None:
    """The refusal text for an operator price whose source claims to be the shipped list. Origin
    is decided by provenance, never by this text; the refusal applies to new input only."""
    return E_OPERATOR_SOURCE if source.strip().startswith(SHIPPED_PREFIX) else None


ENDPOINT_NOT_ALLOWED = "endpoint_id_not_allowed"
P_ZERO = (
    "openai-compatible: a zero price must be declared free for a local connection "
    '(source "declared free (local)" and a bound endpoint)'
)
P_BOUND_NONZERO = "openai-compatible: only a zero price has a bound endpoint"
P_LOOPBACK = (
    "openai-compatible: a free price is only allowed for an active local (loopback) connection"
)
P_BOUND_SUPPLIED = "openai-compatible: the bound endpoint is set by the service and cannot be given"
P_OVERRIDE = "openai-compatible: a price override cannot be zero"


def _endpoint_rule(provider: str, endpoint_id: str | None) -> None:
    if endpoint_id is not None and provider != OPENAI_COMPATIBLE:
        raise ValueError(ENDPOINT_NOT_ALLOWED)


def _aware_moment(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class Price(_Frozen):
    """What one model costs per 1M tokens, from `effective_from` until a newer price replaces it.

    Optional rates (`cached_input_per_million`, `cache_write_per_million`) are unknown when absent;
    a call that uses those token kinds then has an unknown cost.
    """

    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName
    currency: Currency = Currency.USD
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    effective_from: datetime
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    bound_endpoint: str | None = None
    """The normalised endpoint a zero price was declared for (spec/openai-compatible 12)."""
    bound_epoch: int | None = Field(default=None, ge=1)
    """The ledger sequence at which the connection took the facts (endpoint, auth, key name) a
    zero price was declared for. Any later change of those facts, even one that comes back to the
    old address, starts a new epoch and voids the price (spec openai-compatible 12.3)."""

    @model_validator(mode="after")
    def _free_price_rule(self) -> Self:
        """No silent zero (12.2): both main rates 0 is valid only as a declared-free price in USD
        bound to a literal loopback endpoint; a bound endpoint belongs to a zero price only."""
        _endpoint_rule(self.provider, self.endpoint_id)
        zero = self.input_per_million == 0 and self.output_per_million == 0
        if not zero:
            if self.bound_endpoint is not None or self.bound_epoch is not None:
                raise ValueError(P_BOUND_NONZERO)
            return self
        if (
            self.currency is not Currency.USD
            or self.source != FREE_SOURCE
            or self.bound_endpoint is None
        ):
            raise ValueError(P_ZERO)
        if not is_loopback(self.bound_endpoint):
            raise ValueError(P_LOOPBACK)
        return self

    @field_validator(
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
        "cache_write_per_million",
        mode="before",
    )
    @classmethod
    def _no_floats(cls, value: object) -> object:
        if isinstance(value, float | bool):
            raise ValueError("prices must be Decimal, int or text, never float")
        return value

    @field_validator(
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
        "cache_write_per_million",
    )
    @classmethod
    def _valid_rate(cls, value: Decimal | None) -> Decimal | None:
        return _rate(value, "a price")

    @field_validator("effective_from")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _aware_moment(value, "effective_from")


class TokenUsage(_Frozen):
    """Tokens one call used, as the provider reported them.

    `input_tokens` excludes cached reads and cache writes, which are counted separately.
    """

    input_tokens: Tokens = 0
    output_tokens: Tokens = 0
    cached_input_tokens: Tokens = 0
    cache_write_tokens: Tokens = 0


class UnknownPriceError(LookupError):
    """No price for the model (or for a token kind the call used): the call is blocked."""


class PriceBook(_Frozen):
    """Every known price. `lookup` returns the one in effect at a moment."""

    prices: tuple[Price, ...] = ()
    _blocked: Mapping[tuple[str, str | None, str], tuple[datetime, ...]] = PrivateAttr(
        default_factory=dict
    )
    _shipped: tuple[Price, ...] = PrivateAttr(default=())

    @property
    def blocked(self) -> frozenset[tuple[str, str | None, str]]:
        """The `(provider, endpoint_id, model)` that have a voided free price (spec
        openai-compatible 12.3); `blocked_at` says from when."""
        return frozenset(self._blocked)

    def blocked_since(
        self, provider: str, endpoint_id: str | None, model: str
    ) -> tuple[datetime, ...]:
        """The `effective_from` of each voided free price of this model."""
        return self._blocked.get((provider, endpoint_id, model), ())

    def blocked_at(self, provider: str, endpoint_id: str | None, model: str, at: datetime) -> bool:
        """Whether a voided free price of this model is in force at `at`."""
        return any(t <= at for t in self.blocked_since(provider, endpoint_id, model))

    @property
    def shipped(self) -> tuple[Price, ...]:
        """The shipped default prices (spec default-prices 5). They are not in `prices`: they
        apply to a key only before the organization's own earliest price of that key."""
        return self._shipped

    def with_shipped(self, rows: Iterable[Price]) -> "PriceBook":
        """A copy of this book that also holds the shipped default prices. The rows must have no
        endpoint; a book never holds two shipped rows of one key and `effective_from`."""
        shipped = tuple(rows)
        seen: set[tuple[str, str, datetime]] = set()
        for row in shipped:
            key = (row.provider, row.model, row.effective_from)
            if row.endpoint_id is not None or key in seen:
                raise ValueError("shipped rows have no endpoint and no duplicate version")
            seen.add(key)
        book = PriceBook(prices=self.prices)
        book._blocked = dict(self._blocked)
        book._shipped = shipped
        return book

    def origin_of(self, price: Price) -> str:
        """`shipped` for a row of the package file, else `operator`. Decided by provenance (the
        row object came from the shipped list), never by the text of its `source`."""
        return "shipped" if any(price is row for row in self._shipped) else "operator"

    def with_blocked(
        self, voided: Mapping[tuple[str, str | None, str], Iterable[datetime]]
    ) -> "PriceBook":
        """A copy of this book that also records the `effective_from` of each voided free price
        per model. A voided price governs from its own date, like any price: until then the price
        in force today is still used."""
        book = PriceBook(prices=self.prices)
        book._shipped = self._shipped
        merged = {k: tuple(v) for k, v in self._blocked.items()}
        for key, times in voided.items():
            merged[key] = (*merged.get(key, ()), *times)
        book._blocked = merged
        return book

    @model_validator(mode="after")
    def _no_duplicate_versions(self) -> Self:
        seen: set[tuple[str, str | None, str, datetime]] = set()
        for price in self.prices:
            key = (price.provider, price.endpoint_id, price.model, price.effective_from)
            if key in seen:
                raise ValueError(
                    f"two prices for {price.provider}/{price.model} from {price.effective_from}"
                )
            seen.add(key)
        return self

    def lookup(
        self, provider: str, model: str, at: datetime, endpoint_id: str | None = None
    ) -> Price | None:
        """The newest price for this model (on this endpoint) in effect at `at`, or None. None too
        when a voided free price is the newest in force: it never falls back to an older one."""
        _aware_moment(at, "the lookup time")
        if endpoint_id is None and self._shipped:
            # Shipped rows apply only before the organization's own earliest price of the key
            # (T0, any endpoint); from T0 on only the organization's versions apply (spec 5.2).
            own = [
                p.effective_from for p in self.prices if p.provider == provider and p.model == model
            ]
            if not own or at < min(own):
                rows = [
                    p
                    for p in self._shipped
                    if p.provider == provider and p.model == model and p.effective_from <= at
                ]
                return max(rows, key=lambda p: p.effective_from, default=None)
        candidates = [
            p
            for p in self.prices
            if p.provider == provider
            and p.endpoint_id == endpoint_id
            and p.model == model
            and p.effective_from <= at
        ]
        newest = max(candidates, key=lambda p: p.effective_from, default=None)
        if self._voided_governs(provider, endpoint_id, model, at, newest):
            return None
        return newest

    def _voided_governs(
        self, provider: str, endpoint_id: str | None, model: str, at: datetime, newest: Price | None
    ) -> bool:
        return any(
            t <= at and (newest is None or newest.effective_from <= t)
            for t in self.blocked_since(provider, endpoint_id, model)
        )


class PriceOverride(_Frozen):
    """An approved provisional price for a model with no known price.

    Both rates are required, so budgets can still reserve and stop. Costs computed with it are
    recorded as `provisional`, and each use is a ledger event naming who set it and when.
    """

    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName
    currency: Currency = Currency.USD
    input_per_million: Decimal
    output_per_million: Decimal
    set_by: Annotated[str, StringConstraints(pattern=HUMAN_ID_PATTERN)]
    set_at: datetime
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2_000)]
    # The keyed hash of `reason` under the organization's key (ND25, T47). Set only by the engine
    # (`ContentService.key_overrides`), never read from a config file; the ledger never gets the
    # text, and an override that isn't keyed can't be written to it.
    _reason_hash: str | None = PrivateAttr(default=None)

    @property
    def reason_hash(self) -> str | None:
        return self._reason_hash

    def with_reason_hash(self, value: str) -> "PriceOverride":
        if not re.fullmatch(r"[0-9a-f]{64}", value) or value == self.reason:
            raise ValueError("a reason hash is a 64-digit lower-case hex keyed hash")
        keyed = self.model_copy()
        keyed._reason_hash = value
        return keyed

    @field_validator("input_per_million", "output_per_million", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> object:
        if isinstance(value, float | bool):
            raise ValueError("prices must be Decimal, int or text, never float")
        return value

    @field_validator("input_per_million", "output_per_million")
    @classmethod
    def _valid_rate(cls, value: Decimal) -> Decimal:
        checked = _rate(value, "an override price")
        assert checked is not None
        return checked

    @field_validator("set_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _aware_moment(value, "set_at")

    @model_validator(mode="after")
    def _never_free(self) -> Self:
        """An override can never create a free model (12.2)."""
        _endpoint_rule(self.provider, self.endpoint_id)
        if self.input_per_million == 0 and self.output_per_million == 0:
            raise ValueError(P_OVERRIDE)
        return self


class PriceBasis(StrEnum):
    """Where a resolved price comes from (maps to the ledger's cost basis)."""

    KNOWN = "known"
    PROVISIONAL = "provisional"


class ResolvedPrice(_Frozen):
    """The rates to charge for one call, and where they came from."""

    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName
    at: datetime
    currency: Currency
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None
    cache_write_per_million: Decimal | None
    basis: PriceBasis
    price: Price | None = None
    override: PriceOverride | None = None

    @property
    def cost_basis(self) -> CostBasis:
        """The ledger's cost basis for costs computed with this price."""
        return CostBasis.ACTUAL if self.basis is PriceBasis.KNOWN else CostBasis.PROVISIONAL

    def ledger_payload(self) -> dict[str, object]:
        """What the ledger records about the price used (decimals stay exact)."""
        payload: dict[str, object] = {
            "provider": self.provider,
            "model": self.model,
            "at": self.at.isoformat(),
            "basis": self.basis.value,
            "currency": self.currency.value,
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
        }
        if self.cached_input_per_million is not None:
            payload["cached_input_per_million"] = self.cached_input_per_million
        if self.cache_write_per_million is not None:
            payload["cache_write_per_million"] = self.cache_write_per_million
        if self.endpoint_id is not None:
            payload["endpoint_id"] = self.endpoint_id
        if self.price is not None:
            if self.price.bound_endpoint is not None:
                payload["bound_endpoint"] = self.price.bound_endpoint
            payload["source"] = self.price.source
            payload["effective_from"] = self.price.effective_from.isoformat()
        if self.override is not None:
            payload["override_set_by"] = self.override.set_by
            payload["override_set_at"] = self.override.set_at.isoformat()
            # the reason text is in the owner's config file; the ledger carries its keyed hash only
            if self.override.reason_hash is None:
                raise ValueError(
                    "a price override must be keyed before use (ContentService.key_overrides)"
                )
            payload["override_reason_hash"] = self.override.reason_hash
        return payload


def resolve_price(
    book: PriceBook,
    provider: str,
    model: str,
    at: datetime,
    *,
    overrides: tuple[PriceOverride, ...] = (),
    endpoint_id: str | None = None,
) -> ResolvedPrice:
    """The price to charge, or `UnknownPriceError` (the call is blocked, A3).

    A known price always wins over an override. Overrides apply only to models with no known
    price at `at`, and only if they were set at or before `at`.
    """
    _aware_moment(at, "the call time")
    price = book.lookup(provider, model, at, endpoint_id)
    if price is None and book.blocked_at(provider, endpoint_id, model, at):
        raise UnknownPriceError(f"the free price of {provider}/{model} is not valid")
    if price is not None:
        return ResolvedPrice(
            provider=provider,
            endpoint_id=endpoint_id,
            model=model,
            currency=price.currency,
            input_per_million=price.input_per_million,
            output_per_million=price.output_per_million,
            cached_input_per_million=price.cached_input_per_million,
            cache_write_per_million=price.cache_write_per_million,
            basis=PriceBasis.KNOWN,
            price=price,
            at=at,
        )
    matches = [
        o
        for o in overrides
        if o.provider == provider
        and o.endpoint_id == endpoint_id
        and o.model == model
        and o.set_at <= at
    ]
    if len(matches) > 1:
        raise ValueError(f"more than one override for {provider}/{model}")
    if matches:
        override = matches[0]
        return ResolvedPrice(
            provider=provider,
            endpoint_id=endpoint_id,
            model=model,
            currency=override.currency,
            input_per_million=override.input_per_million,
            output_per_million=override.output_per_million,
            cached_input_per_million=None,
            cache_write_per_million=None,
            basis=PriceBasis.PROVISIONAL,
            override=override,
            at=at,
        )
    raise UnknownPriceError(
        f"no price for {provider}/{model} at {at.isoformat()}; the call is blocked "
        "(add a price, or an override with provisional prices)"
    )


def _part(tokens: int, rate: Decimal | None, kind: str, price: ResolvedPrice) -> Decimal:
    if tokens == 0:
        return Decimal(0)
    if rate is None:
        raise UnknownPriceError(
            f"{price.provider}/{price.model} has no {kind} price but the call used {tokens} "
            f"{kind} tokens; the cost is unknown"
        )
    with _exact():
        return (Decimal(tokens) * rate).scaleb(PER_MILLION_EXPONENT)


def cost_of(usage: TokenUsage, price: ResolvedPrice) -> Money:
    """The exact cost of one call. Raises `UnknownPriceError` if a used token kind has no price."""
    parts = (
        _part(usage.input_tokens, price.input_per_million, "input", price),
        _part(usage.output_tokens, price.output_per_million, "output", price),
        _part(usage.cached_input_tokens, price.cached_input_per_million, "cached input", price),
        _part(usage.cache_write_tokens, price.cache_write_per_million, "cache write", price),
    )
    with _exact():
        total = sum(parts, Decimal(0))
    return Money(amount=total, currency=price.currency)


def worst_case_cost(prompt_tokens: int, max_output_tokens: int, price: ResolvedPrice) -> Money:
    """The most a call can cost, for budget reservations before it runs.

    Every prompt token is priced at the highest known input-side rate (fresh input, cached input
    or cache write), and the output at `max_output_tokens`. However the provider later splits the
    prompt between those kinds, the actual cost can't exceed this.
    """
    for count in (prompt_tokens, max_output_tokens):
        if type(count) is not int or not 0 <= count <= MAX_TOKENS:
            raise ValueError(f"token counts must be integers from 0 to {MAX_TOKENS}")
    input_rate = price.input_per_million
    if price.cache_write_per_million is not None:
        input_rate = max(input_rate, price.cache_write_per_million)
    if price.cached_input_per_million is not None:
        input_rate = max(input_rate, price.cached_input_per_million)
    with _exact():
        total = (Decimal(prompt_tokens) * input_rate).scaleb(PER_MILLION_EXPONENT) + (
            Decimal(max_output_tokens) * price.output_per_million
        ).scaleb(PER_MILLION_EXPONENT)
    return Money(amount=total, currency=price.currency)
