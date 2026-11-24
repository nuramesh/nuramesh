"""Build a store, a configuration and a client from the cost spec's cases (spec/costs)."""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.ledger import CostBasis, EventType
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.prices import Price, PriceBook, PriceOverride
from nuramesh.stores import EventDraft, MemoryStore, Store

CASES = json.loads((Path(__file__).resolve().parents[1] / "spec/costs/cases.json").read_text())
ORG_ID = "acme"
FAR_PAST = "0001-01-01T00:00:00Z"
FAR_FUTURE = "9999-12-31T00:00:00Z"
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Clock:
    """A settable clock for the store, so a case can write an event at a chosen time."""

    def __init__(self) -> None:
        self.now = EPOCH

    def __call__(self) -> datetime:
        return self.now


def make_override(provider: str, model: str, set_at: datetime) -> PriceOverride:
    return PriceOverride(
        provider=provider,
        model=model,
        input_per_million=Decimal("1"),
        output_per_million=Decimal("2"),
        set_by="human:u_a1b2",
        set_at=set_at,
        reason="test override",
    )


def make_config(
    roles: Mapping[str, str],
    prices: Sequence[Price] = (),
    org_id: str = ORG_ID,
    overrides: Sequence[PriceOverride] = (),
) -> NuraMeshConfig:
    role_ids = sorted(set(roles.values()) | {"cos"})
    return NuraMeshConfig(
        org=OrgGraph(
            id=org_id,
            name="Acme",
            roles=tuple(
                Role(id=r, name=r, kind=RoleKind.CHIEF_OF_STAFF)
                if r == "cos"
                else Role(id=r, name=r)
                for r in role_ids
            ),
            agents=(
                Agent(id="boss", name="Boss", role="cos"),
                *(Agent(id=a, name=a, role=r, reports_to="boss") for a, r in sorted(roles.items())),
            ),
        ),
        prices=PriceBook(prices=tuple(prices)),
        price_overrides=tuple(overrides),
    )


def make_price(provider: str, model: str, effective: datetime) -> Price:
    return Price(
        provider=provider,
        model=model,
        input_per_million=Decimal("1"),
        output_per_million=Decimal("2"),
        effective_from=effective,
        source="test price list",
    )


class Writer:
    """Appends events to a store at chosen times."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store, self.clock = store, clock
        self.started: set[str] = set()

    def append(self, draft: EventDraft, at: datetime) -> None:
        self.clock.now = at
        with self.store.transaction() as uow:
            uow.append(draft)

    def start(self, run: str, at: datetime, org: str = ORG_ID) -> None:
        if run in self.started:
            return
        self.started.add(run)
        self.append(
            EventDraft(
                stream=f"run:{run}",
                type=EventType.RUN_STARTED,
                actor="system",
                run_id=run,
                payload={"org": org},
            ),
            at,
        )

    def call(
        self,
        run: str,
        task: str | None,
        at: datetime,
        *,
        agent: str,
        provider: str,
        model: str,
        outcome: str = "ok",
        cost: str | None = None,
        basis: str | None = None,
        role: str | None = None,
        price: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        endpoint_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "agent": agent,
            "provider": provider,
            "model": model,
            "outcome": outcome,
        }
        if usage is not None:
            payload["usage"] = dict(usage)
        if endpoint_id is not None:
            payload["endpoint_id"] = endpoint_id
        if role is not None:
            payload["role"] = role
        if price is not None:
            payload["price"] = dict(price)
        self.append(
            EventDraft(
                stream=f"run:{run}",
                type=EventType.MODEL_CALLED,
                actor=f"agent:{agent}",
                run_id=run,
                task_id=task,
                payload=payload,
                cost=None if cost is None else Money.of(cost),
                cost_basis=None if basis is None else CostBasis(basis),
            ),
            at,
        )

    def move(
        self,
        run: str,
        task: str,
        at: datetime,
        to: str,
        *,
        closed_attempt: bool = False,
        actor: str = "system",
        assignee: str | None = None,
    ) -> None:
        self.append(
            EventDraft(
                stream=f"run:{run}",
                type=EventType.TASK_TRANSITIONED,
                actor=actor,
                run_id=run,
                task_id=task,
                payload={"to": to, "closed_attempt": closed_attempt, "assignee": assignee},
            ),
            at,
        )

    def late(self, run: str, at: datetime) -> None:
        """An entry that is no cost fact, written after the run ended (a reconciler's release)."""
        self.append(
            EventDraft(
                stream=f"run:{run}",
                type=EventType.BUDGET_RELEASED,
                actor="system",
                run_id=run,
                payload={},
            ),
            at,
        )

    def end(self, run: str, at: datetime, aborted: bool = False) -> None:
        payload: dict[str, Any] = {"counts": {}}
        if aborted:
            payload.update({"aborted": True, "error": "Boom"})
        self.append(
            EventDraft(
                stream=f"run:{run}",
                type=EventType.RUN_ENDED,
                actor="system",
                run_id=run,
                payload=payload,
            ),
            at,
        )


def costs_case_store(case: Mapping[str, Any]) -> tuple[Store, NuraMeshConfig]:
    """The events of a `costs` case, written in ledger order with their own times."""
    store, config, _ = costs_case_world(case)
    return store, config


def costs_case_world(case: Mapping[str, Any]) -> tuple[Store, NuraMeshConfig, Writer]:
    clock = Clock()
    store = MemoryStore(clock=clock)
    writer = Writer(store, clock)
    first: dict[str, datetime] = {}
    for e in case["events"]:
        at = utc(e["at"])
        first[e["run"]] = min(first.get(e["run"], at), at)
    for run, at in first.items():
        writer.start(run, at)
    for e in case["events"]:
        at = utc(e["at"])
        if e["type"] == "model.called":
            writer.call(
                e["run"],
                e["task"],
                at,
                agent=e["agent"],
                provider=e["provider"],
                model=e["model"],
                outcome=e["outcome"],
                cost=e["cost"],
                basis=e["basis"],
                role=e.get("role"),
            )
        else:
            writer.move(
                e["run"],
                e["task"],
                at,
                e["to"],
                closed_attempt=e["closed_attempt"],
                actor=e["actor"],
            )
    return store, make_config(case["roles"]), writer


def quote_case_store(case: Mapping[str, Any]) -> tuple[Store, NuraMeshConfig]:
    clock = Clock()
    store = MemoryStore(clock=clock)
    writer = Writer(store, clock)
    for run in case["runs"]:
        rid = run["id"]
        tick = [0]

        def when(tick: list[int] = tick) -> datetime:
            tick[0] += 1
            return EPOCH + timedelta(seconds=tick[0])

        writer.start(rid, when(), org=run["org"])
        for t in run["tasks"]:
            writer.move(rid, t["id"], when(), t["final"], assignee=t["assignee"])
        agent = run["tasks"][0]["assignee"] if run["tasks"] else "dev1"
        for c in run["calls"]:
            version = (
                utc(c["price_at"] + "T00:00:00Z")
                if "T" not in c["price_at"]
                else utc(c["price_at"])
            )
            writer.call(
                rid,
                run["tasks"][0]["id"] if run["tasks"] else None,
                when(),
                agent=agent,
                provider=c["provider"],
                model=c["model"],
                cost=c["cost"],
                basis="unknown" if c["cost"] is None else "actual",
                price={"at": EPOCH.isoformat(), "effective_from": version.isoformat()},
            )
        if run["ended"] is not None:
            writer.end(rid, utc(run["ended"]), aborted=run["aborted"])
        for late in run.get("late", []):  # entries written after `run.ended` (spec/costs 7)
            writer.late(rid, utc(late["at"]))
    prices = []
    for key, day in case["current_prices"].items():
        provider, model = key.split("/")
        prices.append(make_price(provider, model, utc(day + "T00:00:00Z")))
    overrides = []
    for key, day in case.get("current_overrides", {}).items():
        provider, model = key.split("/")
        overrides.append(make_override(provider, model, utc(day + "T00:00:00Z")))
    clock.now = max(clock.now, datetime.now(UTC))  # a quote without `as_of` reads the store's now
    return store, make_config(case["roles"], prices, overrides=overrides)


def without(d: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in keys}


Check = Callable[[Any, Any], None]
