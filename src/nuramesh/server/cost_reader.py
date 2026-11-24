"""Reads the ledger for the cost views: which runs, which entries, which facts.

Nothing is stored and nothing is written: every number is computed from the ledger streams of
the organization's runs, up to `as_of`. A request reads at most `MAX_ENTRIES` ledger entries; a
bigger range is refused (`CostReadError`, 422 `range_too_large`), never answered with a
truncated sum.
"""

import heapq
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from nuramesh.core.costs import CURRENCY, RunFacts, SpendCall, TaskMove, codepoints
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.models import ModelCalledPayload
from nuramesh.core.token_view import RecordedRates, TokenCall, TokenUsage
from nuramesh.stores.base import BadTimeError, Store

from .views import read_all

MAX_ENTRIES = 100_000

_SPEND_OUTCOMES = {"ok", "settled_late"}


class CostReadError(Exception):
    """A cost request that can't be answered correctly: `status`, stable `code`, a message."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


def _damaged(stream: str) -> CostReadError:
    """A cost view that cannot be answered because one of the organization's runs holds a stored
    time that is not real (the value is never repeated). Only that organization is affected."""
    return CostReadError(
        409,
        "damaged_run",
        f"run {stream.removeprefix('run:')} holds a stored time that is not real; "
        "the cost views cannot be answered until it is repaired",
    )


def _refuse_damaged_heads(store: Store, org_id: str) -> None:
    """A run whose head cannot be read is not in `heads()`: it must not be left out of the
    organization's numbers silently (ND238)."""
    for stream in store.damaged_heads():
        if stream.startswith("run:") and store.run_owner(stream) == org_id:
            raise _damaged(stream)


@dataclass(frozen=True)
class RunStream:
    run: str
    events: tuple[LedgerEvent, ...]  # only entries made by `as_of`


def read_org_runs(
    store: Store,
    org_id: str,
    *,
    start: datetime | None,
    end: datetime | None,
    as_of: datetime,
) -> list[RunStream]:
    """The run streams of one organization that can matter for `[start, end)` and `as_of`.

    A stream matters when its first entry is by `as_of` and before `end`, and its last entry is
    at or after `start`: nothing outside that span can add to a period's numbers. The sum of the
    candidates' lengths is checked against the bound **before** any of them is read.
    """
    _refuse_damaged_heads(store, org_id)
    heads = store.heads()
    chosen: list[tuple[str, int]] = []
    for stream in sorted(heads):
        if not stream.startswith("run:"):
            continue
        head = heads[stream]
        if start is not None and head.at < start:
            continue
        if store.run_owner(stream) != org_id:  # another organization's run: one raw row, no time
            continue
        try:
            first = next(iter(store.read_stream(stream, limit=1)), None)
        except BadTimeError:
            raise _damaged(stream) from None
        if first is None or first.type != EventType.RUN_STARTED.value:
            continue
        if first.at > as_of or (end is not None and first.at >= end):
            continue
        chosen.append((stream, head.seq))
    if sum(n for _, n in chosen) > MAX_ENTRIES:
        raise CostReadError(
            422,
            "range_too_large",
            f"this range holds more than {MAX_ENTRIES} ledger entries; narrow the period",
        )
    runs: list[RunStream] = []
    for stream, seq in chosen:
        try:
            events = [e for e in read_all(store, stream) if e.seq <= seq and e.at <= as_of]
        except BadTimeError:
            raise _damaged(stream) from None
        runs.append(RunStream(stream[4:], tuple(events)))
    return runs


class _Pending:
    """A read run waiting in the heap; the one that ended last (ties: larger id) comes out first."""

    __slots__ = ("ended", "key", "stream")

    def __init__(self, ended: datetime, stream: RunStream) -> None:
        self.ended, self.stream, self.key = ended, stream, codepoints(stream.run)

    def __lt__(self, other: "_Pending") -> bool:
        return (self.ended, self.key) > (other.ended, other.key)


def _ended_at(events: Sequence[LedgerEvent]) -> datetime | None:
    ended = next((e for e in reversed(events) if e.type == EventType.RUN_ENDED.value), None)
    return ended.at if ended else None


def iter_runs_newest_first(store: Store, org_id: str, *, as_of: datetime) -> Iterator[RunStream]:
    """The organization's ended runs, the one that ended last first (by `run.ended` time, ties
    by run id, the larger first), read lazily.

    Streams are read in order of their newest entry. A run's `run.ended` time can never be later
    than its newest entry (nor than `as_of`), so a read run is released only once its end time is
    later than that bound for every stream not read yet; an entry written after `run.ended`
    (a late settle or release) or an `as_of` in the past can therefore not change the order.

    The entries read are counted as the walk goes (the whole stream up to its head, including
    entries after `as_of`); one more than `MAX_ENTRIES` raises `range_too_large` (the sample
    would be a guess). Checking whether a stream belongs to the organization reads only its first
    entry and is not counted: other organizations' streams are outside the bound.
    """
    _refuse_damaged_heads(store, org_id)
    heads = store.heads()
    streams = sorted((s for s in heads if s.startswith("run:")), key=lambda s: (heads[s].at, s))
    waiting: list[_Pending] = []
    read = 0
    while streams or waiting:
        # the latest a not-yet-read run could have ended
        bound = min(heads[streams[-1]].at, as_of) if streams else None
        if waiting and (bound is None or waiting[0].ended > bound):
            yield heapq.heappop(waiting).stream
            continue
        stream = streams.pop()
        if store.run_owner(stream) != org_id:  # another organization's run: one raw row, no time
            continue
        try:
            first = next(iter(store.read_stream(stream, limit=1)), None)
        except BadTimeError:
            raise _damaged(stream) from None
        if first is None or first.type != EventType.RUN_STARTED.value:
            continue
        if first.at > as_of:
            continue
        read += heads[stream].seq
        if read > MAX_ENTRIES:
            raise CostReadError(
                422,
                "range_too_large",
                f"the history holds more than {MAX_ENTRIES} ledger entries in the newest runs",
            )
        try:
            events = tuple(
                e for e in read_all(store, stream) if e.seq <= heads[stream].seq and e.at <= as_of
            )
        except BadTimeError:
            raise _damaged(stream) from None
        ended = _ended_at(events)
        if ended is not None:  # a run still going is no history
            heapq.heappush(waiting, _Pending(ended, RunStream(stream[4:], events)))


def _currency_check(event: LedgerEvent) -> None:
    if event.cost is not None and str(event.cost.currency) != CURRENCY:
        raise CostReadError(409, "mixed_currencies", "the ledger holds more than one currency")


def spend_call(event: LedgerEvent) -> SpendCall | None:
    """The spend a `model.called` entry stands for (spec section 1), or None if it is no spend."""
    try:
        payload = ModelCalledPayload.model_validate(dict(event.payload))
    except ValidationError:
        raise CostReadError(500, "bad_ledger_entry", "a model.called entry is malformed") from None
    basis = str(event.cost_basis) if event.cost_basis is not None else None
    cost: Decimal | None
    if payload.outcome == "settle_failed":
        cost = None
    elif payload.outcome in _SPEND_OUTCOMES:
        if event.cost is not None:
            cost = event.cost.amount
        elif basis is None:
            return None  # nothing was spent
        else:
            cost = None
    else:
        return None
    _currency_check(event)
    return SpendCall(
        stream=event.stream,
        seq=event.seq,
        run=event.run_id or event.stream.removeprefix("run:"),
        task=event.task_id,
        at=event.at,
        agent=payload.agent,
        role=payload.role,
        provider=payload.provider,
        endpoint_id=payload.endpoint_id,
        model=payload.model,
        cost=cost,
        cost_basis=basis,
        provisional=basis == "provisional",
        price_version_at=payload.price_version_at,
    )


def _count(value: object) -> int | None:
    """A token count from a payload: a non-negative integer, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage_of(raw: object) -> TokenUsage | None:
    """The `usage` block of a `model.called` payload; None when absent, null or damaged (a call
    without usage: its tokens are unknown, never zero)."""
    if not isinstance(raw, Mapping):
        return None
    counts = [
        _count(raw.get(k))
        for k in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")
    ]
    if any(c is None for c in counts):
        return None
    inp, out, cached, write = (c for c in counts if c is not None)
    return TokenUsage(input=inp, output=out, cached=cached, write=write)


def _rate(raw: Mapping[str, object], key: str) -> Decimal | None:
    value = raw.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate >= 0 else None


def token_call(event: LedgerEvent) -> TokenCall | None:
    """A spend call with its recorded usage and rates (R1.34 Part A), or None if it is no spend.
    The rates are the ones the call was charged at (`price` block); today's price book is never
    consulted."""
    call = spend_call(event)
    if call is None:
        return None
    price = event.payload.get("price")
    rates = (
        RecordedRates(
            input=_rate(price, "input_per_million"),
            cached_input=_rate(price, "cached_input_per_million"),
            cache_write=_rate(price, "cache_write_per_million"),
        )
        if isinstance(price, Mapping)
        else None
    )
    return TokenCall(call=call, usage=_usage_of(event.payload.get("usage")), rates=rates)


def token_facts_of(runs: Sequence[RunStream]) -> list[TokenCall]:
    """Every spend call of the runs with its usage and rates, in ledger order."""
    return [
        item
        for run in runs
        for event in run.events
        if event.type == EventType.MODEL_CALLED.value
        for item in (token_call(event),)
        if item is not None
    ]


def task_move(event: LedgerEvent) -> TaskMove | None:
    if event.task_id is None:
        return None
    to = event.payload.get("to")
    if not isinstance(to, str):
        return None
    assignee = event.payload.get("assignee")
    return TaskMove(
        run=event.run_id or event.stream.removeprefix("run:"),
        task=event.task_id,
        seq=event.seq,
        at=event.at,
        to=to,
        closed_attempt=event.payload.get("closed_attempt") is True,
        actor=event.actor,
        assignee=assignee if isinstance(assignee, str) else None,
    )


def facts_of(runs: Sequence[RunStream]) -> tuple[list[SpendCall], list[TaskMove]]:
    calls: list[SpendCall] = []
    moves: list[TaskMove] = []
    for run in runs:
        for event in run.events:
            if event.type == EventType.MODEL_CALLED.value:
                call = spend_call(event)
                if call is not None:
                    calls.append(call)
            elif event.type == EventType.TASK_TRANSITIONED.value:
                move = task_move(event)
                if move is not None:
                    moves.append(move)
    return calls, moves


def run_facts(stream: RunStream) -> RunFacts:
    """What a quote needs to know about one run."""
    calls, moves = facts_of([stream])
    tasks: set[str] = {m.task for m in moves}
    tasks |= {
        e.task_id
        for e in stream.events
        if e.type == EventType.TASK_CREATED.value and e.task_id is not None
    }
    last: dict[str, TaskMove] = {}
    for move in moves:
        last[move.task] = move  # moves are in seq order
    ended = next((e for e in reversed(stream.events) if e.type == EventType.RUN_ENDED.value), None)
    return RunFacts(
        run=stream.run,
        ended_at=ended.at if ended else None,
        aborted=bool(ended and ended.payload.get("aborted") is True),
        task_assignees=tuple(last[t].assignee if t in last else None for t in sorted(tasks)),
        all_done=bool(tasks) and all(t in last and last[t].to == "done" for t in tasks),
        calls=tuple(calls),
    )
