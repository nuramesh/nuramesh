"""Cost per verified result, the spend view and quotes: pure arithmetic over ledger facts.

The rules are `spec/costs/README.md` (ND61, ND62). Everything here is a **read**: it is handed
the model calls and task moves a reader took from the ledger and returns numbers; it writes
nothing and calls nothing. Money is `Decimal`; sums are exact and never rounded; a ratio is
rounded once, at the end, to 6 places half up (computed with `Fraction`, so no context precision
can change a result). An unknown price is never guessed and never counted as zero.
"""

import calendar
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, Inexact
from fractions import Fraction
from typing import Literal

from nuramesh.core.models import format_model_ref

GroupBy = Literal["none", "role", "provider", "model"]
Series = Literal["none", "day", "month"]
Bucket = Literal["accepted", "rejected", "pending", "unattributed"]
BUCKETS: tuple[Bucket, ...] = ("accepted", "rejected", "pending", "unattributed")
REASONS = ("cancelled_by_human", "cancelled_by_system", "gate_failed")
CURRENCY = "USD"
MIN_SAMPLE = 5
SAMPLE_CAP = 20
UNKNOWN_ROLE = "unknown"

_MICRO = Fraction(1, 1_000_000)


# -- number formatting ---------------------------------------------------------------------------


_EXACT = Context(prec=1_000, traps=[Inexact])
"""Decimal arithmetic that raises rather than rounds: a sum here is exact or it is an error."""


def add_exact(a: Decimal, b: Decimal) -> Decimal:
    """`a + b` without the default 28-digit context rounding."""
    return _EXACT.add(a, b)


def sum_exact(values: Sequence[Decimal]) -> Decimal:
    total = Decimal(0)
    for value in values:
        total = _EXACT.add(total, value)
    return total


def money_text(value: Decimal) -> str:
    """Plain decimal text for an exact amount: no exponent, no trailing zeros (`0.30` -> `0.3`).

    Formats the digits as they are; never `normalize()`, which rounds to the context precision."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def round6(value: Fraction | Decimal) -> str:
    """A ratio rounded once to 6 decimal places, half up (amounts are non-negative)."""
    exact = Fraction(value)
    micros = (exact * 1_000_000 + Fraction(1, 2)).__floor__()
    whole, frac = divmod(micros, 1_000_000)
    return f"{whole}.{frac:06d}"


def percentile(sorted_values: Sequence[Decimal], p: Fraction) -> Fraction:
    """Linear interpolation between closest ranks (spec 5.4), exact; `sorted_values` ascending."""
    n = len(sorted_values)
    h = (n - 1) * p
    lo = h.__floor__()
    low = Fraction(sorted_values[lo])
    if lo + 1 >= n:
        return low
    return low + (h - lo) * (Fraction(sorted_values[lo + 1]) - low)


# -- facts handed in by the reader ---------------------------------------------------------------


@dataclass(frozen=True)
class SpendCall:
    """One `model.called` entry that is a spend: a known cost, or an unknown one (`cost` None)."""

    stream: str
    seq: int
    run: str
    task: str | None
    at: datetime
    agent: str
    role: str | None
    provider: str
    model: str
    cost: Decimal | None
    cost_basis: str | None
    provisional: bool = False
    price_version_at: datetime | None = None
    endpoint_id: str | None = None

    @property
    def model_key(self) -> str:
        """The model's identity: two endpoints are never the same model."""
        return format_model_ref(self.provider, self.endpoint_id, self.model)


@dataclass(frozen=True)
class TaskMove:
    """One `task.transitioned` entry."""

    run: str
    task: str
    seq: int
    at: datetime
    to: str
    closed_attempt: bool
    actor: str
    assignee: str | None


@dataclass(frozen=True)
class Placed:
    """A spend call in its bucket (spec 3.2) and the time that decides its period (spec 3.3)."""

    call: SpendCall
    bucket: Bucket
    reason: str | None
    period_time: datetime
    in_retry: bool  # accepted spend made before the task's last closing move


@dataclass
class TaskState:
    moves: list[TaskMove] = field(default_factory=list)

    @property
    def done_at(self) -> datetime | None:
        done = [m for m in self.moves if m.to == "done"]
        return done[-1].at if done else None


def effective_role(call: SpendCall, roles: Mapping[str, str]) -> tuple[str, str]:
    """The role of a call and where it came from: the entry's own `role` always wins; only an
    entry without one uses the configuration as it is now, else `unknown` (spec section 1)."""
    if call.role is not None:
        return call.role, "ledger"
    return roles.get(call.agent, UNKNOWN_ROLE), "config"


def group_key(call: SpendCall, roles: Mapping[str, str], group_by: str) -> str:
    if group_by == "role":
        return effective_role(call, roles)[0]
    if group_by == "provider":
        return call.provider
    if group_by == "model":
        return call.model_key
    return ""


# -- attribution (spec 3.2) ----------------------------------------------------------------------


def task_states(moves: Sequence[TaskMove], as_of: datetime) -> dict[tuple[str, str], TaskState]:
    states: dict[tuple[str, str], TaskState] = defaultdict(TaskState)
    for move in sorted(moves, key=lambda m: (m.run, m.seq)):
        if move.at <= as_of:
            states[(move.run, move.task)].moves.append(move)
    return states


def place_calls(
    calls: Sequence[SpendCall],
    states: Mapping[tuple[str, str], TaskState],
    as_of: datetime,
) -> list[Placed]:
    """Put every spend call (made by `as_of`) in exactly one bucket."""
    placed: list[Placed] = []
    for call in calls:
        if call.at > as_of:
            continue
        if call.task is None:
            placed.append(Placed(call, "unattributed", None, call.at, False))
            continue
        state = states.get((call.run, call.task))
        moves = state.moves if state else []
        closing_after = [m for m in moves if m.closed_attempt and m.seq > call.seq]
        done_at = state.done_at if state else None
        if done_at is not None:
            placed.append(Placed(call, "accepted", None, done_at, bool(closing_after)))
            continue
        cancelled = [m for m in moves if m.to == "cancelled"]
        if cancelled:
            reason = (
                "cancelled_by_human"
                if cancelled[-1].actor.startswith("human:")
                else "cancelled_by_system"
            )
            placed.append(Placed(call, "rejected", reason, call.at, False))
        elif closing_after:
            placed.append(Placed(call, "rejected", "gate_failed", call.at, False))
        else:
            placed.append(Placed(call, "pending", None, call.at, False))
    return placed


# -- rows ----------------------------------------------------------------------------------------


@dataclass
class Cell:
    known: Decimal = Decimal(0)
    calls: int = 0
    unknown: int = 0
    provisional: int = 0
    retry_known: Decimal = Decimal(0)
    by_reason: dict[str, "Cell"] = field(default_factory=dict)

    def add(self, item: Placed) -> None:
        self.calls += 1
        cost = item.call.cost
        if cost is None:
            self.unknown += 1
            return
        self.known = add_exact(self.known, cost)
        if item.call.provisional:
            self.provisional += 1
        if item.in_retry:
            self.retry_known = add_exact(self.retry_known, cost)

    def basic(self) -> dict[str, object]:
        return {
            "known_total": money_text(self.known),
            "calls": self.calls,
            "unknown_calls": self.unknown,
        }


@dataclass
class Row:
    group: str | None = None
    cells: dict[str, Cell] = field(default_factory=lambda: {b: Cell() for b in BUCKETS})
    results: int = 0
    ledger_roles: int = 0
    config_roles: int = 0

    def add(self, item: Placed, role_source: str) -> None:
        cell = self.cells[item.bucket]
        cell.add(item)
        if item.reason is not None:
            cell.by_reason.setdefault(item.reason, Cell()).add(item)
        if role_source == "ledger":
            self.ledger_roles += 1
        else:
            self.config_roles += 1

    @property
    def total_known(self) -> Decimal:
        return sum_exact([c.known for c in self.cells.values()])

    @property
    def total_complete(self) -> bool:
        return all(c.unknown == 0 for c in self.cells.values())

    def to_json(self) -> dict[str, object]:
        acc = self.cells["accepted"]
        complete = acc.unknown == 0
        ratio = Fraction(acc.known) / self.results if self.results > 0 else None
        accepted = {
            **acc.basic(),
            "results": self.results,
            "retry_known_total": money_text(acc.retry_known),
            "provisional_calls": acc.provisional,
            "complete": complete,
            "per_result": round6(ratio) if ratio is not None and complete else None,
            "per_result_at_least": round6(ratio) if ratio is not None and not complete else None,
        }
        rej = self.cells["rejected"]
        rejected = {
            **rej.basic(),
            "by_reason": {r: rej.by_reason[r].basic() for r in REASONS if r in rej.by_reason},
        }
        total_calls = sum(c.calls for c in self.cells.values())
        total_unknown = sum(c.unknown for c in self.cells.values())
        out: dict[str, object] = {
            "accepted": accepted,
            "rejected": rejected,
            "pending": self.cells["pending"].basic(),
            "unattributed": self.cells["unattributed"].basic(),
            "total": {
                "known_total": money_text(self.total_known),
                "calls": total_calls,
                "unknown_calls": total_unknown,
                "complete": self.total_complete,
            },
        }
        return out


def role_source_of(row: Row) -> str:
    if row.ledger_roles and row.config_roles:
        return "mixed"
    return "ledger" if row.ledger_roles else "config"


@dataclass(frozen=True)
class Summary:
    """The overall row and the group rows of one period, as JSON-ready dicts."""

    overall: dict[str, object]
    rows: list[dict[str, object]]
    approximate: bool  # the overall total has an unknown call, so shares are approximate


def summarize(
    placed: Sequence[Placed],
    done_times: Sequence[tuple[tuple[str, str], datetime]],
    roles: Mapping[str, str],
    *,
    group_by: str,
    start: datetime | None,
    end: datetime | None,
    share: bool = False,
) -> Summary:
    """Rows of spec 3.6 for one period `[start, end)` (None = no limit on that side)."""

    def inside(moment: datetime) -> bool:
        return (start is None or moment >= start) and (end is None or moment < end)

    overall = Row()
    groups: dict[str, Row] = {}
    group_tasks: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for item in placed:
        if not inside(item.period_time):
            continue
        _, source = effective_role(item.call, roles)
        overall.add(item, source)
        if group_by != "none":
            key = group_key(item.call, roles, group_by)
            row = groups.setdefault(key, Row(group=key))
            row.add(item, source)
            if item.bucket == "accepted" and item.call.task is not None:
                group_tasks[key].add((item.call.run, item.call.task))
    overall.results = sum(1 for _, at in done_times if inside(at))
    for key, row in groups.items():
        row.results = len(group_tasks[key])
    rows: list[dict[str, object]] = []
    total = overall.total_known
    for key in sorted(groups):
        row = groups[key]
        data = row.to_json()
        if group_by == "role":
            data = {"group": key, "role_source": role_source_of(row), **data}
        else:
            data = {"group": key, **data}
        if share:
            data["share"] = (
                round6(Fraction(row.total_known) / Fraction(total)) if total != 0 else None
            )
        rows.append(data)
    return Summary(overall.to_json(), rows, not overall.total_complete)


# -- periods -------------------------------------------------------------------------------------


def _next_boundary(moment: datetime, series: str) -> datetime | None:
    """The next UTC day or month boundary after `moment`; None past the last representable one."""
    try:
        day = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
        if series == "day":
            return day + timedelta(days=1)
        days = calendar.monthrange(moment.year, moment.month)[1]
        return datetime(moment.year, moment.month, 1, tzinfo=UTC) + timedelta(days=days)
    except OverflowError:
        return None


def iter_periods(
    start: datetime, end: datetime, series: str
) -> Iterator[tuple[datetime, datetime]]:
    """Lazily split `[start, end)` into UTC day or month buckets, the first and last cut at the
    ends. A caller that limits the count stops reading early (a huge range is never built)."""
    if series == "none":
        yield (start, end)
        return
    cursor = start
    while cursor < end:
        boundary = _next_boundary(cursor, series)
        stop = end if boundary is None else min(boundary, end)
        yield (cursor, stop)
        cursor = stop


def series_periods(start: datetime, end: datetime, series: str) -> list[tuple[datetime, datetime]]:
    return list(iter_periods(start, end, series))


# -- quotes (spec section 5) ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunFacts:
    """What a past run looked like, for a quote."""

    run: str
    ended_at: datetime | None
    aborted: bool
    task_assignees: tuple[str | None, ...]
    all_done: bool
    calls: tuple[SpendCall, ...]


@dataclass(frozen=True)
class QuoteResult:
    status: str
    low: str | None
    median: str | None
    high: str | None
    sample_size: int
    excluded: Mapping[str, int]


def quote(
    wanted_roles: Sequence[str],
    wanted_models: set[str],
    runs: Iterable[RunFacts],
    roles: Mapping[str, str],
    current_price_at: Mapping[str, datetime],
    as_of: datetime,
) -> QuoteResult:
    """The advisory cost range of a run like the request, from comparable runs (spec 5.3, 5.4).

    `runs` is read lazily, **newest run first** (by the time it ended, ties by run id, larger
    first), and the walk stops once 20 comparable runs are found, so the `excluded` counts cover
    the runs walked, that is the runs newer than the 20th comparable one."""
    want_roles = sorted(wanted_roles)
    excluded = {"not_clean": 0, "unknown_cost": 0, "price_changed": 0}
    comparable: list[tuple[datetime, str, Decimal]] = []
    for r in runs:
        if r.ended_at is None or r.ended_at > as_of:
            continue
        have_roles = sorted(
            roles.get(a, UNKNOWN_ROLE) if a else UNKNOWN_ROLE for a in r.task_assignees
        )
        if have_roles != want_roles or {c.model_key for c in r.calls} != wanted_models:
            continue
        if r.aborted or not r.all_done:
            excluded["not_clean"] += 1
            continue
        if any(c.cost is None for c in r.calls):
            excluded["unknown_cost"] += 1
            continue
        if any(
            c.price_version_at is None or current_price_at.get(c.model_key) != c.price_version_at
            for c in r.calls
        ):
            excluded["price_changed"] += 1
            continue
        comparable.append((r.ended_at, r.run, sum_exact([c.cost or Decimal(0) for c in r.calls])))
        if len(comparable) >= SAMPLE_CAP:
            break  # `runs` is newest first: these are the newest 20, nothing older is needed
    # newest first; ties by run id, the larger first (code-point order of the id)
    comparable.sort(key=lambda t: (t[0], codepoints(t[1])), reverse=True)
    sample = comparable[:SAMPLE_CAP]
    if len(sample) < MIN_SAMPLE:
        return QuoteResult("not_enough_history", None, None, None, len(sample), excluded)
    totals = sorted(t[2] for t in sample)
    low, median, high = (percentile(totals, Fraction(k, 4)) for k in (1, 2, 3))
    return QuoteResult("ok", round6(low), round6(median), round6(high), len(sample), excluded)


def codepoints(text: str) -> tuple[int, ...]:
    """A text as its code points: the order run ids are tied by (spec/costs 5.4)."""
    return tuple(ord(ch) for ch in text)
