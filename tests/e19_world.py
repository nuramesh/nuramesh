"""A small world for the E19 engine tests: a store with a movable clock, an organization with the
roles of a scenario, and a way to seed a scenario's streams and compare what was written
(section 14)."""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from e19_support import at

from nuramesh.core.breakers import BreakerRule
from nuramesh.core.gates import Criterion, DefinitionOfDone, GateKind, GateSpec
from nuramesh.core.ledger import CostBasis, EventType, LedgerEvent
from nuramesh.core.money import Money
from nuramesh.core.org import Agent, OrgGraph, Role, RoleKind
from nuramesh.core.tasks import Task, TaskStatus
from nuramesh.stores import EventDraft, MemoryStore, Store

HUMAN = "human:ann"
START = at("2026-11-01T00:00:00Z")


class Clock:
    """A clock the test moves; `Store(clock=...)` stamps new entries with it."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def org_for(roles: Iterable[str] = ("researcher", "writer", "verifier")) -> OrgGraph:
    """An organization with a chief of staff and one agent `<role>-1` per role."""
    names = list(dict.fromkeys(roles))
    return OrgGraph(
        id="acme",
        name="Acme",
        roles=(
            Role(id="cos", name="Chief of Staff", kind=RoleKind.CHIEF_OF_STAFF),
            *(
                Role(
                    id=r,
                    name=r.title(),
                    kind=RoleKind.VERIFIER if r == "verifier" else RoleKind.WORKER,
                    default_model="fake/worker-1",
                )
                for r in names
            ),
        ),
        agents=(
            Agent(id="boss", name="Boss", role="cos"),
            *(Agent(id=f"{r}-1", name=f"{r}-1", role=r, reports_to="boss") for r in names),
        ),
    )


def running_task(task_id: str, run_id: str, role: str) -> Task:
    """A task of `<role>-1` that is in progress."""
    return Task(
        id=task_id,
        run_id=run_id,
        title=f"Task {task_id}",
        definition_of_done=DefinitionOfDone(
            criteria=(Criterion(id="ok", text="ok"),),
            gates=(GateSpec(id="g", kind=GateKind.RULE, checks=("ok",), config={"rules": []}),),
        ),
        assignee=f"{role}-1",
        status=TaskStatus.IN_PROGRESS,
        attempt=1,
    )


class World:
    """A store, a clock and an organization, seeded from a scenario."""

    def __init__(
        self,
        roles: Iterable[str] = ("researcher", "writer", "verifier"),
        store_factory: Callable[[Clock], Store] | None = None,
    ) -> None:
        self.clock = Clock()
        self.store: Store = (
            store_factory(self.clock) if store_factory else MemoryStore(clock=self.clock)
        )
        self.org = org_for(roles)

    def append(self, stream: str, raw: dict[str, Any], default_actor: str = "system") -> None:
        """Append a scenario entry at its own time (a missing actor is `system`)."""
        previous = self.clock.now
        self.clock.now = at(raw["at"]) if "at" in raw else self.clock.now
        cost = raw.get("cost")
        try:
            with self.store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=stream,
                        type=EventType(raw["type"]),
                        actor=raw.get("actor", default_actor),
                        payload=raw.get("payload", {}),
                        cost=None if cost is None else Money.of(cost),
                        cost_basis=None if cost is None else CostBasis.ACTUAL,
                    )
                )
        finally:
            self.clock.now = max(previous, self.clock.now)

    def start_run(self, run_id: str, first_at: str | None = None) -> None:
        raw: dict[str, Any] = {"type": "run.started", "payload": {"org": "acme"}}
        if first_at:
            raw["at"] = first_at
        self.append(f"run:{run_id}", raw)

    def seed(self, case: dict[str, Any]) -> None:
        """The org stream, the run entries (`events`, run `r1`) and the open runs of a scenario."""
        for raw in sorted(case.get("org_stream", []), key=lambda r: r.get("at", "")):
            self.append("org:acme", raw)
        events = sorted(case.get("events", []), key=lambda r: r["at"])
        if events:
            self.start_run("r1", events[0]["at"])
            for raw in events:
                self.append("run:r1", raw)
        open_ids = [i if isinstance(i, str) else i["run"] for i in case.get("open_runs", [])]
        if events and "r1" not in open_ids:  # the scenario's entries come from a finished run
            self.append("run:r1", {"type": "run.ended", "at": events[-1]["at"], "payload": {}})
        for item in case.get("open_runs", []):
            run = item if isinstance(item, str) else item["run"]
            if not events or run != "r1":
                self.start_run(run)
            if not isinstance(item, str):
                for t in item.get("running_tasks", []):
                    with self.store.transaction() as uow:
                        uow.put_task(running_task(t["task"], run, t["role"]), expected_version=None)

    def mark(self) -> dict[str, int]:
        """Where every stream stands now, to tell later what was written."""
        return {s: h.seq for s, h in self.store.heads().items()}

    def written_since(self, mark: dict[str, int]) -> list[LedgerEvent]:
        """The entries appended after `mark`, org stream first then run streams."""
        out: list[LedgerEvent] = []
        for stream in sorted(self.store.streams(), key=lambda s: (not s.startswith("org:"), s)):
            out.extend(self.store.read_stream(stream, after_seq=mark.get(stream, 0), limit=10_000))
        return out


def rule_set(raw: list[dict[str, Any]]) -> list[BreakerRule]:
    """The rules of a scenario."""
    return [BreakerRule.model_validate(r) for r in raw]


def describe(e: LedgerEvent) -> str:
    """A written entry in the scenarios' words, for failure messages."""
    return f"{e.type}:{e.stream}:{e.actor}:{dict(e.payload)}"


def matches(e: LedgerEvent, expected: str) -> bool:
    """Whether a ledger entry is the one `expected` describes (`type:positional,key=value,...`)."""
    kind, _, rest = expected.partition(":")
    if e.type != kind:
        return False
    parts = rest.split(",") if rest else []
    for part in parts:
        if "=" not in part:
            if kind == "breaker.tripped" and e.payload.get("rule") != part:
                return False
            if kind == "run.stop_requested" and e.stream != f"run:{part}":
                return False
            if kind == "task.stop_requested":
                run, _, task = part.partition("/")
                if e.stream != f"run:{run}" or e.payload.get("task") != task:
                    return False
            if kind not in ("breaker.tripped", "run.stop_requested", "task.stop_requested"):
                return False
            continue
        key, _, value = part.partition("=")
        if key == "actor":
            if e.actor != value:
                return False
            continue
        have = e.payload.get(key)
        if isinstance(have, bool):
            have = str(have).lower()
        if str(have) != value:
            return False
    return True


def writes_match(written: list[LedgerEvent], expected: list[str]) -> bool:
    """Whether `written` is exactly the entries `expected` describes (any order)."""
    pool = list(written)
    for want in expected:
        hit = next((e for e in pool if matches(e, want)), None)
        if hit is None:
            return False
        pool.remove(hit)
    return not pool
