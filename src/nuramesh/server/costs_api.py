"""The cost endpoints: cost per verified result, the
cross-provider spend view, the calls behind a figure, and advisory quotes.

Every endpoint only **reads** the ledger of the session's organization (the one organization of
the store until sessions are enforced, R1.22 PR 3): no ledger entry, no store write, no effect on
a budget, an approval or a run. A quote is a POST only because its plan is a list. The arithmetic
is `nuramesh.core.costs`; `cost_reader` takes the facts from the ledger.
"""

import bisect
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import islice
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.costs import (
    BUCKETS,
    CURRENCY,
    MIN_SAMPLE,
    UNKNOWN_ROLE,
    Placed,
    effective_role,
    group_key,
    iter_periods,
    money_text,
    place_calls,
    quote,
    summarize,
    task_states,
)
from nuramesh.core.models import U_ENDPOINT_OTHER, format_model_ref
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId, Slug
from nuramesh.core.prices import ModelName
from nuramesh.server import cost_reader
from nuramesh.server.operation_ids import operation_id_for
from nuramesh.server.pagination import (
    Cursor,
    decode_position,
    encode_cursor,
    invalid,
    refuse_old_parameters,
    respond,
)
from nuramesh.stores.base import Store

MAX_PERIODS = 1_000
MAX_QUOTE_TASKS = 200
DEFAULT_PAGE = 50
MAX_PAGE = 200
Problem = Callable[..., Exception]


def iso(moment: datetime) -> str:
    """UTC text with a `Z`, with microseconds only when there are some."""
    utc = moment.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return text + (f".{utc.microsecond:06d}" if utc.microsecond else "") + "Z"


class _Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Slug
    provider: Slug
    endpoint_id: EndpointId | None = None
    model: ModelName

    @model_validator(mode="after")
    def _endpoint_rule(self) -> "_Task":
        """Only `openai-compatible` has an endpoint id (spec openai-compatible 2.1)."""
        if self.endpoint_id is not None and self.provider != OPENAI_COMPATIBLE:
            raise ValueError(U_ENDPOINT_OTHER)
        return self

    @field_validator("role")
    @classmethod
    def _real_role(cls, value: str) -> str:
        if value == UNKNOWN_ROLE:
            raise ValueError("'unknown' is not a role a quote can ask for")
        return value


class QuoteBody(BaseModel):
    """A plan: one entry per planned task, with the role of its assignee, provider and model."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[_Task] = Field(min_length=1, max_length=MAX_QUOTE_TASKS)
    as_of: str | None = Field(default=None, max_length=64)


def install_costs(
    app: FastAPI,
    store: Store,
    need_config: Callable[[], NuraMeshConfig],
    problem: Problem,
    dependencies: Sequence[Any] = (),
) -> None:
    """Add the four `/v1/costs` routes. `problem(status, code, detail)` builds the API's error.

    `dependencies` (FastAPI `Depends(...)` items) apply to every route here: this is where the
    session gate attaches when sessions are enforced on reads (R1.22 PR 3)."""
    router = APIRouter(
        dependencies=list(dependencies), generate_unique_id_function=operation_id_for
    )
    Config = Annotated[NuraMeshConfig, Depends(need_config)]

    def instant(text: str, name: str) -> datetime:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise problem(422, "invalid_request", f"{name} must be a UTC instant") from None
        if moment.tzinfo is None:
            raise problem(422, "invalid_request", f"{name} needs a UTC offset (for example Z)")
        try:
            return moment.astimezone(UTC)
        except OverflowError:
            raise problem(422, "invalid_request", f"{name} is out of range") from None

    def window(
        from_: str | None, to: str | None, as_of: str | None
    ) -> tuple[datetime, datetime, datetime]:
        now = store.now()
        month = datetime(now.year, now.month, 1, tzinfo=UTC)
        following = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
        start = instant(from_, "from") if from_ else month
        end = instant(to, "to") if to else following
        if not start < end:
            raise problem(422, "invalid_request", "from must be before to")
        return start, end, instant(as_of, "as_of") if as_of else now

    def load(config: NuraMeshConfig, start: datetime | None, end: datetime | None, as_of: datetime):  # type: ignore[no-untyped-def]
        try:
            runs = cost_reader.read_org_runs(
                store, config.org.id, start=start, end=end, as_of=as_of
            )
            return runs
        except cost_reader.CostReadError as exc:
            raise problem(exc.status, exc.code, exc.detail) from None

    def facts(runs: list[cost_reader.RunStream]):  # type: ignore[no-untyped-def]
        try:
            return cost_reader.facts_of(runs)
        except cost_reader.CostReadError as exc:
            raise problem(exc.status, exc.code, exc.detail) from None

    def head(
        config: NuraMeshConfig, start: datetime, end: datetime, as_of: datetime
    ) -> dict[str, Any]:
        return {
            "organization": config.org.id,
            "currency": CURRENCY,
            "as_of": iso(as_of),
            "from": iso(start),
            "to": iso(end),
        }

    def slice_(
        items: list[Placed], times: list[datetime], start: datetime, end: datetime
    ) -> list[Placed]:
        return items[bisect.bisect_left(times, start) : bisect.bisect_left(times, end)]

    GroupBy = Annotated[str, Query(pattern=r"^(none|role|provider|model)$")]
    Text = Annotated[str | None, Query(max_length=64)]

    @router.get("/v1/costs/per-result")
    def per_result(
        cfg: Config,
        group_by: GroupBy = "none",
        from_: Annotated[Text, Query(alias="from")] = None,
        to: Text = None,
        as_of: Text = None,
    ) -> dict[str, Any]:
        """Cost per verified result for `[from, to)`: an overall row and a row per group."""
        start, end, now = window(from_, to, as_of)
        runs = load(cfg, start, end, now)
        calls, moves = facts(runs)
        states = task_states(moves, now)
        placed = place_calls(calls, states, now)
        done = [(k, s.done_at) for k, s in states.items() if s.done_at is not None]
        done_times = [(k, at) for k, at in done if at is not None]
        summary = summarize(
            placed, done_times, _roles(cfg), group_by=group_by, start=start, end=end
        )
        return {
            **head(cfg, start, end, now),
            "group_by": group_by,
            "overall": summary.overall,
            "rows": summary.rows,
        }

    @router.get("/v1/costs/spend")
    def spend(
        cfg: Config,
        group_by: Annotated[str, Query(pattern=r"^(role|provider|model)$")],
        from_: Annotated[Text, Query(alias="from")] = None,
        to: Text = None,
        series: Annotated[str, Query(pattern=r"^(none|day|month)$")] = "none",
        as_of: Text = None,
    ) -> dict[str, Any]:
        """What the money bought: per provider, model or role, for the range or per day or month."""
        start, end, now = window(from_, to, as_of)
        periods = list(islice(iter_periods(start, end, series), MAX_PERIODS + 1))
        if len(periods) > MAX_PERIODS:
            raise problem(
                422, "range_too_large", f"more than {MAX_PERIODS} periods; narrow the period"
            )
        runs = load(cfg, start, end, now)
        calls, moves = facts(runs)
        states = task_states(moves, now)
        placed = sorted(place_calls(calls, states, now), key=lambda p: p.period_time)
        times = [p.period_time for p in placed]
        done = sorted(
            ((k, s.done_at) for k, s in states.items() if s.done_at is not None),
            key=lambda t: t[1],
        )
        done_at = [at for _, at in done]
        roles = _roles(cfg)
        out: list[dict[str, Any]] = []
        for p_start, p_end in periods:
            part = slice_(placed, times, p_start, p_end)
            finished = done[
                bisect.bisect_left(done_at, p_start) : bisect.bisect_left(done_at, p_end)
            ]
            summary = summarize(
                part, finished, roles, group_by=group_by, start=p_start, end=p_end, share=True
            )
            out.append(
                {
                    "start": iso(p_start),
                    "end": iso(p_end),
                    "overall": summary.overall,
                    "rows": summary.rows,
                    "share_approximate": summary.approximate,
                }
            )
        return {
            **head(cfg, start, end, now),
            "group_by": group_by,
            "series": series,
            "periods": out,
        }

    @router.get("/v1/costs/calls")
    def calls_list(
        request: Request,
        cfg: Config,
        bucket: Annotated[
            str | None, Query(pattern=r"^(accepted|rejected|pending|unattributed)$")
        ] = None,
        group_by: GroupBy = "none",
        group: Annotated[str | None, Query(max_length=200)] = None,
        from_: Annotated[Text, Query(alias="from")] = None,
        to: Text = None,
        as_of: Text = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        after: Annotated[str | None, Query(max_length=300)] = None,
        cursor: Cursor = None,
    ) -> Response:
        """The calls behind one cell of the other views, newest first, paged."""
        if (group is None) != (group_by == "none"):
            raise problem(422, "invalid_request", "group and group_by go together")
        start, end, now = window(from_, to, as_of)
        route = "GET /v1/costs/calls"
        refuse_old_parameters(cursor, after=after)
        since = _cursor(after, problem) if after else None
        if cursor is not None:  # the position is the same `at|stream|seq` text as `after`
            since = _position(decode_position(cursor, route))
            if since is None:
                raise invalid()
        runs = load(cfg, start, end, now)
        calls, moves = facts(runs)
        placed = place_calls(calls, task_states(moves, now), now)
        roles = _roles(cfg)
        chosen = [
            p
            for p in placed
            if start <= p.period_time < end
            and (bucket is None or p.bucket == bucket)
            and (group is None or group_key(p.call, roles, group_by) == group)
        ]
        chosen.sort(key=lambda p: (p.call.at, p.call.stream, p.call.seq), reverse=True)
        if since is not None:
            chosen = [p for p in chosen if (p.call.at, p.call.stream, p.call.seq) < since]
        page = chosen[:limit]
        more = len(chosen) > limit
        last = page[-1].call if page else None
        last_at = f"{iso(last.at)}|{last.stream}|{last.seq}" if more and last else None
        return respond(
            request,
            items=[_call_json(p, roles) for p in page],
            old_key="calls",
            next_cursor=None if last_at is None else encode_cursor(route, last_at),
            extra={**head(cfg, start, end, now), "bucket": bucket, "group_by": group_by,
                   "group": group},
            old_extra={"next": last_at},
            native=True,
        )  # fmt: skip

    @router.post("/v1/costs/quote")
    def quote_for(body: QuoteBody, cfg: Config) -> dict[str, Any]:
        """An advisory cost range for a plan, from comparable past runs. It writes nothing."""
        now = instant(body.as_of, "as_of") if body.as_of else store.now()
        wanted = {format_model_ref(t.provider, t.endpoint_id, t.model) for t in body.tasks}
        try:
            history = (
                cost_reader.run_facts(r)
                for r in cost_reader.iter_runs_newest_first(store, cfg.org.id, as_of=now)
            )
            result = quote(
                [t.role for t in body.tasks],
                wanted,
                history,
                _roles(cfg),
                _current_versions(cfg, body, now),
                now,
            )
        except cost_reader.CostReadError as exc:
            raise problem(exc.status, exc.code, exc.detail) from None
        return {
            "organization": cfg.org.id,
            "currency": CURRENCY,
            "as_of": iso(now),
            "status": result.status,
            "low": result.low,
            "median": result.median,
            "high": result.high,
            "sample_size": result.sample_size,
            "minimum_sample": MIN_SAMPLE,
            "excluded": dict(result.excluded),
            "advisory": True,
            "blocking": False,
            "budget_input": False,
        }

    # plain routes, not `include_router`: newer FastAPI wraps an included router in one object
    # that has no path, which the authorizer and the route-table test could not see through
    app.router.routes.extend(router.routes)


def _roles(config: NuraMeshConfig) -> dict[str, str]:
    return {a.id: a.role for a in config.org.agents}


def _current_versions(config: NuraMeshConfig, body: QuoteBody, at: datetime) -> dict[str, datetime]:
    """When the price version in force for each requested model took effect (an override counts as
    its own version; a listed price beats an override). A model with no price is left out."""
    found: dict[str, datetime] = {}
    for t in body.tasks:
        key = format_model_ref(t.provider, t.endpoint_id, t.model)
        price = config.prices.lookup(t.provider, t.model, at, t.endpoint_id)
        if price is not None:
            found[key] = price.effective_from
            continue
        for o in config.price_overrides:
            if (o.provider, o.endpoint_id, o.model) == (
                t.provider,
                t.endpoint_id,
                t.model,
            ) and o.set_at <= at:
                found[key] = o.set_at
    return found


def _position(text: str) -> tuple[datetime, str, int] | None:
    try:
        at, stream, seq = text.split("|")
        moment = datetime.fromisoformat(at)
        if moment.tzinfo is None:
            raise ValueError("no time zone")
        return moment, stream, int(seq)
    except ValueError:
        return None


def _cursor(text: str, problem: Problem) -> tuple[datetime, str, int]:
    found = _position(text)
    if found is None:
        raise problem(422, "invalid_request", "after is not a cursor from this endpoint")
    return found


def _call_json(item: Placed, roles: dict[str, str]) -> dict[str, Any]:
    c = item.call
    return {
        "stream": c.stream,
        "seq": c.seq,
        "run": c.run,
        "task": c.task,
        "agent": c.agent,
        "role": effective_role(c, roles)[0],
        "provider": c.provider,
        "model": c.model,
        "at": iso(c.at),
        "cost": None if c.cost is None else money_text(c.cost),
        "cost_basis": c.cost_basis,
        "bucket": item.bucket,
        "reason": item.reason,
    }


__all__ = ["BUCKETS", "QuoteBody", "install_costs"]
