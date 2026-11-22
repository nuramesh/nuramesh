"""The dashboard read API (spec/r211 sections 3 and 4): four derived, read-only views of the ledger.

`GET /v1/dashboards/{verification,verifiers,cost-latency,approvals}`. Nothing is stored and nothing
is written: every figure is computed from the ledger streams of the caller's organization on
read, by the pure formulas of `nuramesh.telemetry.dashboards`.

**The organization is the one of the authenticated session or token, never an input.** The routes
take no organization id (a query parameter named `organization` or `org` is 400 `bad_request`). A
caller with no single organization gets 403. The check order is: credentials (401) and the read
permission (403) in the authorizer, the organization (403), the window (400), then a cache hit,
or a free computation slot (429 `busy`), and only then the figures.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.ledger import EventType
from nuramesh.server import dashboards_reader
from nuramesh.server.authz import Caller
from nuramesh.server.operation_ids import operation_id_for
from nuramesh.server.scope import LOCAL_INSPECTOR, is_org
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores.base import Store
from nuramesh.telemetry import dashboards as formulas

DEFAULT_MAX_EVENTS = 1_000_000
DEFAULT_MAX_CONCURRENCY = 2
CACHE_SECONDS = 30
DEFAULT_DAYS = 7
MONOTONIC: Callable[[], float] = time.monotonic

_FORBIDDEN_PARAMS = frozenset({"organization", "org"})
_KNOWN_PARAMS = frozenset({"from", "to", "as_of"})
Problem = Callable[..., Exception]


def _setting(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be a whole number of 1 or more")
    return value


@dataclass
class DashboardService:
    """The limits and the memory-only cache of one server process."""

    max_events: int
    max_concurrency: int
    slots: threading.BoundedSemaphore = field(init=False)
    cache: dict[tuple[str, str, str, str, str], tuple[float, dict[str, Any]]] = field(
        default_factory=dict
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    inflight: dict[tuple[str, str, str, str, str], threading.Event] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.slots = threading.BoundedSemaphore(self.max_concurrency)

    def cached(self, key: tuple[str, str, str, str, str]) -> dict[str, Any] | None:
        with self.lock:
            hit = self.cache.get(key)
            if hit is None:
                return None
            if hit[0] <= MONOTONIC():
                del self.cache[key]
                return None
            return hit[1]

    def keep(self, key: tuple[str, str, str, str, str], value: dict[str, Any]) -> None:
        now = MONOTONIC()
        with self.lock:
            for stale in [k for k, (until, _) in self.cache.items() if until <= now]:
                del self.cache[stale]
            self.cache[key] = (now + CACHE_SECONDS, value)


def _instant(text: str, name: str, problem: Problem) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise problem(400, "bad_window", f"{name} must be an ISO-8601 UTC instant") from None
    if moment.tzinfo is None:
        raise problem(400, "bad_window", f"{name} needs a UTC offset (for example Z)")
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        raise problem(400, "bad_window", f"{name} is out of range") from None


def _slot(moment: datetime) -> datetime:
    return moment.replace(second=moment.second // 30 * 30, microsecond=0)


def _window(request: Request, problem: Problem, now: datetime) -> formulas.Window:
    params = request.query_params
    for name in params:
        if name in _FORBIDDEN_PARAMS or name not in _KNOWN_PARAMS:
            raise problem(
                400, "bad_request", "the organization is the session's; no such parameter"
            )
    # no as_of: now, rounded down to its 30 s slot, so the default window repeats and is cached
    as_of = _instant(params["as_of"], "as_of", problem) if "as_of" in params else _slot(now)
    end = _instant(params["to"], "to", problem) if "to" in params else as_of
    start = (
        _instant(params["from"], "from", problem)
        if "from" in params
        else end - timedelta(days=DEFAULT_DAYS)
    )
    if not start < end:
        raise problem(400, "bad_window", "from must be before to")
    if end - start > timedelta(days=formulas.MAX_WINDOW_DAYS):
        raise problem(
            400, "window_too_long", f"a window is at most {formulas.MAX_WINDOW_DAYS} days"
        )
    return formulas.Window(start=start, end=end, as_of=as_of)


def install_dashboards(
    app: FastAPI,
    store: Store,
    need_config: Callable[[], NuraMeshConfig | None],
    caller: Callable[..., Caller],
    problem: Problem,
) -> DashboardService:
    """Add the four `/v1/dashboards/*` routes (GET only; the authorizer holds their rule)."""
    service = DashboardService(
        max_events=_setting("NURAMESH_DASHBOARD_MAX_EVENTS", DEFAULT_MAX_EVENTS),
        max_concurrency=_setting("NURAMESH_DASHBOARD_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY),
    )
    app.state.dashboards = service
    router = APIRouter(generate_unique_id_function=operation_id_for)
    Who = Annotated[Caller, Depends(caller)]

    def organization_of(who: Caller) -> str:
        org = who.org_id
        if org == LOCAL_INSPECTOR:  # authentication off: the one organization of this server
            config = need_config()
            org = None if config is None else config.org.id
        if org is None or not is_org(org):
            raise problem(403, "no_organization", "this credential has no single organization")
        return org

    def answer(route: str, request: Request, who: Who) -> dict[str, Any]:
        org = organization_of(who)
        window = _window(request, problem, store.now())
        rounded = _slot(window.as_of)
        key = (
            org,
            route,
            formulas.iso(window.start),
            formulas.iso(window.end),
            formulas.iso(rounded),
        )
        while True:  # one computation per key: a concurrent same-key request waits for it
            hit = service.cached(key)
            if hit is not None:
                return hit
            with service.lock:
                waiting_for = service.inflight.get(key)
                if waiting_for is None:
                    service.inflight[key] = mine = threading.Event()
                    break
            waiting_for.wait(timeout=60)
        try:
            if not service.slots.acquire(blocking=False):
                raise AuthProblem(
                    429,
                    "busy",
                    "too many dashboards are being computed; retry",
                    {"Retry-After": "5"},
                )
            try:
                scan = formulas.Scan(route, window, service.max_events)
                try:
                    for events in dashboards_reader.org_streams(
                        store, org, window, all_streams=route == "approvals"
                    ):
                        scan.add_stream(events)
                except dashboards_reader.DamagedRun as damaged:
                    raise problem(
                        409,
                        "damaged_run",
                        f"run {damaged.run_id} holds a stored time that is not real; "
                        "the dashboards cannot be answered until it is repaired",
                    ) from None
                result = scan.result(org)
            finally:
                service.slots.release()
            service.keep(key, result)
        finally:
            with service.lock:
                service.inflight.pop(key, None)
            mine.set()
        return result

    for route in formulas.ROUTES:

        def make(name: str) -> Callable[..., dict[str, Any]]:
            def view(request: Request, who: Who) -> dict[str, Any]:
                return answer(name, request, who)

            view.__name__ = "dashboard_" + name.replace("-", "_")
            view.__doc__ = f"The {name} dashboard of the caller's organization (read only)."
            return view

        router.add_api_route(f"/v1/dashboards/{route}", make(route), methods=["GET"])

    # plain routes, as `install_costs` does (the authorizer and the route-table test read paths)
    app.router.routes.extend(router.routes)
    return service


__all__ = ["DashboardService", "EventType", "install_dashboards"]
