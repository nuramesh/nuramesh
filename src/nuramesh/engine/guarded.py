"""What the ledger says about one guarded run of an adapter (spec/adapters/README.md, S4; AD-19,
AD-42, AD-44a; decision T267), the `denial_limit` entry, and the budget gate (AD-32, N4).

This module imports the core and the stores only, so both `engine.policy` (a person's answer and
an expiry count a soft denial, AD-42) and `engine.toolcalls` (the check's own transaction) use
one implementation of the count without importing each other (decision T268)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from nuramesh.core.breakers import halt_state, paused_roles
from nuramesh.core.budgets import CallContext, applicable_budgets, roll
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.org import OrgGraph
from nuramesh.core.policy import HASHED_TARGETS, POLICY_CONTRACT, Approval, Decision
from nuramesh.core.toolcalls import ReasonCode, is_soft_denial, is_stop
from nuramesh.stores.base import EventDraft, UnitOfWork, run_stream

FROZEN_KIND = "tool_set_frozen"
PAGE = 500
# the closed list of `task.refused` codes of a guarded run (the ledger spec: "`task.refused` of a
# guarded run"); each records the stop of the run (AD-44a). The last three have no offered tool
# and no call (#971, LG-1 and LG-3): a tool result without a valid run token, a final turn's
# tool calls left unanswered, a tool message that answers no call.
REFUSALS = frozenset(
    {
        ReasonCode.TOOL_SET_CHANGED,
        ReasonCode.TOOL_REPLACED,
        ReasonCode.TOOL_TOKEN_INVALID,
        ReasonCode.TOOL_CALLS_UNANSWERED,
        ReasonCode.TOOL_MESSAGE_ORPHAN,
    }
)
# fixed reasons chosen by the code (AD-27); never a rule's text, never an argument
REASONS: Mapping[ReasonCode, str] = {
    ReasonCode.UNREADABLE_TOOL_NAME: "the tool name is not readable",
    ReasonCode.UNREADABLE_ARGUMENTS: "the arguments are not one readable JSON object",
    ReasonCode.UNKNOWN_DELEGATE: "the handoff target is not an agent of the organization",
    ReasonCode.PATH_OUTSIDE_WORKSPACE: "the path is outside the workspace",
    ReasonCode.ARGUMENTS_CHANGED: "the arguments are not the checked ones",
    ReasonCode.ADAPTER_ERROR: "the adapter failed before the tool could be checked",
    ReasonCode.TOOL_NOT_FROZEN: "the tool is not in the frozen tool set",
    ReasonCode.TOOL_SET_CHANGED: "the tool set is not the frozen one",
    ReasonCode.TOOL_REPLACED: "the tool is not the frozen one",
    ReasonCode.ORG_HALTED: "the organization is halted",
    ReasonCode.ROLE_PAUSED: "the agent's role is paused",
    ReasonCode.RUN_STOPPED: "the run was stopped",
    ReasonCode.NO_BUDGET: "no budget applies",
    ReasonCode.BUDGET_STOP: "a budget that applies has nothing available",
    ReasonCode.DENIAL_LIMIT: "the guarded run reached its denial limit",
    ReasonCode.GUARDED_RUN_STOPPED: "the guarded run was stopped",
    ReasonCode.APPROVAL_TIMEOUT: "nobody answered before the deadline",
}

Gate = Callable[[UnitOfWork, CallContext], ReasonCode | None]
"""The budget gate as one read inside the check's transaction (AD-32): the first matching code,
or `None` when the call may go on. It reserves nothing and writes nothing."""


@dataclass(frozen=True)
class GuardedRun:
    """The unit the contract protects, embedded (section 2): one agent on one attempt of one
    task of one run."""

    run_id: str
    task_id: str
    attempt: int
    agent_id: str

    @property
    def context(self) -> CallContext:
        """The same run, task and agent as a policy and budget context."""
        return CallContext(run_id=self.run_id, task_id=self.task_id, agent_id=self.agent_id)


@dataclass(frozen=True)
class GuardedState:
    """What the run stream says about one guarded run (AD-19, AD-42, AD-44a). `stop` is the code
    of the first recorded stop. `superseded`: another attempt of the same task and agent froze
    its tool set after this one did (this guarded run's window has ended), or a newer attempt
    froze before this one could; the pin, stop and count are then those at the end of the window
    and its writer writes nothing more (AD-20)."""

    tool_set_hash: str | None = None
    tools: Mapping[str, str | None] = field(default_factory=dict)
    stopped: bool = False
    denials: int = 0
    stop: ReasonCode | None = None
    superseded: bool = False


def guarded_state(uow: UnitOfWork, guarded: GuardedRun) -> GuardedState:
    """Read one guarded run's pin, stop and soft-denial count from the run stream, including this
    unit of work's own appends. A guarded run's entries are those of its task and agent from its
    freeze record (which names the attempt) up to the freeze record of another attempt. Entries
    carry no attempt, so a guarded run whose window has ended is `superseded`: what follows
    belongs to the other attempt, and this one's writer must write nothing more."""
    inside = False
    superseded = False
    state: dict[str, Any] = {}
    asked: set[str] = set()  # approvals opened for a tool call of this guarded run
    counted: set[str] = set()  # approvals whose outcome may count no more (AD-42 (3))

    def stop(code: ReasonCode) -> None:
        if not state["stopped"]:
            state["stopped"], state["stop"] = True, code

    for event in uow.task_events(guarded.run_id, guarded.task_id):
        payload = event.payload
        if payload.get("agent") != guarded.agent_id:
            continue
        if event.type == EventType.DECISION_RECORDED and payload.get("kind") == FROZEN_KIND:
            theirs = payload.get("attempt")
            if theirs != guarded.attempt:
                newer = isinstance(theirs, int) and theirs > guarded.attempt
                superseded = superseded or bool(state) or newer  # after its own, or newer
                inside = False
                continue
            inside = not superseded  # a window that ended never opens again
            if inside:
                asked, counted = set(), set()
                state = {
                    "tool_set_hash": payload["tool_set_hash"],
                    "tools": {t["name"]: t["schema"] for t in payload["tools"]},
                    "stopped": False,
                    "denials": 0,
                    "stop": None,
                }
            continue
        if not inside:
            continue
        approval = payload.get("approval")
        if event.type == EventType.TASK_REFUSED:
            if payload.get("code") in REFUSALS:
                stop(ReasonCode(payload["code"]))
        elif event.type == EventType.APPROVAL_REQUESTED and "args_hash" in payload:
            asked.add(str(approval))
        elif event.type == EventType.APPROVAL_DECIDED:
            if payload.get("approved") is False and approval in asked and approval not in counted:
                counted.add(str(approval))
                state["denials"] += 1
        elif event.type == EventType.POLICY_DECIDED and "adapter" in payload:
            code = payload.get("reason_code")
            if payload.get("decision") == Decision.ASK.value:
                if code == ReasonCode.ASK_PENDING:  # the call will not wait (AD-42 (2))
                    counted.add(str(approval))
                    state["denials"] += 1
            elif payload.get("decision") == Decision.DENY.value and code in set(ReasonCode):
                reason = ReasonCode(code)
                if is_stop(reason):
                    stop(reason)
                elif is_soft_denial(reason) and approval is None:
                    state["denials"] += 1
                elif is_soft_denial(reason) and approval in asked and approval not in counted:
                    # an expiry: the one outcome of an approval this guarded run opened (an
                    # approval of another attempt that expires later is not this run's denial)
                    counted.add(str(approval))
                    state["denials"] += 1
    return GuardedState(**state, superseded=superseded)


def deny_draft(
    run_id: str,
    task_id: str | None,
    actor: str,
    fields: Mapping[str, Any],
    code: ReasonCode,
    **more: Any,
) -> EventDraft:
    """A `policy.decided` deny without a rule (AD-27): `fields` are the entry's AD-26 fields."""
    return EventDraft(
        stream=run_stream(run_id),
        type=EventType.POLICY_DECIDED,
        actor=actor,
        run_id=run_id,
        task_id=task_id,
        payload={
            **fields,
            "decision": Decision.DENY.value,
            "rule": None,
            "reason": REASONS[code],
            "reason_code": code.value,
            **more,
        },
    )


def opener_fields(approval: Approval, role: str) -> dict[str, Any]:
    """The AD-26 fields of the check that opened a tool-call approval, as kept with it (AD-46)."""
    opener = approval.opener
    if opener is None or approval.args_hash is None:
        raise ValueError("not an approval of an adapter tool call")
    # the action as the opening check recorded it: a hashed target (`run_command`, AD-12a) is
    # kept as its keyed hash and never written from the approval row's plain target (AD-65)
    action: dict[str, Any] = dict(opener.action_fields)
    if not action:
        action = {"action": approval.action.kind.value}
        if approval.action.kind not in HASHED_TARGETS and approval.action.target is not None:
            action["target"] = approval.action.target
    return {
        "agent": approval.agent_id,
        "role": role,
        "policy_contract": POLICY_CONTRACT,
        **action,
        "adapter": opener.adapter,
        "call_id": opener.call_id,
        "tool_set_hash": opener.tool_set_hash,
        "args_hash": approval.args_hash,
        **approval.args_hash_marker,
    }


def count_outcome(uow: UnitOfWork, approval: Approval, role: str, before: GuardedState) -> bool:
    """After an approval's outcome (a rejection or an expiry) was appended in this unit of work:
    when that entry was counted and the count reached the limit, append the one `denial_limit`
    entry directly after it, actor `system`, with the `call_id` of the check that opened the
    approval (AD-42). Never after a recorded stop. `before` is the state read before the append.
    True when the entry was written."""
    opener = approval.opener
    if opener is None or approval.task_id is None:
        return False
    after = guarded_state(uow, guarded_of(approval))
    if after.stopped or after.denials <= before.denials or after.denials < opener.denial_limit:
        return False
    uow.append(
        deny_draft(
            approval.run_id,
            approval.task_id,
            "system",
            opener_fields(approval, role),
            ReasonCode.DENIAL_LIMIT,
        )
    )
    return True


def guarded_of(approval: Approval) -> GuardedRun:
    """The guarded run whose check opened this tool-call approval."""
    if approval.opener is None or approval.task_id is None:
        raise ValueError("not an approval of an adapter tool call")
    return GuardedRun(approval.run_id, approval.task_id, approval.opener.attempt, approval.agent_id)


def _events(uow: UnitOfWork, stream: str) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = uow.events_after(stream, cursor, PAGE)
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        cursor = batch[-1].seq


def _stops(event: LedgerEvent, task_id: str | None) -> bool:
    """Whether a run-stream entry says the run was stopped or this task has a stop request."""
    if event.type == EventType.RUN_STOP_REQUESTED.value:
        return True
    if event.type == EventType.RUN_ENDED.value:
        return event.payload.get("status") == "stopped"
    return event.type == EventType.TASK_STOP_REQUESTED.value and event.task_id == task_id


def budget_gate(org: OrgGraph) -> Gate:
    """The gate before every tool call (AD-32) as one read in the check's transaction: the
    organization's halt, the role's pause, the run's and the task's stop request, then every
    budget that applies with the balance of its current window. A window that has ended is
    rolled in memory only (an empty, unblocked balance; reservations still open stay with the
    closed window): the gate reserves nothing, changes no budget and writes nothing. Control
    state that cannot be read answers `org_halted`; a budget that cannot be read raises, which
    the writer turns into `budget_stop` (F7)."""

    def gate(uow: UnitOfWork, context: CallContext) -> ReasonCode | None:
        try:
            org_events = _events(uow, f"org:{org.id}")
            halted = halt_state(org_events).halted
            paused = paused_roles(org_events)
            run_events = _events(uow, run_stream(context.run_id))
            run_stopped = any(_stops(e, context.task_id) for e in run_events)
        except Exception:  # noqa: BLE001 (AD-32 row 1: unreadable control state, the run's too)
            return ReasonCode.ORG_HALTED
        if halted:
            return ReasonCode.ORG_HALTED
        if org.role_of(context.agent_id).id in paused:
            return ReasonCode.ROLE_PAUSED
        if run_stopped:
            return ReasonCode.RUN_STOPPED
        stored = {s.balance.budget.id: s.balance for s in uow.list_balances(org.id)}
        applicable = applicable_budgets([b.budget for b in stored.values()], context, org)
        if not applicable:
            return ReasonCode.NO_BUDGET
        now = uow.now()
        balances = []
        for budget in applicable:
            rolled = roll(stored[budget.id], now)
            balances.append(rolled.balance if rolled is not None else stored[budget.id])
        if any(b.blocked for b in balances):
            return ReasonCode.BUDGET_STOP
        if any(b.available.amount <= 0 for b in balances):
            return ReasonCode.BUDGET_STOP
        return None

    return gate


__all__ = [
    "FROZEN_KIND",
    "REASONS",
    "REFUSALS",
    "Gate",
    "GuardedRun",
    "GuardedState",
    "budget_gate",
    "count_outcome",
    "deny_draft",
    "guarded_of",
    "guarded_state",
    "opener_fields",
]
