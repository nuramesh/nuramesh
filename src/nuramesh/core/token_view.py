"""The token and cache view (R1.34 Part A): pure arithmetic over
facts a reader took from the ledger.

It reads nothing and writes nothing. Money is `Decimal` and every sum is exact; a ratio is
rounded once, to 6 places, half away from zero (the absolute value rounded, then the sign put
back). A price is only ever the one **recorded on the call**: the view never reprices an old
call from today's price book. Unknown stays unknown: a call whose saving cannot be known is
counted in `unknown_saving_calls`, never as zero and never guessed.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact
from fractions import Fraction

from nuramesh.core.costs import SpendCall, effective_role, money_text, sum_exact
from nuramesh.core.models import format_model_ref

EXACT = Context(prec=1_000, traps=[Inexact])
"""Decimal arithmetic that raises rather than rounds: a sum here is exact or it is an error."""
_MILLION = Decimal(1_000_000)
ALL = "all"


@dataclass(frozen=True)
class TokenUsage:
    """The tokens of one call, as recorded (`input_tokens` excludes the cached ones)."""

    input: int
    output: int
    cached: int
    write: int


@dataclass(frozen=True)
class RecordedRates:
    """The per-million rates a call was charged at, from its own `price` block (None: absent)."""

    input: Decimal | None
    cached_input: Decimal | None
    cache_write: Decimal | None


@dataclass(frozen=True)
class TokenCall:
    """One spend call with the usage and the recorded rates (`usage` None: no usage block;
    `rates` None: no price block, as on a `settled_late` entry)."""

    call: SpendCall
    usage: TokenUsage | None
    rates: RecordedRates | None


def round6_away(value: Fraction | Decimal) -> str:
    """A ratio rounded once to 6 places, half away from zero: `-0.0000005` is `-0.000001`."""
    exact = Fraction(value)
    micros = (abs(exact) * 1_000_000 + Fraction(1, 2)).__floor__()
    whole, frac = divmod(micros, 1_000_000)
    sign = "-" if exact < 0 and micros != 0 else ""
    return f"{sign}{whole}.{frac:06d}"


def provider_key(call: SpendCall) -> str:
    """`provider`, or `provider/endpoint id`, as the provider router names it."""
    return call.provider if call.endpoint_id is None else f"{call.provider}/{call.endpoint_id}"


def group_of(call: SpendCall, roles: Mapping[str, str], group_by: str) -> str:
    if group_by == "role":
        return effective_role(call, roles)[0]
    if group_by == "provider":
        return provider_key(call)
    if group_by == "model":
        return format_model_ref(call.provider, call.endpoint_id, call.model)
    return ALL


def known_saving(item: TokenCall) -> tuple[Decimal, Decimal] | None:
    """`(saving, write premium)` of one call, or None when it cannot be known (spec section 3)."""
    usage = item.usage
    if usage is None:
        return None
    if usage.cached == 0 and usage.write == 0:
        return Decimal(0), Decimal(0)  # nothing to discount or charge extra, whatever the price
    rates = item.rates
    if item.call.cost is None or rates is None or rates.input is None:
        return None
    saving = Decimal(0)
    premium = Decimal(0)
    if usage.cached > 0:
        if rates.cached_input is None:
            return None
        saving = EXACT.divide(
            EXACT.multiply(Decimal(usage.cached), EXACT.subtract(rates.input, rates.cached_input)),
            _MILLION,
        )
    if usage.write > 0:
        if rates.cache_write is None:
            return None
        premium = EXACT.divide(
            EXACT.multiply(Decimal(usage.write), EXACT.subtract(rates.cache_write, rates.input)),
            _MILLION,
        )
    return saving, premium


def _row(items: Sequence[TokenCall]) -> dict[str, object]:
    unknown_calls = sum(1 for i in items if i.call.cost is None)
    known = sum_exact([i.call.cost for i in items if i.call.cost is not None])
    with_usage = [i.usage for i in items if i.usage is not None]
    input_tokens = sum(u.input for u in with_usage)
    cached = sum(u.cached for u in with_usage)
    write = sum(u.write for u in with_usage)
    savings = [known_saving(i) for i in items]
    unknown_saving = sum(1 for s in savings if s is None)
    saving = sum_exact([s[0] for s in savings if s is not None])
    premium = sum_exact([s[1] for s in savings if s is not None])
    net = EXACT.subtract(saving, premium)
    prompt = input_tokens + cached + write
    hit_rate = None if prompt == 0 else round6_away(Fraction(cached, prompt))
    saving_complete = unknown_saving == 0
    denominator = EXACT.add(known, net)
    share = (
        round6_away(Fraction(net) / Fraction(denominator))
        if saving_complete and unknown_calls == 0 and denominator != 0
        else None
    )
    return {
        "calls": len(items),
        "unknown_calls": unknown_calls,
        "known_total": money_text(known),
        "complete": unknown_calls == 0,
        "input_tokens": input_tokens,
        "output_tokens": sum(u.output for u in with_usage),
        "cached_input_tokens": cached,
        "cache_write_tokens": write,
        "calls_without_usage": len(items) - len(with_usage),
        "cache_hit_rate": hit_rate,
        "cache_saving": money_text(saving),
        "cache_write_premium": money_text(premium),
        "net_cache_saving": money_text(net),
        "unknown_saving_calls": unknown_saving,
        "saving_complete": saving_complete,
        "cache_saving_share": share,
    }


def token_rows(
    calls: Sequence[TokenCall], roles: Mapping[str, str], group_by: str
) -> list[dict[str, object]]:
    """One row per group (`all` for `none`), in code point order of the group; a group with no
    call has no row. Each row is `{"group": key, ...}` with the fields of spec section 3."""
    groups: dict[str, list[TokenCall]] = defaultdict(list)
    for item in calls:
        groups[group_of(item.call, roles, group_by)].append(item)
    return [{"group": key, **_row(groups[key])} for key in sorted(groups)]


__all__ = [
    "ALL",
    "RecordedRates",
    "TokenCall",
    "TokenUsage",
    "known_saving",
    "round6_away",
    "token_rows",
]
