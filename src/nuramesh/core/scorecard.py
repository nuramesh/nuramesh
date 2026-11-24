"""Agent scorecards (E14): a pure read model of the ledger.

A scorecard is a function of the organization's ledger entries (the org stream and its runs'
streams, each with its commit position), the configuration recorded at `as_of`, the subject, the
period and `as_of`. It reads; it appends nothing. Ratios are rounded once, at the end, half to
even (`round4`, `round6`); sums are exact; a number that cannot be known is `None`, never a guess.

The caller supplies what is not in the ledger entries themselves: the eval rows (`check_eligibility`
needs the suites) and the gate kind of a verdict (`autonomy.GateKindOf`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from nuramesh.core.autonomy import (
    DEFAULT_SETTINGS,
    Config,
    Entry,
    GateKindOf,
    Settings,
    Verdict,
    decision_kind,
    entry_id,
    grant_states,
    parse_config,
    payload_gate_kind,
    verdicts_of,
)

SCORECARD_VERSION = 1
DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 366
DEFAULT_MIN_RESULTS = 20
CURRENCY = "USD"
GATE_KINDS = ("test_command", "rule", "schema", "custom")


class ScorecardError(Exception):
    """A request that is not valid (400) or a subject that does not exist (404)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


def round4(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))


def round6(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN))


SOLO_MODE = "solo"
"""`approval_mode` of a solo decision (SA-14). An entry with no mode
was written before modes existed: it is never solo and is counted in the totals (SA-18)."""


def solo_share(solo: int, approved: int) -> str | None:
    """SA-18: `round4(solo / approved)`, rounded once from the exact ratio; None when nothing
    was approved (a share of nothing is not known, never a guess)."""
    if approved == 0:
        return None
    return round4(Decimal(solo) / Decimal(approved))


def config_at(entries: Sequence[Entry], as_of: datetime) -> Config | None:
    """The configuration recorded by the latest `config_loaded` entry (ledger order) with
    `at <= as_of`; None before the first one (spec 2)."""
    found: Config | None = None
    for item in sorted(entries, key=lambda e: e.pos):
        if item.event.at <= as_of and decision_kind(item.event) == "config_loaded":
            parsed = parse_config(item)
            if parsed is not None:
                found = parsed
    return found


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime | None  # None: up to and including `as_of`
    as_of: datetime

    def contains(self, at: datetime) -> bool:
        if at > self.as_of or at < self.start:
            return False
        return self.end is None or at < self.end


def make_period(
    start: datetime | None, end: datetime | None, as_of: datetime, min_results: int
) -> tuple[Period, int]:
    """Validate the request's period and `min_results` (400)."""
    if not 1 <= min_results <= 1000:
        raise ScorecardError(400, "invalid_request", "min_results is between 1 and 1000")
    begin = start if start is not None else as_of - timedelta(days=DEFAULT_PERIOD_DAYS)
    stop = end if end is not None else as_of
    if begin >= stop:
        raise ScorecardError(400, "invalid_period", "from must be before to")
    if stop - begin > timedelta(days=MAX_PERIOD_DAYS):
        raise ScorecardError(400, "invalid_period", f"a period is at most {MAX_PERIOD_DAYS} days")
    return Period(begin, end, as_of), min_results


@dataclass(frozen=True)
class SubjectScope:
    """Who counts as the subject: an agent, or every agent of a role (at `as_of`)."""

    kind: str
    id: str
    role: str
    agents: frozenset[str]

    def has(self, agent: str | None) -> bool:
        return agent is not None and agent in self.agents


def scope_of(config: Config | None, kind: str, subject: str) -> SubjectScope:
    """The subject's scope under the configuration at `as_of`; 404 `not_found` if absent."""
    if config is None:
        raise ScorecardError(404, "not_found", f"{kind} '{subject}' is not in the organization")
    if kind == "agent":
        found = config.agents.get(subject)
        if found is None:
            raise ScorecardError(404, "not_found", f"agent '{subject}' is not in the organization")
        return SubjectScope("agent", subject, found[0], frozenset({subject}))
    if kind == "role":
        if subject not in config.roles:
            raise ScorecardError(404, "not_found", f"role '{subject}' is not in the organization")
        return SubjectScope("role", subject, subject, config.members(subject))
    raise ScorecardError(400, "invalid_request", "kind is agent or role")


def _counts() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "error": 0}


def _money(value: Decimal) -> str:
    return format(value, "f")


def _call_cost(event: Any) -> tuple[str, Decimal | None, bool]:
    """`(class, amount, provisional)` of a `model.called` entry: class is `known`, `unknown` or
    `none`."""
    outcome = event.payload.get("outcome")
    basis = str(event.cost_basis) if event.cost_basis is not None else None
    if outcome == "settle_failed":
        return "unknown", None, basis == "provisional"
    if outcome in ("ok", "settled_late"):
        if event.cost is not None:
            return "known", event.cost.amount, basis == "provisional"
        if basis is not None:
            return "unknown", None, basis == "provisional"
    return "none", None, False


def scorecard(
    entries: Sequence[Entry],
    *,
    kind: str,
    subject: str,
    period: Period,
    min_results: int,
    evals: Sequence[Mapping[str, Any]] = (),
    required_suites: Mapping[str, frozenset[str]] | None = None,
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings = DEFAULT_SETTINGS,
) -> dict[str, Any]:
    """The scorecard (version 1) as a JSON-ready dict."""
    as_of = period.as_of
    ordered = sorted((e for e in entries if e.event.at <= as_of), key=lambda e: e.pos)
    config = config_at(entries, as_of)
    scope = scope_of(config, kind, subject)

    # -- gates and the pass rate (3.2, 3.3)
    gate = {k: _counts() for k in ("gate", "verifier")}
    human_ignored = 0
    for verdict in verdicts_of(ordered, gate_kind_of):
        if not period.contains(verdict.at) or not scope.has(verdict.agent):
            continue
        bucket = "verifier" if verdict.gate_kind == "verifier" else "gate"
        gate[bucket]["passed" if verdict.passed else "failed"] += 1
    assignee_of = _AssigneeTracker()
    for item in ordered:
        event = item.event
        if event.type == "task.transitioned":
            assignee_of.note(event)
        elif event.type == "gate.completed":
            who = assignee_of.current(event)
            outcome = event.payload.get("outcome")
            if not period.contains(event.at) or not scope.has(who):
                continue
            gkind = gate_kind_of(event)
            if gkind == "human":
                human_ignored += 1
            elif outcome == "error" and gkind is not None:
                gate["verifier" if gkind == "verifier" else "gate"]["error"] += 1
    passed = gate["gate"]["passed"] + gate["verifier"]["passed"]
    failed = gate["gate"]["failed"] + gate["verifier"]["failed"]
    verdict_count = passed + failed
    sufficient = verdict_count >= min_results
    pass_rate = round4(Decimal(passed) / Decimal(verdict_count)) if sufficient else None

    # -- tasks and cost (3.3)
    moves = _moves(ordered)
    last_move: dict[tuple[str, str], _Move] = {}
    results: dict[tuple[str, str], _Move] = {}
    cancelled_human = cancelled_system = 0
    for move in moves:
        key = (move.run, move.task)
        last_move[key] = move
        if not period.contains(move.at):
            continue
        if move.to == "done" and scope.has(move.assignee):
            results[key] = move
        elif move.to == "cancelled" and scope.has(move.held_by):
            if move.actor.startswith("human:"):
                cancelled_human += 1
            else:
                cancelled_system += 1
    done_as_of = {k for k, m in last_move.items() if m.to == "done"}
    known_total = Decimal(0)
    retry_total = Decimal(0)
    unknown = provisional = 0
    unaccepted = Decimal(0)
    closed_at: dict[tuple[str, str], int] = {}
    for move in moves:
        if move.closed_attempt:
            closed_at[(move.run, move.task)] = move.seq
    for item in ordered:
        event = item.event
        if event.type != "model.called":
            continue
        agent = event.payload.get("agent")
        role = event.payload.get("role")
        if role is None and isinstance(agent, str) and config is not None:
            role = config.agents.get(agent, (None, None))[0]
        mine = (agent == scope.id) if scope.kind == "agent" else (role == scope.id)
        if not mine:
            continue
        cls, amount, is_provisional = _call_cost(event)
        if cls == "none":
            continue
        key = (event.run_id or "", event.task_id or "")
        if event.task_id is not None and key in results:
            if cls == "known":
                assert amount is not None
                known_total += amount
                if key in closed_at and event.seq < closed_at[key]:
                    retry_total += amount
            else:
                unknown += 1
            provisional += is_provisional
        if cls == "known" and period.contains(event.at):
            assert amount is not None
            if event.task_id is None or key not in done_as_of:
                unaccepted += amount
    n = len(results)
    complete = unknown == 0
    ratio = (known_total / Decimal(n)) if n else None
    cost = {
        "currency": CURRENCY,
        "results": n,
        "known_total": _money(known_total),
        "per_result": round6(ratio) if ratio is not None and complete else None,
        "per_result_at_least": round6(ratio) if ratio is not None and not complete else None,
        "unknown_calls": unknown,
        "provisional_calls": provisional,
        "retry_known_total": _money(retry_total),
        "unaccepted_known_total": _money(unaccepted),
    }

    # -- approvals and safety
    requested: dict[str, datetime] = {}
    decided_at: dict[str, datetime] = {}
    approved = denied = blocked = breached = 0
    approved_solo = denied_solo = 0  # subsets of `approved` and `denied` (SA-18 item 1)
    for item in ordered:
        event, p = item.event, item.event.payload
        who = p.get("agent")
        if event.type == "approval.requested" and period.contains(event.at) and scope.has(who):
            requested[str(p.get("approval"))] = event.at
        elif event.type == "approval.decided":
            decided_at.setdefault(str(p.get("approval")), event.at)
            if period.contains(event.at) and scope.has(who):
                solo = p.get("approval_mode") == SOLO_MODE
                approved += p.get("approved") is True
                denied += p.get("approved") is False
                approved_solo += solo and p.get("approved") is True
                denied_solo += solo and p.get("approved") is False
        elif event.type == "model.blocked" and period.contains(event.at) and scope.has(who):
            blocked += 1
        elif event.type == "budget.breached" and period.contains(event.at) and scope.has(who):
            breached += 1
    pending = sum(1 for a in requested if a not in decided_at)

    # -- autonomy
    states = grant_states(
        entries,
        as_of,
        required_suites=required_suites or {},
        gate_kind_of=gate_kind_of,
        settings=settings,
    )
    own = [
        s
        for s in states.values()
        if (
            s.grant.role == scope.id
            if scope.kind == "role"
            else (
                s.grant.subject_id == scope.id
                if s.grant.agent_level
                else s.grant.subject_id == scope.role and s.grant.role == scope.role
            )
        )
    ]
    by_entry = {entry_id(e.event): e.event for e in ordered}
    demotions = []
    for state in own:
        if state.state == "demoted" and state.trigger_entry in by_entry:
            demotions.append((by_entry[state.trigger_entry].at, state))
    demotions.sort(key=lambda d: d[0])
    last = (
        None
        if not demotions
        else {
            "at": _iso(demotions[-1][0]),
            "grant_id": demotions[-1][1].grant.id,
            "trigger": demotions[-1][1].trigger,
        }
    )
    return {
        "scorecard_version": SCORECARD_VERSION,
        "subject": {"kind": scope.kind, "id": scope.id},
        "role": scope.role,
        "period": {
            "from": _iso(period.start),
            "to": _iso(period.end if period.end is not None else as_of),
            "to_inclusive": period.end is None,
        },
        "as_of": _iso(as_of),
        "min_results": min_results,
        "gates": {**gate, "human_ignored": human_ignored},
        "verdicts": verdict_count,
        "passed": passed,
        "pass_rate": pass_rate,
        "sufficient": sufficient,
        "tasks": {
            "done": n,
            "cancelled_by_human": cancelled_human,
            "cancelled_by_system": cancelled_system,
        },
        "cost": cost,
        "evals": [dict(row) for row in evals],
        "approvals": {
            "requested": len(requested),
            "approved": approved,
            "denied": denied,
            "pending": pending,
            # ND396, additive and outside every hash; `scorecard_version` stays 1
            "approved_solo": approved_solo,
            "denied_solo": denied_solo,
            "solo_share": solo_share(approved_solo, approved),
        },
        "safety": {"model_blocked": blocked, "budget_breached": breached},
        "autonomy": {
            "active_grants": sorted(s.grant.id for s in own if s.active),
            "last_demotion": last,
        },
    }


def _iso(moment: datetime) -> str:
    from nuramesh.core.ledger import format_time

    return format_time(moment)


@dataclass(frozen=True)
class _Move:
    run: str
    task: str
    seq: int
    at: datetime
    to: str
    actor: str
    assignee: str | None
    held_by: str | None
    closed_attempt: bool


class _AssigneeTracker:
    """The assignee of a task at a point in the run (the latest move before it)."""

    def __init__(self) -> None:
        self._by_task: dict[tuple[str, str], str | None] = {}

    def note(self, event: Any) -> None:
        if event.run_id is not None and event.task_id is not None:
            who = event.payload.get("assignee")
            self._by_task[(event.run_id, event.task_id)] = who if isinstance(who, str) else None

    def current(self, event: Any) -> str | None:
        return self._by_task.get((event.run_id or "", event.task_id or ""))


def _moves(ordered: Sequence[Entry]) -> list[_Move]:
    """Every task move in commit order, with the assignee in the move and the one holding the task
    (the move's own assignee, else the latest known one)."""
    held: dict[tuple[str, str], str | None] = {}
    out: list[_Move] = []
    for item in ordered:
        event = item.event
        if event.type != "task.transitioned" or event.run_id is None or event.task_id is None:
            continue
        key = (event.run_id, event.task_id)
        who = event.payload.get("assignee")
        assignee = who if isinstance(who, str) else None
        if assignee is not None:
            held[key] = assignee
        to = event.payload.get("to")
        out.append(
            _Move(
                event.run_id,
                event.task_id,
                event.seq,
                event.at,
                to if isinstance(to, str) else "",
                event.actor,
                assignee,
                held.get(key),
                event.payload.get("closed_attempt") is True,
            )
        )
    return out


__all__ = [
    "SCORECARD_VERSION",
    "Period",
    "ScorecardError",
    "SubjectScope",
    "Verdict",
    "config_at",
    "make_period",
    "round4",
    "round6",
    "scope_of",
    "scorecard",
]
