"""The embedded ledger writer for adapter tool calls (S4; section 16 N1,
N5, N9): one transaction per check, in the order of AD-2 as the writer sees it.

1. the recorded stop of the guarded run (AD-44a): deny `guarded_run_stopped`, one entry;
2. a pre-rule deny the agent's process reported (AD-27: unreadable name or arguments, changed
   arguments, an adapter error);
3. the frozen set: the pinned `tool_set_hash` (AD-19), the tool's name (AD-17), its schema (AD-18);
4. the gate (AD-32), given as one read made inside this transaction;
5. the policy check (`check_action` inside this unit of work, AD-21) and, for a soft denial, the
   count and the `denial_limit` entry directly after the counted entry (AD-42).

It also writes the freeze record (AD-16, AD-29), `task.refused` (AD-17 to AD-19) and `tool.called`
(AD-28). The writer keeps nothing between calls: the pin, the stop and the soft-denial count of a
guarded run are read from the run stream in the same transaction that decides (decision T267),
so a restarted process, a second writer and a parallel call are counted together. A store
transaction holds the store's write lock from its start (ND234), which is the per-run lock of
AD-42. Payloads hold names, ids, hashes and fixed codes only (AD-65)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from nuramesh.core.ledger import EventType
from nuramesh.core.org import OrgGraph
from nuramesh.core.policy import (
    POLICY_CONTRACT,
    Action,
    ActionKind,
    Approval,
    ApprovalStatus,
    Decision,
    PolicySet,
    approval_due,
)
from nuramesh.core.toolcalls import (
    ADAPTERS,
    DEADLINE_WAIT_SECONDS,
    DENIAL_LIMIT,
    FrozenToolSet,
    ReasonCode,
    ToolArguments,
    UnreadableToolName,
    check_deadline_seconds,
    check_denial_limit,
    check_tool_name,
    is_stop,
)
from nuramesh.engine.content import ContentService, LedgerHashing
from nuramesh.engine.guarded import (
    FROZEN_KIND,
    REFUSALS,
    Gate,
    GuardedRun,
    GuardedState,
    budget_gate,
    deny_draft,
    guarded_state,
)
from nuramesh.engine.policy import ToolCall, check_action, expire_approval_in, expire_matching_in
from nuramesh.stores.base import EventDraft, Store, UnitOfWork, run_stream

UNREADABLE_TARGET = "unreadable"  # AD-27: the target when the name itself was unreadable
OUTCOMES = frozenset({"ok", "error", "cancelled"})

# the pre-rule denies the agent's process finds itself (AD-2 steps 2 and 6, AD-13, F1)
_REPORTED = frozenset(
    {
        ReasonCode.UNREADABLE_TOOL_NAME,
        ReasonCode.UNREADABLE_ARGUMENTS,
        ReasonCode.UNKNOWN_DELEGATE,
        ReasonCode.PATH_OUTSIDE_WORKSPACE,
        ReasonCode.ARGUMENTS_CHANGED,
        ReasonCode.ADAPTER_ERROR,
    }
)
_GATE = frozenset(
    {
        ReasonCode.ORG_HALTED,
        ReasonCode.ROLE_PAUSED,
        ReasonCode.RUN_STOPPED,
        ReasonCode.NO_BUDGET,
        ReasonCode.BUDGET_STOP,
    }
)
_REFUSALS = REFUSALS
# the only checks recorded without an `args_hash` (AD-26: the arguments were unreadable, or an
# adapter error before a digest existed, F1; an unreadable name is refused before they are read)
_NO_DIGEST = frozenset(
    {
        ReasonCode.UNREADABLE_TOOL_NAME,
        ReasonCode.UNREADABLE_ARGUMENTS,
        ReasonCode.ADAPTER_ERROR,
    }
)


class Wait(Enum):
    """What a waiting call learns when its approval has no final outcome yet."""

    PENDING = "pending"  # nobody answered and the deadline has not passed: keep waiting
    RECHECK = "recheck"  # a person approved (or a sibling used it): ask again (AD-47, AD-48)


class NotFrozen(RuntimeError):  # noqa: N818 (a state, said in words)
    """A check or a record for a guarded run whose tool set was never frozen (AD-16)."""


class Superseded(RuntimeError):  # noqa: N818 (a state, said in words)
    """A write for a guarded run after another attempt of its task and agent froze its tool set
    (AD-20, section 16 item 2): the older attempt's window has ended, so its writer writes
    nothing more and its seam stops locally (as F4)."""


@dataclass(frozen=True)
class Answer:
    """The writer's answer to one check: the outcome, its reason code (the call's own code, the
    one a text for the model may name, AD-42), the approval of an `ask`, and the stop this check
    recorded or met, if any."""

    decision: Decision
    code: ReasonCode
    approval_id: str | None = None
    stop: ReasonCode | None = None


class ToolLedger:
    """The party that writes the ledger for one embedded guarded run."""

    def __init__(
        self,
        store: Store,
        policies: PolicySet,
        org: OrgGraph,
        guarded: GuardedRun,
        *,
        adapter: str,
        hashing: LedgerHashing,
        gate: Gate,
        content: ContentService | None = None,
        required_suites: Mapping[str, frozenset[str]] | None = None,
        denial_limit: object = DENIAL_LIMIT,
        deadline_seconds: object = DEADLINE_WAIT_SECONDS,
    ) -> None:
        if adapter not in ADAPTERS:
            raise ValueError("unknown adapter")
        self._limit = check_denial_limit(denial_limit)  # 1 to 3, never off (AD-42, AD-69)
        self._deadline = check_deadline_seconds(deadline_seconds)  # 1 to 604800 (AD-46)
        self._store, self._policies, self._org = store, policies, org
        self._guarded, self._adapter, self._hashing, self._gate = guarded, adapter, hashing, gate
        self._content, self._suites = content, required_suites
        self._role = org.role_of(guarded.agent_id).id  # KeyError for an agent outside the org
        self._actor = f"agent:{guarded.agent_id}"

    @property
    def deadline_seconds(self) -> int:
        """The approval deadline this writer records, in seconds (AD-46)."""
        return self._deadline

    def _append(self, uow: UnitOfWork, type_: EventType, payload: dict[str, Any]) -> None:
        uow.append(
            EventDraft(
                stream=run_stream(self._guarded.run_id),
                type=type_,
                actor=self._actor,
                run_id=self._guarded.run_id,
                task_id=self._guarded.task_id,
                payload=payload,
            )
        )

    def _refused(self, uow: UnitOfWork, code: ReasonCode, tool_set_hash: str) -> None:
        self._append(
            uow,
            EventType.TASK_REFUSED,
            {
                "agent": self._guarded.agent_id,
                "role": self._role,
                "adapter": self._adapter,
                "code": code.value,
                "tool_set_hash": tool_set_hash,
            },
        )

    def _state(self, uow: UnitOfWork, *, ended: bool = False) -> tuple[GuardedState, str]:
        """The guarded run's state and pin. Raises before any write when the run was never
        frozen, or when another attempt froze after it (`Superseded`), unless `ended` says the
        caller records the end of a body that had started (AD-28)."""
        state = guarded_state(uow, self._guarded)
        if state.superseded and not ended:
            raise Superseded("another attempt of this task has frozen its tool set")
        if state.tool_set_hash is None:
            raise NotFrozen("the guarded run's tool set was not frozen")
        return state, state.tool_set_hash

    def _own(self, approval: Approval) -> bool:
        """Whether a tool-call check of this guarded run opened the approval (AD-20)."""
        guarded = self._guarded
        return (
            approval.agent_id == guarded.agent_id
            and approval.run_id == guarded.run_id
            and approval.task_id == guarded.task_id
            and approval.opener is not None
            and approval.opener.attempt == guarded.attempt
        )

    def freeze(
        self, frozen: FrozenToolSet, *, framework: str, framework_version: str
    ) -> ReasonCode | None:
        """Freeze the tool set of the guarded run (AD-16): one `decision.recorded` entry of kind
        `tool_set_frozen`. A second freeze with the same hash writes nothing; with another hash it
        is refused: `task.refused` `tool_set_changed`, and that code is returned as the stop
        (AD-19). A failed write propagates: the run does not start (F5). `Superseded`, and
        nothing written, once another attempt of the task froze after this one, or a newer
        attempt froze first (AD-20)."""
        guarded = self._guarded
        with self._store.transaction() as uow:
            state = guarded_state(uow, guarded)
            if state.superseded:
                raise Superseded("another attempt of this task has frozen its tool set")
            if state.tool_set_hash == frozen.tool_set_hash:
                return None
            if state.tool_set_hash is not None:
                if not state.stopped:
                    self._refused(uow, ReasonCode.TOOL_SET_CHANGED, state.tool_set_hash)
                return ReasonCode.TOOL_SET_CHANGED
            self._append(
                uow,
                EventType.DECISION_RECORDED,
                {
                    "kind": FROZEN_KIND,
                    "agent": guarded.agent_id,
                    "role": self._role,
                    "adapter": self._adapter,
                    "framework": framework,
                    "framework_version": framework_version,
                    "tool_count": len(frozen.tools),
                    "tool_set_hash": frozen.tool_set_hash,
                    "tools": frozen.ledger_tools(),
                    "attempt": guarded.attempt,
                },
            )
        return None

    def refuse_turn(self, code: ReasonCode) -> None:
        """A refusal of the guarded run that has no call to decide: a model turn offered a tool
        outside the frozen set or a replaced one (AD-17, AD-18), or, in a LangGraph run (#971,
        LG-1, LG-3), a tool result came back without a valid run token (`tool_token_invalid`),
        the run ended with tool calls nobody answered (`tool_calls_unanswered`), or a tool
        message answers no call (`tool_message_orphan`). One `task.refused` with the fixed code
        and the frozen `tool_set_hash`, and nothing else: no message content, call id or token.
        It records the stop of the guarded run (AD-44a); a run that is already stopped gets no
        second entry. `NotFrozen` before the freeze, `Superseded` in another attempt's window:
        nothing is written."""
        if code not in _REFUSALS:
            raise ValueError(
                "a guarded run is refused only with a task.refused code: "
                + ", ".join(sorted(c.value for c in _REFUSALS))
            )
        with self._store.transaction() as uow:
            state, pinned = self._state(uow)
            if not state.stopped:
                self._refused(uow, code, pinned)

    def check(
        self,
        tool: str,
        call_id: str,
        *,
        arguments: ToolArguments | None,
        tool_set_hash: str,
        schema: str | None = None,
        pre_deny: ReasonCode | None = None,
        waits: bool = True,
        action: Action | None = None,
        waiting_on: str | None = None,
    ) -> Answer:
        """Decide one tool call in one transaction and record it. `arguments` is `None` only with
        a `pre_deny` that says why no digest exists (`unreadable_tool_name`,
        `unreadable_arguments`, `adapter_error`), anything else is a `ValueError` before the
        transaction; `schema` is the tool's `schema_hash` now;
        `pre_deny` is a refusal the agent's process found (AD-27); `waits` false says the call
        will not wait on an `ask` (AD-45); `action` replaces `use_tool:<tool>` for a handoff
        (AD-13); `waiting_on` is the approval this call waited on, for its re-check after an
        answer (AD-46: if that approval expires here the call ends `approval_timeout`). A check
        first records the expiry of every due approval it would match and of every due approval
        its own guarded run opened (C78, T270a). A failed transaction propagates and nothing of
        it is kept (F4)."""
        if pre_deny is not None and pre_deny not in _REPORTED:
            raise ValueError("not a pre-rule deny an agent's process can report")
        if arguments is None and pre_deny not in _NO_DIGEST:
            # AD-24, AD-26: a check without a digest could match a plain approval or return an
            # ask nobody counts; only the three codes above are written without `args_hash`
            raise ValueError("a check without arguments needs the pre-rule deny that explains it")
        if pre_deny is not ReasonCode.UNREADABLE_TOOL_NAME and not _is_tool_name(tool):
            # AD-7: the writer is the trust boundary (the standalone transport bypasses the
            # seam); a name that fails the pattern is never written, it is the same refusal as
            # an unreadable name, without a digest (AD-26)
            pre_deny, arguments = ReasonCode.UNREADABLE_TOOL_NAME, None
        digest = arguments.digest if arguments is not None else None
        named = pre_deny is not ReasonCode.UNREADABLE_TOOL_NAME
        with self._store.transaction() as uow:
            state, pinned = self._state(uow)
            hasher = self._hashing.bind(uow, self._org.id)  # inside this unit of work
            call = ToolCall(
                self._adapter,
                call_id,
                digest,
                pinned,
                waits=waits,
                canonical=arguments.canonical if arguments is not None else None,
                deadline_seconds=self._deadline,
                attempt=self._guarded.attempt,
                denial_limit=self._limit,
            )
            target = action or Action(kind=ActionKind.USE_TOOL, target=tool)
            # the action as `check_action` records it: a hashed target (a derived `run_command`,
            # AD-12a) is written as its keyed hash in every entry, a pre-rule deny included
            shown: dict[str, str] = {"action": target.kind.value, "target": UNREADABLE_TARGET}
            if named:
                shown = target.ledger_fields(hasher)
                if "target_hash" in shown:
                    shown.update(hasher.marker())
            fields = {
                "agent": self._guarded.agent_id,
                "role": self._role,
                "policy_contract": POLICY_CONTRACT,
                **shown,
                **call.ledger_fields(hasher),
            }

            def deny(code: ReasonCode) -> None:
                guarded = self._guarded
                uow.append(deny_draft(guarded.run_id, guarded.task_id, self._actor, fields, code))

            def pre_rule(code: ReasonCode, refusal: ReasonCode | None = None) -> Answer:
                deny(code)
                if refusal is not None:
                    self._refused(uow, refusal, pinned)
                if is_stop(code):
                    return Answer(Decision.DENY, code, stop=code)
                return Answer(Decision.DENY, code, stop=counted())

            def counted() -> ReasonCode | None:
                # one more soft denial was just recorded: the limit entry directly after it
                if state.denials + 1 < self._limit:
                    return None
                deny(ReasonCode.DENIAL_LIMIT)
                return ReasonCode.DENIAL_LIMIT

            # a count that already stands at the limit without a recorded stop (an answer or an
            # expiry counted in another transaction) is a stop here too: fail closed
            if state.stopped or state.denials >= self._limit:
                return pre_rule(ReasonCode.GUARDED_RUN_STOPPED)
            if pre_deny is not None:
                return pre_rule(pre_deny)
            if tool_set_hash != pinned:
                return pre_rule(ReasonCode.TOOL_SET_CHANGED)
            if tool not in state.tools:
                return pre_rule(ReasonCode.TOOL_NOT_FROZEN, ReasonCode.TOOL_SET_CHANGED)
            if state.tools[tool] != schema:
                return pre_rule(ReasonCode.TOOL_REPLACED, ReasonCode.TOOL_REPLACED)
            try:
                gated = self._gate(uow, self._guarded.context)
            except Exception:  # noqa: BLE001 (F7: a gate that cannot read refuses)
                gated = ReasonCode.BUDGET_STOP
            if gated is not None:
                return pre_rule(gated if gated in _GATE else ReasonCode.BUDGET_STOP)
            expired = expire_matching_in(
                uow,
                self._org,
                self._guarded.context,
                target,
                call.ledger_fields(hasher).get("args_hash"),
                attempt=self._guarded.attempt,
            )
            if expired:  # AD-46: this check recorded an expiry (and its count, AD-42)
                state = guarded_state(uow, self._guarded)
                if waiting_on in expired:
                    return Answer(
                        Decision.DENY, ReasonCode.APPROVAL_TIMEOUT, waiting_on, state.stop
                    )
                if state.stopped:  # the expiry reached the limit: no approval is opened
                    # AD-42: the call's own deny, with the stop this transaction recorded
                    deny(ReasonCode.GUARDED_RUN_STOPPED)
                    return Answer(Decision.DENY, ReasonCode.GUARDED_RUN_STOPPED, stop=state.stop)
            if waiting_on is not None:
                # AD-46: the approval this call waited on was already recorded as expired (a
                # sweep, a status read, a sibling): the call ends `approval_timeout`; the expiry
                # entry is its one entry, so nothing more is written
                held = uow.get_approval(waiting_on)
                if (
                    held is not None
                    and held.approval.status is ApprovalStatus.EXPIRED
                    and self._own(held.approval)
                ):
                    return Answer(
                        Decision.DENY, ReasonCode.APPROVAL_TIMEOUT, waiting_on, state.stop
                    )
            result = check_action(
                self._store,
                self._policies,
                self._org,
                self._guarded.context,
                target,
                hasher=hasher,
                actor=self._actor,
                required_suites=self._suites,
                content=self._content,
                tool_call=call,
                uow=uow,
            )
            if result.allowed:
                return Answer(Decision.ALLOW, ReasonCode.ALLOW, result.approval_id)
            if result.decision.decision is Decision.ASK:
                stop = None if waits else counted()
                return Answer(Decision.ASK, ReasonCode.ASK_PENDING, result.approval_id, stop)
            return Answer(Decision.DENY, ReasonCode.DENIED_BY_RULE, stop=counted())

    def waited(self, approval_id: str) -> "Answer | Wait":
        """What a call that waits on an approval learns now (AD-46, AD-48, AD-49), in one
        transaction: `Wait.PENDING` while nobody answered and the deadline has not passed;
        `Wait.RECHECK` once a person approved (the call then asks again and runs only on a
        consumed `allow`, AD-47); a deny `rejected_by_person`; a deny `approval_timeout`, whose
        expiry this read records if nobody has; a deny `stopped` when a stop was recorded while
        the call waited (the recorded stop comes first, AD-49: a deadline that passes after it
        is still recorded, counts nothing and the wait ends `stopped`). An approval that no
        check of this guarded run opened is answered `approval_expired` (AD-20). The answer
        carries the recorded stop, if any."""
        with self._store.transaction() as uow:
            state, _ = self._state(uow)
            stored = uow.get_approval(approval_id)
            if stored is None or not self._own(stored.approval):
                return Answer(Decision.DENY, ReasonCode.APPROVAL_EXPIRED, approval_id, state.stop)
            approval = stored.approval
            if approval_due(approval, uow.at):
                expire_approval_in(uow, stored, self._org)
                if state.stopped:  # AD-49: the stop was recorded before this expiry
                    return Answer(Decision.DENY, ReasonCode.STOPPED, approval_id, state.stop)
                stop = guarded_state(uow, self._guarded).stop
                return Answer(Decision.DENY, ReasonCode.APPROVAL_TIMEOUT, approval_id, stop)
            if approval.status is ApprovalStatus.EXPIRED:
                return Answer(Decision.DENY, ReasonCode.APPROVAL_TIMEOUT, approval_id, state.stop)
            if approval.status is ApprovalStatus.DENIED:
                return Answer(Decision.DENY, ReasonCode.REJECTED_BY_PERSON, approval_id, state.stop)
            if state.stopped:  # AD-49: a later approval is never consumed by this run
                return Answer(Decision.DENY, ReasonCode.STOPPED, approval_id, state.stop)
            return Wait.PENDING if approval.status is ApprovalStatus.PENDING else Wait.RECHECK

    def called(self, tool: str, call_id: str, arguments: ToolArguments, outcome: str) -> None:
        """Record that a body ended (AD-28): one `tool.called`, also after a recorded stop (a
        body that started before it, AD-44a). The cost is unknown, never zero (AD-33). A failed
        write propagates (F6)."""
        if outcome not in OUTCOMES:
            raise ValueError("a tool call ends ok, error or cancelled")
        check_tool_name(tool)  # AD-7: refused (`UnreadableToolName`) before anything is written
        with self._store.transaction() as uow:
            _, pinned = self._state(uow, ended=True)
            call = ToolCall(self._adapter, call_id, arguments.digest, pinned)
            fields = call.ledger_fields(self._hashing.bind(uow, self._org.id))
            self._append(
                uow,
                EventType.TOOL_CALLED,
                {
                    "agent": self._guarded.agent_id,
                    "role": self._role,
                    "tool": tool,
                    **fields,
                    "outcome": outcome,
                    "cost": None,
                    "cost_basis": "unknown",
                },
            )


def _is_tool_name(tool: object) -> bool:
    try:
        check_tool_name(tool)
    except UnreadableToolName:
        return False
    return True


__all__ = [
    "Answer",
    "Gate",
    "GuardedRun",
    "GuardedState",
    "NotFrozen",
    "Superseded",
    "ToolLedger",
    "Wait",
    "budget_gate",
    "guarded_state",
]
