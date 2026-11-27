"""A tiny ledger builder for the earned-autonomy tests (section 9).

Entries get increasing commit positions in the order they are added, so "committed after the
grant" is the order of the calls. Only the fields the autonomy maths reads are meaningful; the
hashes are placeholders (the maths never verifies a chain)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from nuramesh.core.autonomy import Entry
from nuramesh.core.ledger import CostBasis, LedgerEvent, format_time
from nuramesh.core.money import Money

T = datetime(2026, 10, 20, tzinfo=UTC)
ORG = "org:o1"
RUN = "run:r1"
ZERO = "0" * 64
SUITES = {"dev": frozenset({"smoke"})}


def d(days: float = 0, seconds: float = 0) -> datetime:
    """`T` plus a (usually negative) offset."""
    return T + timedelta(days=days, seconds=seconds)


def event(
    stream: str,
    seq: int,
    type_: str,
    at: datetime,
    payload: Mapping[str, Any],
    *,
    task: str | None = None,
    actor: str = "system",
    cost: Money | None = None,
    cost_basis: CostBasis | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        stream=stream,
        seq=seq,
        type=type_,
        actor=actor,
        at=at,
        run_id=stream.removeprefix("run:") if stream.startswith("run:") else None,
        task_id=task,
        payload=dict(payload),
        cost=cost,
        cost_basis=cost_basis,
        prev_hash=ZERO,
        hash="1" * 64,
    )


class World:
    """Entries with commit positions; `config` and `grant` write the spec's `decision.recorded`s."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self._seq: dict[str, int] = {}

    def add(
        self,
        stream: str,
        type_: str,
        at: datetime,
        payload: Mapping[str, Any],
        *,
        task: str | None = None,
        actor: str = "system",
        cost: Money | None = None,
        cost_basis: CostBasis | None = None,
    ) -> Entry:
        seq = self._seq[stream] = self._seq.get(stream, 0) + 1
        item = Entry(
            len(self.entries) + 1,
            event(
                stream,
                seq,
                type_,
                at,
                payload,
                task=task,
                actor=actor,
                cost=cost,
                cost_basis=cost_basis,
            ),
        )
        self.entries.append(item)
        return item

    def decision(self, at: datetime, payload: Mapping[str, Any]) -> Entry:
        return self.add(ORG, "decision.recorded", at, payload)

    def config(
        self,
        at: datetime,
        *,
        roles: Mapping[str, Sequence[str] | None] | None = None,
        agents: Mapping[str, tuple[str, str]] | None = None,
        contract: int = 3,
    ) -> Entry:
        return self.decision(at, config_record(roles, agents, contract))

    def grant(
        self,
        grant_id: str = "g",
        *,
        subject: tuple[str, str] = ("role", "dev"),
        at: datetime | None = None,
        days: int = 30,
        key: int = 1,
        rule_hash: str = "h1",
        contract: int = 3,
        role: str = "dev",
        kind: str = "run_command",
    ) -> Entry:
        granted = at if at is not None else d(-10)
        return self.decision(
            granted,
            {
                "kind": "autonomy_grant",
                "grant_id": grant_id,
                "subject_kind": subject[0],
                "subject_id": subject[1],
                "role": role,
                "rule_key": key,
                "rule_hash": rule_hash,
                "action_kind": kind,
                "policy_contract": contract,
                "granted_at": format_time(granted),
                "expires_at": format_time(granted + timedelta(days=days)),
            },
        )

    def ceiling(
        self, at: datetime, new: str, role: str = "dev", kind: str = "run_command"
    ) -> Entry:
        return self.decision(
            at,
            {"kind": "autonomy_ceiling_set", "role": role, "action_kind": kind, "new": new},
        )

    def verdict(
        self, at: datetime, passed: bool, *, kind: str = "test_command", outcome: str | None = None
    ) -> Entry:
        return self.add(
            RUN,
            "gate.completed",
            at,
            {"gate": "g1", "kind": kind, "outcome": outcome or ("passed" if passed else "failed")},
            task="t1",
        )

    def assign(self, at: datetime, agent: str, task: str = "t1") -> Entry:
        return self.add(
            RUN, "task.transitioned", at, {"to": "verifying", "assignee": agent}, task=task
        )


# The policy of spec 9.4: rule `hN` is at key N (key 0 is a filler).
# Each value is `(kind, decision, never-matching shape)`.
KINDS: dict[str, tuple[str, str, bool]] = {
    "h0": ("use_tool", "deny", False),
    "h1": ("run_command", "ask", False),
    "h2": ("write_file", "ask", False),
    "h3": ("spend", "ask", False),
    "h4": ("merge", "ask", False),
    "h5": ("run_command", "deny", False),
    "h6": ("use_tool", "ask", False),
    "h7": ("external_message", "ask", False),
    "h8": ("run_command", "ask", False),
    "h9": ("run_command", "allow", False),
    "h10": ("run_command", "ask", False),
    "h11": ("run_command", "ask", True),
}
POLICY = [f"h{i}" for i in range(12)]


def config_record(
    roles: Mapping[str, Sequence[str] | None] | None = None,
    agents: Mapping[str, tuple[str, str]] | None = None,
    contract: int = 3,
) -> dict[str, Any]:
    base_roles = {"dev": ["h0", "h1", "h2"], "rev": ["h0", "h1"]} if roles is None else roles
    base_agents = (
        {"a1": ("dev", "m1"), "a2": ("dev", "m1"), "w1": ("rev", "m1")}
        if agents is None
        else agents
    )
    return {
        "kind": "config_loaded",
        "config_hash": "c" * 64,
        "policy_contract": contract,
        "roles": {k: None if v is None else list(v) for k, v in base_roles.items()},
        "agents": {a: {"role": r, "model": m} for a, (r, m) in base_agents.items()},
        "rule_kinds": {
            k: None if v is None else [list(KINDS.get(h, ("use_tool", "ask", False))) for h in v]
            for k, v in base_roles.items()
        },
    }
