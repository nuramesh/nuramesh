"""Read models for the API: what the app's Runs, Ledger, Approvals and Budgets screens need.

Everything here is **derived from the store and the ledger** with the engine's own models; nothing
is decided here (a gate's outcome is the recorded one, a budget's state is the stored balance).
The ledger holds hashes, never prompts or answers (A7), so these views can't show them either:
gate verdicts carry the criterion, the verdict and the result hash, and the stored text of a
person's reason is only reachable through the content endpoint.

Shapes are documented, with examples from the first real run, in `docs/api.md`.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nuramesh.core.budgets import BudgetBalance, as_of
from nuramesh.core.canonical import thaw as thaw
from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.ledger import CostBasis, EventType, LedgerEvent, format_time
from nuramesh.core.models import payload_model_ref
from nuramesh.core.money import Money
from nuramesh.core.policy import Approval
from nuramesh.core.redaction import reason_text_visible, redact_payload
from nuramesh.core.solo import action_digest
from nuramesh.core.tasks import MAX_ATTEMPTS_LIMIT, Task, TaskStatus
from nuramesh.engine.run import budgets_in_ledger, build_report
from nuramesh.engine.run_control import withheld_text
from nuramesh.stores.base import (
    BadTimeError,
    Store,
    StoredApproval,
    StoredBalance,
    StoredTask,
    run_stream,
)

PAGE = 500
MAX_SCAN = 5_000  # most events one filtered ledger request reads
T = EventType
_WAITING_FOR_HUMAN = {TaskStatus.NEEDS_HUMAN, TaskStatus.BLOCKED, TaskStatus.VERIFYING}


def money(value: Money | None) -> dict[str, str] | None:
    """`{"amount": "0.00052050", "currency": "USD"}` (exact decimal text), or None."""
    return value.to_json() if value is not None else None


def read_all(store: Store, stream: str) -> list[LedgerEvent]:
    """A whole stream, in pages."""
    events: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
        events.extend(batch)
        if len(batch) < PAGE:
            return events
        cursor = batch[-1].seq


class ScanLimit(Exception):  # noqa: N818 (a bound reached, not a failure)
    """A request would read more ledger entries than it may."""


def event_json(event: LedgerEvent) -> dict[str, Any]:
    """A ledger event as the API shows it: the stored event (hashes, no content), with a person's
    reason and note text hidden (R1.27-H). `redacted` names the keys hidden, when there are any."""
    body = event.model_dump(mode="json")
    body["payload"], hidden = redact_payload(event.type, event.actor, body["payload"])
    if hidden:
        body["redacted"] = hidden
    return body


def approval_json(approval: Approval) -> dict[str, Any]:
    """An approval as the API shows it: a legacy plain `decision_reason` is a person's text, so it
    is hidden unless the decider may show text (R7; the hash stays)."""
    body = approval.model_dump(mode="json")
    if not reason_text_visible(approval.decided_by):
        body["decision_reason"] = None
    # SA-6: what a solo approve must send back. Shown only here: an unkeyed hash of a target
    # that may be sensitive never goes to the ledger, a log or an error body.
    body["action_digest"] = action_digest(approval)
    return body


def run_ids(store: Store) -> list[str]:
    """The ids of every run with a ledger stream."""
    return [s[4:] for s in store.streams() if s.startswith("run:")]


# -- spend ---------------------------------------------------------------------------------------


@dataclass
class _Spend:
    total: Money
    calls: int = 0
    unknown_calls: int = 0

    def add(self, event: LedgerEvent) -> None:
        self.calls += 1
        if event.cost is None or event.cost.currency != self.total.currency:
            self.unknown_calls += 1
        else:
            self.total = self.total + event.cost

    def view(self) -> dict[str, Any]:
        return {
            "total": money(self.total),
            "calls": self.calls,
            "unknown_cost_calls": self.unknown_calls,
        }


_OK_OUTCOMES = ("ok", "settled_late")  # the `model.called` outcomes of an answered call


def _at_least(total: Money, cost_unknown_calls: int) -> dict[str, str] | None:
    """`at least`: the KNOWN total, when any call is of unknown cost (cost spec 3.5 rules 1 and 5,
    default-prices 6.2). What the budgets charged for such a call is the reservation, an upper
    bound, so it is never added. None when no call is of unknown cost."""
    return money(total) if cost_unknown_calls else None


def _is_cost_unknown(event: LedgerEvent) -> bool:
    """A call whose ledger entry says `cost_basis: unknown` (the answer had no usable usage; its
    reservation was charged), never inferred from the outcome. Only an answered call (`ok`,
    `settled_late`) counts: a sent call that errored or was cancelled is recorded with basis
    unknown too, but is no spend (cost spec 1), the same rule as the engine's `run_spend_line`."""
    return (
        event.cost is None
        and str(event.cost_basis) == CostBasis.UNKNOWN.value
        and event.payload.get("outcome") in _OK_OUTCOMES
    )


def spend_of(events: Iterable[LedgerEvent]) -> dict[str, Any]:
    """Actual spend of a run from its `model.called` entries: total, per agent, per model.

    A call whose price was unknown has no cost and is counted in `unknown_cost_calls` (it is never
    guessed, ND3)."""
    events = list(events)
    unknown = sum(1 for e in events if e.type == T.MODEL_CALLED.value and _is_cost_unknown(e))
    total = _Spend(Money.of("0"))
    by_agent: dict[str, _Spend] = {}
    by_model: dict[str, _Spend] = {}
    by_task: dict[str, _Spend] = {}
    for event in events:
        if event.type != T.MODEL_CALLED.value:
            continue
        total.add(event)
        agent = str(event.payload.get("agent", "unknown"))
        model = payload_model_ref(event.payload)
        for table, key in ((by_agent, agent), (by_model, model), (by_task, event.task_id or "")):
            table.setdefault(key, _Spend(Money.of("0"))).add(event)
    return {
        **total.view(),
        "cost_unknown_calls": unknown,
        "at_least": _at_least(total.total, unknown),
        "by_agent": [{"agent": k, **v.view()} for k, v in sorted(by_agent.items())],
        "by_model": [{"model": k, **v.view()} for k, v in sorted(by_model.items())],
        "by_task": {k: v.view() for k, v in by_task.items() if k},
    }


# -- tasks, gates and verdicts -----------------------------------------------------------------


def _gate_line(task: Task, event: LedgerEvent) -> dict[str, Any]:
    payload = event.payload
    gate_id = str(payload.get("gate"))
    criteria = {c.id: c.text for c in task.definition_of_done.criteria}
    try:
        spec = task.definition_of_done.gate(gate_id)
        kind, required = spec.kind.value, spec.required
    except KeyError:
        kind, required = "unknown", None
    return {
        "gate": gate_id,
        "kind": kind,
        "required": required,
        "outcome": payload.get("outcome"),
        "checked_by": payload.get("checked_by"),
        "result_hash": payload.get("result_hash"),
        "hash_scheme": payload.get("hash_scheme"),
        "hash_key_id": payload.get("hash_key_id"),
        "verdicts": [
            {
                "criterion": v.get("criterion"),
                "text": criteria.get(str(v.get("criterion")), ""),
                "verdict": v.get("verdict"),
            }
            for v in payload.get("verdicts", [])
            if isinstance(v, Mapping)
        ],
        "ledger_seq": event.seq,
        "at": event.at.isoformat(),
    }


def _definition_of_done(task: Task) -> dict[str, Any]:
    return {
        "criteria": [{"id": c.id, "text": c.text} for c in task.definition_of_done.criteria],
        "gates": [
            {"id": g.id, "kind": g.kind.value, "required": g.required, "checks": list(g.checks)}
            for g in task.definition_of_done.gates
        ],
    }


def _shown_reason(move: LedgerEvent | None) -> str | None:
    """A move's reason when it is a non-empty string written by `system`; else None (R5)."""
    if move is None or not reason_text_visible(move.actor):
        return None
    reason = move.payload.get("reason")
    return reason if isinstance(reason, str) and reason else None


def _shown_nested(move: LedgerEvent | None, key: str) -> Any:
    """A payload sub-tree of a move, with a person's reason and note text hidden (R2)."""
    if move is None:
        return []
    shown, _ = redact_payload(move.type, move.actor, {key: thaw(move.payload.get(key, []))})
    return shown[key]


def wait_warning_view(task: Task, events: Sequence[LedgerEvent]) -> dict[str, Any] | None:
    """The in-app expiry marker (R1.20 PR D, spec 7): the latest `human_wait_warning` of the task's
    current attempt while it waits in `verifying`, as `{days_left, expires_at}`; None otherwise.
    The text the person sees is built from these two numbers and nothing else."""
    if task.status is not TaskStatus.VERIFYING:
        return None
    latest: LedgerEvent | None = None
    for e in events:
        if (
            e.type == T.DECISION_RECORDED.value
            and e.task_id == task.id
            and e.payload.get("kind") == "human_wait_warning"
            and e.payload.get("attempt") == task.attempt
        ):
            latest = e
    if latest is None:
        return None
    left, expires = latest.payload.get("days_left"), latest.payload.get("expires_at")
    return {
        "days_left": left if isinstance(left, int) else None,
        "expires_at": expires if isinstance(expires, str) else None,
    }


def task_view(
    stored: StoredTask,
    events: Sequence[LedgerEvent],
    spend: Mapping[str, Any],
    *,
    show_warning: bool = False,
) -> dict[str, Any]:
    """One task: status, definition of done, every gate round with its verdicts, the attempts.
    `show_warning` adds the expiry marker; it is for people who may decide only (N05)."""
    task = stored.task
    mine = [e for e in events if e.task_id == task.id]
    rounds: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for e in mine:
        if e.type == T.GATE_COMPLETED.value:
            key = (int(e.payload.get("attempt", 0)), int(e.payload.get("submission", 0)))
            rounds.setdefault(key, []).append(_gate_line(task, e))
    moves = [e for e in mine if e.type == T.TASK_TRANSITIONED.value]
    attempts = []
    for (attempt, submission), gates in sorted(rounds.items()):
        decided = next(
            (
                m
                for m in moves
                if m.payload.get("from") == "verifying"
                and m.payload.get("attempt_before") == attempt
                and m.payload.get("submission") == submission
            ),
            None,
        )
        attempts.append(
            {
                "attempt": attempt,
                "submission": submission,
                "gates": gates,
                "moved_to": decided.payload.get("to") if decided else None,
                "reason": _shown_reason(decided),
                "verification": _shown_nested(decided, "verification"),
            }
        )
    last_move = moves[-1] if moves else None
    cap = {"max_tokens": task.max_tokens} if task.max_tokens is not None else {}
    return {
        **cap,
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status.value,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "submission": task.submission,
        "version": stored.version,
        "note": _shown_reason(last_move) if task.status is not TaskStatus.DONE else None,
        "definition_of_done": _definition_of_done(task),
        "attempts": attempts,
        "latest_gates": attempts[-1]["gates"] if attempts else [],
        "spend": spend.get(task.id) or {"total": money(Money.of("0")), "calls": 0},
        "wait_warning": wait_warning_view(task, mine) if show_warning else None,
    }


# -- runs ----------------------------------------------------------------------------------------


RUN_STATUSES = ("running", "stopping", "stopped", "done", "needs_human", "failed", "aborted")
_LOG = logging.getLogger(__name__)

MAX_RUNS_SCANNED = 500  # runs one list request may look at
MAX_EVENTS_SCANNED = 50_000  # ledger entries one list request may read


def _ended_status(ended: LedgerEvent, tasks: Sequence[StoredTask]) -> str:
    if ended.payload.get("status") == "stopped":
        return "stopped"
    if ended.payload.get("aborted"):
        return "aborted"
    statuses = [t.task.status for t in tasks]
    if statuses and all(s is TaskStatus.DONE for s in statuses):
        return "done"
    if any(s in _WAITING_FOR_HUMAN for s in statuses):
        return "needs_human"
    return "failed"


def _run_status(
    events: Sequence[LedgerEvent], tasks: Sequence[StoredTask], stop_requested: bool
) -> str:
    """running | stopping | stopped | done | needs_human | failed | aborted (docs/api.md)."""
    ended = next((e for e in reversed(events) if e.type == T.RUN_ENDED.value), None)
    if ended is None:
        return "stopping" if stop_requested else "running"
    return _ended_status(ended, tasks)


def list_runs(
    store: Store,
    *,
    limit: int,
    status: str | None,
    visible: Callable[[str], bool | None] | None = None,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Run summaries, most recent activity first: (rows, truncated, runs looked at). See
    `scan_runs`."""
    rows, truncated, scanned, _more = scan_runs(store, limit=limit, status=status, visible=visible)
    return rows, truncated, scanned


class NoSuchRunPosition(LookupError):
    """The run a cursor names is not (or no longer) a run the caller may list."""


def scan_runs(
    store: Store,
    *,
    limit: int,
    status: str | None,
    visible: Callable[[str], bool | None] | None = None,
    after: str | None = None,
) -> tuple[list[dict[str, Any]], bool, int, bool]:
    """Run summaries, most recent activity first: (rows, truncated, runs looked at, more).
    `after` (a run id) starts just behind that run; `NoSuchRunPosition` if it is not a run the
    caller may see. `more` is True only when a further run of the caller's own follows the rows.

    Bounded: at most `MAX_RUNS_SCANNED` runs and `MAX_EVENTS_SCANNED` ledger entries are looked
    at per request. A finished run is classified from its last entry (and its tasks) without
    reading its stream; only rows that are returned, and unfinished runs when a status is asked
    for, are read in full. `truncated` says the bounds stopped the scan before `limit` rows were
    found or all runs were seen. `visible(stream)` (the caller's organization scope) drops the runs
    the caller may not see: only the caller's own runs count as looked at, so another
    organization's runs can neither push the caller's out of the window nor show in `scanned`.
    `visible` answers None when it cannot decide (its first-time lookup budget is spent): the
    search stops, `truncated`."""
    heads = [h for h in store.heads().values() if h.stream.startswith("run:")]
    heads.sort(key=lambda h: (h.at, h.stream), reverse=True)
    if after is not None:
        names = [h.stream for h in heads]
        if f"run:{after}" not in names:
            raise NoSuchRunPosition(after)
        if visible is not None and visible(f"run:{after}") is not True:
            raise NoSuchRunPosition(after)
        heads = heads[names.index(f"run:{after}") + 1 :]
    budget = MAX_EVENTS_SCANNED
    rows: list[dict[str, Any]] = []
    scanned = 0
    for at, head in enumerate(heads):
        if len(rows) >= limit:
            return rows, False, scanned, _has_visible(heads[at:], visible)
        if scanned >= MAX_RUNS_SCANNED:
            return rows, True, scanned, False
        if visible is not None:
            seen = visible(head.stream)
            if seen is None:
                return rows, True, scanned, False
            if not seen:
                continue
        scanned += 1
        run_id = head.stream[4:]
        try:
            events: list[LedgerEvent] | None = None
            if status is not None:
                tail = list(store.read_stream(head.stream, after_seq=head.seq - 1, limit=1))
                if tail and tail[0].type == T.RUN_ENDED.value:
                    if _ended_status(tail[0], store.tasks_for_run(run_id)) != status:
                        continue
                else:  # unfinished: running or stopping needs the whole stream
                    if head.seq > budget:
                        return rows, True, scanned, False
                    budget -= head.seq
                    events = read_all(store, head.stream)
                    # an entry may follow run.ended (an approval decided later): look at the whole
                    # stream, with the run's tasks, exactly as the summary does
                    tasks = store.tasks_for_run(run_id)
                    if _run_status(events, tasks, _stop_view(events) is not None) != status:
                        continue
            if events is None:
                if head.seq > budget:
                    return rows, True, scanned, False
                budget -= head.seq
                events = read_all(store, head.stream)
            rows.append(run_summary(store, run_id, events))
        except BadTimeError:
            # a run holding a time that is not real is left out (`damaged_streams` names it): one
            # run never makes the list fail for everyone (ND238)
            _LOG.warning("run stream %s holds a time that is not real; skipped", head.stream)
    return rows, False, scanned, False


def _has_visible(heads: Sequence[Any], visible: Callable[[str], bool | None] | None) -> bool:
    """Whether any of `heads` is a run of the caller's (an undecided lookup counts as yes)."""
    return any(visible is None or visible(h.stream) is not False for h in heads)


def _stop_view(events: Sequence[LedgerEvent]) -> dict[str, Any] | None:
    request = next((e for e in events if e.type == T.RUN_STOP_REQUESTED.value), None)
    if request is None:
        return None
    ended = next((e for e in reversed(events) if e.type == T.RUN_ENDED.value), None)
    view = {
        "requested_by": request.actor,
        "requested_at": request.at.isoformat(),
        "ledger_seq": request.seq,
        "via": request.payload.get("via"),
        "reason_hash": request.payload.get("reason_hash"),
        "honoured": bool(ended and ended.payload.get("status") == "stopped"),
    }
    withheld = request.payload.get("reason_withheld")
    if isinstance(withheld, str):  # only a stop whose reason could not be kept (R1.37 K6)
        view["reason_withheld"] = withheld
        view["stop_reason_withheld"] = withheld_text(withheld)
    return view


def run_summary(store: Store, run_id: str, events: Sequence[LedgerEvent]) -> dict[str, Any]:
    """One row of the runs list."""
    tasks = store.tasks_for_run(run_id)
    started = next((e for e in events if e.type == T.RUN_STARTED.value), None)
    ended = next((e for e in reversed(events) if e.type == T.RUN_ENDED.value), None)
    gates = [e for e in events if e.type == T.GATE_COMPLETED.value]
    latest: dict[tuple[str, str], str] = {}  # latest recorded outcome per (task, gate)
    for e in gates:
        latest[(str(e.task_id), str(e.payload.get("gate")))] = str(e.payload.get("outcome"))
    stop = _stop_view(events)
    head = events[-1] if events else None
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.task.status.value] = counts.get(t.task.status.value, 0) + 1
    spend = spend_of(events)
    return {
        "run_id": run_id,
        "status": _run_status(events, tasks, stop is not None),
        "org": started.payload.get("org") if started else None,
        "started_at": started.at.isoformat() if started else None,
        "started_by": started.actor if started else None,
        "ended_at": ended.at.isoformat() if ended else None,
        "tasks": {"total": len(tasks), "by_status": counts},
        "gates": {
            "total": len(latest),
            "passed": sum(1 for o in latest.values() if o == "passed"),
            "failed": sum(1 for o in latest.values() if o == "failed"),
            "error": sum(1 for o in latest.values() if o == "error"),
        },
        "spend": {k: spend[k] for k in ("total", "calls", "unknown_cost_calls")},
        "stop": stop,
        "events": head.seq if head else 0,
        "head": head.hash if head else "",
        "updated": head.at.isoformat() if head else None,
    }


def budget_view(stored: StoredBalance, now: datetime) -> dict[str, Any]:
    """A budget and its balance **in its current window** (T56: a windowed budget whose window
    has ended shows the new, empty one even if nobody rolled it yet). `definition` is the stored
    budget as the engine models it (including any field a newer engine adds), so readers can
    show it without this API knowing about it."""
    b: BudgetBalance = as_of(stored.balance, now)
    return {
        "id": b.budget.id,
        "scope": b.budget.scope.value,
        "scope_id": b.budget.scope_id,
        "limit": money(b.budget.limit),
        "spent": money(b.spent),
        "reserved": money(b.reserved),
        "available": money(b.budget.limit - b.spent - b.reserved),
        "blocked": b.blocked,
        "window": b.budget.window.value,
        "on_breach": b.budget.breach_mode.value,
        "window_start": None if b.window_start is None else format_time(b.window_start),
        "definition": b.budget.model_dump(mode="json"),
        "version": stored.version,
    }


def budget_alerts(events: Iterable[LedgerEvent]) -> list[dict[str, Any]]:
    """The run's budget hard stops and overspends, as data (the CLI prints the same facts as
    `BREACH ALERT` lines): the budget, its scope and window, its limit, when it resets or that
    only a human lifts it, and the ledger entry that recorded it."""
    out: list[dict[str, Any]] = []
    for event in events:
        if event.type == T.BUDGET_EXCEEDED.value and event.payload.get("reason") == "hard_stop":
            items, kind = event.payload.get("shortfalls") or [], "hard_stop"
        elif event.type == T.BUDGET_BREACHED.value:
            items, kind = event.payload.get("budgets") or [], "limit_passed"
        else:
            continue
        for item in items:
            if isinstance(item, Mapping) and "budget" in item:
                shown, _ = redact_payload(event.type, event.actor, thaw(item))
                out.append({"kind": kind, "ledger_seq": event.seq, **shown})
    return out


def approval_view(stored: StoredApproval, events: Sequence[LedgerEvent] = ()) -> dict[str, Any]:
    """An approval request; with the run's events, the ledger entries that name it."""
    a = stored.approval
    entries = [
        {"ledger_seq": e.seq, "type": e.type, "at": e.at.isoformat(), "actor": e.actor}
        for e in events
        if e.type in (T.APPROVAL_REQUESTED.value, T.APPROVAL_DECIDED.value)
        and e.payload.get("approval") == a.id
    ]
    return {
        **approval_json(a),
        "version": stored.version,
        "ledger": entries,
        "reason_available": a.decision_reason_hash is not None,
    }


def run_detail(
    store: Store,
    run_id: str,
    events: Sequence[LedgerEvent],
    config: NuraMeshConfig | None,
    *,
    unowned: bool = False,
    show_warning: bool = False,
) -> dict[str, Any]:
    """Everything the live run screen shows: tasks with gates and verdicts, spend, budgets."""
    summary = run_summary(store, run_id, events)
    tasks = store.tasks_for_run(run_id)
    spend = spend_of(events)
    head = store.head(run_stream(run_id))
    check = store.verify_stream(run_stream(run_id))
    canon = [e for e in events if e.type == T.CANON_LOADED.value]
    started = next((e for e in events if e.type == T.RUN_STARTED.value), None)
    planned = [str(t) for t in (started.payload.get("tasks", []) if started else [])]
    order = {tid: i for i, tid in enumerate(planned)}
    tasks.sort(key=lambda t: (order.get(t.task.id, len(order)), t.task.id))
    ids = budgets_in_ledger(store, run_id)
    if config is not None and tasks:
        try:  # also the budgets this run could have spent from, as the report names them
            ids |= {line.id for line in build_report(store, config.org, run_id).budgets}
        except KeyError:  # the server's organization isn't the one this run was made with
            pass
    # the budgets of the organization that started the run (ND90): an id is only unique inside one
    run_org = started.payload.get("org") if started else None
    org_id = run_org if isinstance(run_org, str) and run_org else None
    if org_id is None and config is not None:
        org_id = config.org.id
    pool = [] if org_id is None else store.list_balances(org_id)
    if unowned:  # the local inspector also sees budgets no organization could be attributed
        pool = [*pool, *store.list_balances("")]
    balances = [b for b in pool if b.balance.budget.id in ids]
    return {
        **summary,
        "chief_of_staff": started.payload.get("chief_of_staff") if started else None,
        "canon_commit": str(canon[-1].payload.get("commit")) if canon else None,
        "ledger": {
            "stream": run_stream(run_id),
            "seq": head.seq if head else 0,
            "head": head.hash if head else "",
            "ok": check.ok,
        },
        "tasks": [task_view(t, events, spend["by_task"], show_warning=show_warning) for t in tasks],
        "spend": {k: v for k, v in spend.items() if k != "by_task"},
        "budgets": [budget_view(b, store.now()) for b in balances],
        "budget_alerts": budget_alerts(events),
        "approvals": [approval_view(a, events) for a in store.approvals(run_id=run_id)],
        "task_counts": summary["tasks"],
    }


# -- the per-task audit (R1.27) -----------------------------------------------------------------

MAX_AUDIT_ENTRIES = 1000  # only the `entries` list is cut at this many (T162)


def _count(value: Any) -> int | None:
    """A JSON integer >= 0 (a boolean is not one), else None."""
    return value if type(value) is int and value >= 0 else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@dataclass
class _AuditSpend:
    """One total with the split counts the audit page words (the run page's `_Spend` stays)."""

    total: Money
    calls: int = 0
    price_unknown: int = 0
    other_currency: int = 0
    failed_no_cost: int = 0
    cost_unknown: int = 0

    def add(self, event: LedgerEvent) -> None:
        self.calls += 1
        cost = event.cost
        if cost is not None and cost.currency == self.total.currency:
            self.total = self.total + cost
        elif cost is not None:
            self.other_currency += 1
        elif event.payload.get("outcome") in _OK_OUTCOMES:
            self.price_unknown += 1
            if _is_cost_unknown(event):
                self.cost_unknown += 1
        else:
            self.failed_no_cost += 1

    def view(self) -> dict[str, Any]:
        return {
            "total": money(self.total),
            "calls": self.calls,
            "unknown_cost_calls": self.price_unknown + self.other_currency + self.failed_no_cost,
            "price_unknown_calls": self.price_unknown,
            "cost_unknown_calls": self.cost_unknown,
            "at_least": _at_least(self.total, self.cost_unknown),
            "other_currency_calls": self.other_currency,
            "failed_calls_no_cost": self.failed_no_cost,
        }


def _attempt_of(moves: Sequence[LedgerEvent], seq: int) -> int:
    """The `attempt_after` of the latest transition before `seq`, else 0 (never an older one)."""
    latest: LedgerEvent | None = None
    for m in moves:
        if m.seq >= seq:
            break
        latest = m
    return (_count(latest.payload.get("attempt_after")) if latest else None) or 0


def _spend_by_attempt(mine: Sequence[LedgerEvent], moves: Sequence[LedgerEvent]) -> list[Any]:
    # A tampered stream may hold any number; the engine never goes past MAX_ATTEMPTS_LIMIT, so the
    # buckets stop there (a call after a larger number counts in the last bucket).
    highest = min(
        max((_count(m.payload.get("attempt_after")) or 0 for m in moves), default=0),
        MAX_ATTEMPTS_LIMIT,
    )
    buckets = {n: _AuditSpend(Money.zero()) for n in range(highest + 1)}
    for e in mine:
        if e.type == T.MODEL_CALLED.value:
            buckets[min(_attempt_of(moves, e.seq), highest)].add(e)
    return [{"attempt": n, **b.view()} for n, b in buckets.items()]


def _by(actor: str | None) -> str:
    if actor is not None and actor.startswith("human:"):
        return "person"
    if actor == "system" or (actor is not None and actor.startswith("gate:")):
        return "gate"
    return "other"


def _rework_row(m: LedgerEvent) -> dict[str, Any]:
    p = m.payload
    by = _by(m.actor)
    return {
        "ledger_seq": m.seq,
        "at": m.at.isoformat(),
        "from": p.get("from"),
        "to": p.get("to"),
        "attempt_before": _count(p.get("attempt_before")),
        "attempt_after": _count(p.get("attempt_after")),
        "max_attempts": _count(p.get("max_attempts")),
        "by": by,
        "reason": _shown_reason(m) if by == "gate" else None,
        "reason_hash": _text(p.get("reason_hash")),
        "feedback_hash": _text(p.get("feedback_hash")),
        "actor": m.actor,
    }


_DECISION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "human_decision": (
        ("gate", "gate"),
        ("attempt", "attempt"),
        ("submission", "submission"),
        ("outcome", "outcome"),
        ("reason_hash", "decision_hash"),
    ),
    "human_decision_refused": (("attempt", "attempt"), ("why", "why")),
    "engine_incident": (("task_status", "task_status"), ("human_required", "human_required")),
    "step_restarted": (("step", "step"), ("attempt", "attempt"), ("epoch", "epoch")),
    "human_wait_warning": (("attempt", "attempt"), ("days_left", "days_left")),
    "human_wait_expired": (("attempt", "attempt"), ("days_left", "days_left")),
    "dependency_waiting": (("waiting_on", "waiting_on"), ("attempt", "attempt")),
    "scripted_approval": (
        ("gate", "gate"),
        ("approver", "approver"),
        ("human_review", "human_review"),
    ),
}


def _decision_row(e: LedgerEvent) -> dict[str, Any]:
    kind = e.payload.get("kind")
    row: dict[str, Any] = {
        "ledger_seq": e.seq,
        "at": e.at.isoformat(),
        "actor": e.actor,
        "kind": kind if isinstance(kind, str) else "unknown",
    }
    fields = _DECISION_FIELDS.get(row["kind"], ())
    picked = {source: thaw(e.payload.get(source)) for _, source in fields}
    shown, _ = redact_payload(e.type, e.actor, picked)  # a nested reason or note stays hidden
    for name, source in fields:
        row[name] = shown[source]
    if row["kind"] == "engine_incident":
        note = e.payload.get("note")
        row["note_present"] = isinstance(note, str) and note != ""
    return row


def task_audit(
    store: Store, run_id: str, stored: StoredTask, approvals: Sequence[StoredApproval]
) -> dict[str, Any]:
    """Everything that happened to one task, from one snapshot of the run's ledger (R1.27).

    The derived sections come from all of the task's entries; only `entries` is cut at
    `MAX_AUDIT_ENTRIES`. A person's reason text and an incident's note are never copied (R12)."""
    stream = run_stream(run_id)
    events, head = store.snapshot(stream)
    task = stored.task
    mine = [e for e in events if e.task_id == task.id]
    moves = [e for e in mine if e.type == T.TASK_TRANSITIONED.value]
    total = _AuditSpend(Money.zero())
    for e in mine:
        if e.type == T.MODEL_CALLED.value:
            total.add(e)
    shown = task_view(stored, mine, {})
    attempts = []
    for row in shown["attempts"]:
        decided = next(
            (
                m
                for m in moves
                if m.payload.get("from") == "verifying"
                and m.payload.get("attempt_before") == row["attempt"]
                and m.payload.get("submission") == row["submission"]
            ),
            None,
        )
        attempts.append(
            {**row, "reason_hash": _text(decided.payload.get("reason_hash")) if decided else None}
        )
    mine_approvals = sorted(
        (a for a in approvals if a.approval.task_id == task.id),
        key=lambda a: (a.approval.requested_at, a.approval.id),
    )
    approval_rows = [{**approval_view(a, events), "decision_reason": None} for a in mine_approvals]
    check = store.verify_stream(stream)
    return {
        "run_id": run_id,
        "stream": stream,
        "task": {
            k: v for k, v in shown.items() if k not in ("attempts", "latest_gates", "spend", "note")
        },
        "chain": {
            "ok": check.ok,
            "head_seq": head.seq if head else 0,
            "head": head.hash if head else "",
        },
        "truncated": len(mine) > MAX_AUDIT_ENTRIES,
        "total_entries": len(mine),
        "entries": [event_json(e) for e in mine[:MAX_AUDIT_ENTRIES]],
        "attempts": attempts,
        "spend": total.view(),
        "spend_by_attempt": _spend_by_attempt(mine, moves),
        "approvals": approval_rows,
        "decisions": [_decision_row(e) for e in mine if e.type == T.DECISION_RECORDED.value],
        "rework": [
            _rework_row(m)
            for m in moves
            if (m.payload.get("from"), m.payload.get("to"))
            in (("verifying", "in_progress"), ("needs_human", "assigned"))
        ],
    }


# -- ledger pages --------------------------------------------------------------------------------


def ledger_page(
    store: Store,
    stream: str,
    *,
    after_seq: int,
    limit: int,
    type_prefix: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """A page of a stream, optionally only entries whose type starts with `type_prefix` and/or
    belong to a task. A filtered request reads at most `MAX_SCAN` entries; `next_after_seq` is the
    last entry read (not the last returned), so paging with it never skips or repeats."""
    head = store.head(stream)
    if head is None:
        raise LookupError(stream)
    found: list[LedgerEvent] = []
    cursor = after_seq
    scanned = 0
    while len(found) < limit and scanned < MAX_SCAN and cursor < head.seq:
        wide = bool(type_prefix or task_id)
        batch = list(
            store.read_stream(
                stream, after_seq=cursor, limit=PAGE if wide else min(PAGE, limit - len(found))
            )
        )
        if not batch:
            break
        for e in batch:
            scanned += 1
            cursor = e.seq
            if type_prefix and not e.type.startswith(type_prefix):
                continue
            if task_id and e.task_id != task_id:
                continue
            found.append(e)
            if len(found) >= limit:
                break
    return {
        "stream": stream,
        "events": [event_json(e) for e in found],
        "head_seq": head.seq,
        "head": head.hash,
        "next_after_seq": cursor,
        "has_more": cursor < head.seq,
    }


# -- what an agent credential may see (E2 spec 6.2.1) ---------------------------------------------
#
# Every view below is built from an explicit allow-list of keys. Nothing is copied by deleting keys
# from the full view, so a key added to a full view later can never reach an agent. An agent gets
# the task, its status and who holds it; the text of its own task and its definition of done; never
# attempts, gate verdicts, reasons, spend, budgets, approvals or people's ids.


def agent_task_view(stored: StoredTask, agent_id: str) -> dict[str, Any]:
    """A task as an agent sees it: `id`, `title`, `status`, `assignee`, and for the agent's own
    task also `instructions` and `definition_of_done`."""
    task = stored.task
    view: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "assignee": task.assignee,
    }
    if task.assignee == agent_id:
        view["instructions"] = task.instructions
        view["definition_of_done"] = _definition_of_done(task)
    return view


def _agent_counts(summary: Mapping[str, Any]) -> dict[str, Any]:
    tasks = summary["tasks"]
    return {"total": tasks["total"], "by_status": dict(tasks["by_status"])}


def agent_run_row(summary: Mapping[str, Any]) -> dict[str, Any]:
    """One row of the runs list for an agent."""
    return {
        "run_id": summary["run_id"],
        "status": summary["status"],
        "started_at": summary["started_at"],
        "ended_at": summary["ended_at"],
        "tasks": _agent_counts(summary),
    }


def agent_run_detail(
    store: Store, run_id: str, events: Sequence[LedgerEvent], agent_id: str
) -> dict[str, Any]:
    """One run for an agent: status, times, task counts and the agent's task list."""
    summary = run_summary(store, run_id, events)
    return {
        "run_id": summary["run_id"],
        "status": summary["status"],
        "started_at": summary["started_at"],
        "ended_at": summary["ended_at"],
        "task_counts": _agent_counts(summary),
        "tasks": agent_tasks(store, run_id, agent_id),
    }


def agent_tasks(store: Store, run_id: str, agent_id: str) -> list[dict[str, Any]]:
    """The run's tasks for an agent (see `agent_task_view`), ordered by id."""
    return [agent_task_view(t, agent_id) for t in store.tasks_for_run(run_id)]


def agent_actor_view(actor: str | None) -> str | None:
    """The actor of a stop request as an agent sees it: `agent:<slug>` as is, otherwise only the
    kind (`human`, `service`, or `system`)."""
    if actor is None:
        return None
    kind = actor.split(":", 1)[0]
    return actor if kind == "agent" else kind
