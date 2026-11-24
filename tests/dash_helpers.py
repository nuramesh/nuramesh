"""Small ledgers for the R2.11 dashboard cases (spec/r211): a memory store with a settable clock."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nuramesh.core.ledger import CostBasis, EventType, LedgerEvent
from nuramesh.core.money import Money
from nuramesh.stores import EventDraft, MemoryStore

ORG = "acme"
AS_OF = datetime(2026, 1, 8, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class World:
    """Appends ledger entries to run streams at chosen times."""

    def __init__(self, org: str = ORG) -> None:
        self.now = datetime(2000, 1, 1, tzinfo=UTC)
        self.store = MemoryStore(clock=lambda: self.now)
        self.org = org
        self.started: set[str] = set()

    def add(
        self,
        run: str,
        type_: EventType,
        at: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task: str | None = None,
        actor: str = "system",
        cost: str | None = None,
        basis: str | None = None,
    ) -> None:
        self.now = utc(at)
        with self.store.transaction() as uow:
            if run not in self.started:
                self.started.add(run)
                uow.append(
                    EventDraft(
                        stream=f"run:{run}",
                        type=EventType.RUN_STARTED,
                        actor="system",
                        run_id=run,
                        payload={"org": self.org},
                    )
                )
            uow.append(
                EventDraft(
                    stream=f"run:{run}",
                    type=type_,
                    actor=actor,
                    run_id=run,
                    task_id=task,
                    payload=dict(payload or {}),
                    cost=None if cost is None else Money.of(cost),
                    cost_basis=None if basis is None else CostBasis(basis),
                )
            )

    def gate(
        self,
        run: str,
        at: str,
        outcome: str | None,
        *,
        gate: str = "g1",
        kind: str | None = "verifier",
        task: str | None = "t1",
        role: str | None = None,
        attempt: int = 1,
        submission: int = 1,
        start_at: str | None = None,
    ) -> None:
        """A `gate.completed` (and, when `kind` is given, its `gate.started` at `start_at`)."""
        key: dict[str, Any] = {"gate": gate, "attempt": attempt, "submission": submission}
        if kind is not None:
            self.add(
                run,
                EventType.GATE_STARTED,
                start_at or at,
                {**key, "kind": kind},
                task=task,
            )
        if outcome is not None:
            extra = {"role": role} if role is not None else {}
            self.add(
                run,
                EventType.GATE_COMPLETED,
                at,
                {**key, "outcome": outcome, **extra},
                task=task,
            )

    def call(
        self,
        run: str,
        at: str,
        *,
        cost: str | None = None,
        basis: str | None = None,
        model: str = "m1",
        role: str | None = None,
        latency: Any = None,
    ) -> None:
        payload: dict[str, Any] = {
            "agent": "dev1",
            "provider": "openai",
            "model": model,
            "outcome": "ok",
        }
        if role is not None:
            payload["role"] = role
        if latency is not None:
            payload["latency_ms"] = latency
        self.add(run, EventType.MODEL_CALLED, at, payload, cost=cost, basis=basis)

    def done(self, run: str, task: str, at: str) -> None:
        self.add(
            run,
            EventType.TASK_TRANSITIONED,
            at,
            {"to": "done", "closed_attempt": False, "assignee": None},
            task=task,
        )

    def ask(self, run: str, at: str, approval: str, action: str = "spend") -> None:
        self.add(
            run,
            EventType.APPROVAL_REQUESTED,
            at,
            {"approval": approval, "action": action, "why": "SENTINEL-why-2c9"},
        )

    def decide(self, run: str, at: str, approval: str, approved: bool) -> None:
        self.add(
            run,
            EventType.APPROVAL_DECIDED,
            at,
            {"approval": approval, "approved": approved, "by": "human:SENTINEL-ann"},
        )

    def end(self, run: str, at: str) -> None:
        self.add(run, EventType.RUN_ENDED, at, {"counts": {}})

    def streams(self) -> list[tuple[str, list[LedgerEvent]]]:
        heads = self.store.heads()
        out: list[tuple[str, list[LedgerEvent]]] = []
        for stream in sorted(heads):
            out.append((stream, list(self.store.read_stream(stream, limit=100000))))
        return out


def dec(text: str) -> Decimal:
    return Decimal(text)
