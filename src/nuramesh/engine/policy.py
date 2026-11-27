"""Policy checks and approvals, each decision recorded in one transaction (PLAN H.1, H.2; T20).

`check_action` evaluates an agent's action against its role's policy (`core.policy.evaluate`:
default deny, deny over ask over allow) and records `policy.decided` **every time**, allowed or
not. What happens next depends on the decision:

- `allow`: nothing more. `deny`: the caller refuses the action.
- `ask`: if a human already approved this exact action (same agent, task, kind and target, same
  amount for spend) and the approval is unused, it is **consumed** (single use) and the action is
  allowed, recorded with the approval's id; otherwise a pending approval is opened
  (`approval.requested`, reused if the same request is already pending) and the caller waits.

`answer_approval` is the human's answer (`approval.decided` with who and why). Only a human, and
only one the role's policy names if it lists approvers (read when the answer is given, so a policy
change between request and answer applies; with no approvers listed any `human:<id>` may answer and
**authenticating who that human really is belongs to the API layer**, R1.22), and only with a
reason.

Only `check_action` and `ModelPolicy` record decisions; calling `core.policy.evaluate` directly
records nothing. An approved approval is consumed by the first check that uses it: with `ask` rules
on both `call_model` and `spend`, an attempt that clears the first check and then asks at the
second has already used up the first approval (fails safe, costs the human an extra answer).

`ModelPolicy` plugs the checks into the model client: `call_model(..., authorize=policy.authorize,
authorize_spend=policy.authorize_spend)`. Refusals raise `PolicyDenied`; a pending approval raises
`ApprovalRequired` (a `PolicyDenied` that carries the approval id).

Ledger payloads name the action; targets that can hold personal data or secrets (commands,
message recipients) are recorded as hashes only (A7, S2).
"""

import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from nuramesh.core.autonomy import rule_hash
from nuramesh.core.budgets import CallContext
from nuramesh.core.content import ContentKind, LedgerHashKind, MarkedLedgerHasher
from nuramesh.core.evals import EvalConfig
from nuramesh.core.ledger import EventType
from nuramesh.core.models import ModelRequest
from nuramesh.core.money import Money
from nuramesh.core.org import OrgGraph
from nuramesh.core.overlay import unavailable
from nuramesh.core.people import is_human_id
from nuramesh.core.policy import (
    NETWORK_APPROVAL_CHAIN,
    NETWORK_APPROVAL_CHAIN_TEXT,
    POLICY_CONTRACT,
    Action,
    ActionKind,
    Approval,
    ApprovalError,
    ApprovalMode,
    ApprovalOpener,
    ApprovalStatus,
    Decision,
    PolicyDecision,
    PolicySet,
    approval_due,
    check_decidable,
    consume_approval,
    decide_approval,
    evaluate,
    expire_approval,
)
from nuramesh.core.solo import SoloProof
from nuramesh.core.toolcalls import (
    ADAPTERS,
    DEADLINE_WAIT_SECONDS,
    DENIAL_LIMIT,
    ReasonCode,
    check_deadline_seconds,
    check_denial_limit,
    check_tool_name,
)
from nuramesh.engine.autonomy import (
    AutonomyUnavailable,
    ensure_config,
    overlay_check,
    required_suites_of,
)
from nuramesh.engine.content import ContentError, ContentService, LedgerHashing
from nuramesh.engine.guarded import (
    count_outcome,
    deny_draft,
    guarded_of,
    guarded_state,
    opener_fields,
)
from nuramesh.engine.models import PolicyDenied
from nuramesh.engine.originator import SelfApproval, originator_of
from nuramesh.engine.solo import (
    SoloTerms,
    check_solo_approve,
    mode_without_solo,
    standing_of,
)
from nuramesh.engine.starter import refuse_foreign_run
from nuramesh.stores.base import (
    EventDraft,
    NotFoundError,
    Store,
    StoredApproval,
    UnitOfWork,
    run_stream,
)


class ApprovalExpired(ApprovalError):
    """An answer that arrived at or after the approval's deadline (S4 AD-46): refused; the
    expiry is recorded if nobody had recorded it, and nothing else."""


class ApprovalRequired(PolicyDenied):
    """The action needs a human's approval first; `approval_id` is the open request."""

    def __init__(self, approval_id: str, reason: str) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval required ({approval_id}): {reason}")


@dataclass(frozen=True)
class Check:
    """The outcome of a policy check: what to do, and the approval involved (if any)."""

    decision: PolicyDecision
    approval_id: str | None = None

    @property
    def allowed(self) -> bool:
        """True if the action may go ahead now."""
        return self.decision.decision is Decision.ALLOW


UNREADABLE_ARGUMENTS_TEXT = "the arguments could not be read"
"""The fixed `reason` of the pre-rule deny for unreadable arguments (AD-27)."""

_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ToolCall:
    """What an adapter tool call adds to its policy check (S4 AD-26, section 16 N1): the adapter,
    the call id (AD-8), the plain arguments digest (AD-10; `None` when the arguments were
    unreadable) and the frozen `tool_set_hash` (AD-16). The digest is folded into the keyed
    `args_hash` where the ledger is written and is never itself recorded or printed."""

    adapter: str
    call_id: str
    args_digest: str | None = field(repr=False)
    tool_set_hash: str
    # False: the call will not wait for a person (AD-45, `wait=None`); its `ask` entry then
    # carries `reason_code: "ask_pending"`, which is how the soft denial is counted (AD-42)
    waits: bool = True
    # the canonical JSON of the arguments (AD-10): stored as keyed content only when this check
    # opens a new approval (AD-24a), never written to the ledger and never printed
    canonical: bytes | None = field(default=None, repr=False)
    # what a new approval keeps (AD-46, AD-42): its deadline and its guarded run's attempt and limit
    deadline_seconds: int = DEADLINE_WAIT_SECONDS
    attempt: int = 0
    denial_limit: int = DENIAL_LIMIT

    def __post_init__(self) -> None:
        check_deadline_seconds(self.deadline_seconds)
        check_denial_limit(self.denial_limit)
        if self.adapter not in ADAPTERS:
            raise ValueError("unknown adapter")
        try:
            check_tool_name(self.call_id)  # the same pattern as a tool name (AD-8)
        except ValueError:
            raise ValueError("the call id is not a readable id (AD-8)") from None
        if self.args_digest is not None and not _is_hex64(self.args_digest):
            raise ValueError("the arguments digest is 64 lower-case hex characters")
        if not _is_hex64(self.tool_set_hash):
            raise ValueError("the tool set hash is 64 lower-case hex characters")

    def ledger_fields(self, hasher: MarkedLedgerHasher) -> dict[str, str]:
        """The AD-26 fields: `adapter`, `call_id`, `tool_set_hash` and, when a digest exists, the
        keyed `args_hash` (hash kind `tool_args` over the digest's 32 bytes) with the hasher's
        marker fields."""
        fields = {
            "adapter": self.adapter,
            "call_id": self.call_id,
            "tool_set_hash": self.tool_set_hash,
        }
        if self.args_digest is not None:
            digest = bytes.fromhex(self.args_digest)
            fields["args_hash"] = hasher(LedgerHashKind.TOOL_ARGS, digest)
            fields.update(hasher.marker())
        return fields


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _new_id() -> str:
    return f"appr-{uuid.uuid4().hex[:16]}"


@contextmanager
def _unit(store: Store, uow: UnitOfWork | None) -> Iterator[UnitOfWork]:
    """The caller's unit of work, or a new transaction when there is none."""
    if uow is not None:
        yield uow
        return
    with store.transaction() as own:
        yield own


def check_action(
    store: Store,
    policies: PolicySet,
    org: OrgGraph,
    context: CallContext,
    action: Action,
    *,
    hasher: MarkedLedgerHasher,
    actor: str = "system",
    new_approval_id: Callable[[], str] = _new_id,
    required_suites: Mapping[str, frozenset[str]] | None = None,
    content: ContentService | None = None,
    tool_call: ToolCall | None = None,
    uow: UnitOfWork | None = None,
    may_ask: bool = True,
) -> Check:
    """Decide whether `context.agent_id` may do `action`, record it, and open or use an approval.
    With `tool_call` (an adapter tool call, S4) the entry also carries the AD-26 fields, a deny
    carries `reason_code: "denied_by_rule"` (AD-23) and an approval is opened for, and answers
    only, the same arguments (AD-24); without it everything is as before. With `uow` the check
    is made inside the caller's unit of work and opens no transaction (the embedded tool-call
    writer, `engine.toolcalls`, which serialises one guarded run's checks, AD-42).
    A hashed target (`run_command`, `external_message`) is hashed by `hasher` BEFORE anything is
    written: a key refusal writes no row and the action isn't taken (PH-KEY R5).

    Earned autonomy (E13): the configuration in use is recorded first (a change voids the grants
    it invalidates, in the same transaction), and an `ask` that every matching rule's active grant
    covers becomes an `allow` that names its grants; an unreadable ledger leaves it `ask`. A
    configuration that cannot be recorded is not used (the error propagates).

    `may_ask=False` (NG-11, raise-and-retry mode after an approval was consumed in this call): an
    `ask` that no grant covers is a recorded deny with `reason_code` `network_approval_chain`; no
    pending approval is reused, none is opened and an approved one is not consumed. Every other
    verdict, and `may_ask=True`, behave as before."""
    role = org.role_of(context.agent_id).id  # KeyError for an agent outside the org
    policy = policies.policy_for(role)
    suites = required_suites or {}
    grants_used: tuple[str, ...] = ()
    base: dict[str, Any] = {
        "agent": context.agent_id,
        "role": role,
        "policy_contract": POLICY_CONTRACT,
        **action.ledger_fields(hasher),
    }
    if "target_hash" in base:
        base.update(hasher.marker())
    call_fields = tool_call.ledger_fields(hasher) if tool_call is not None else {}
    if tool_call is not None and tool_call.args_digest is None:
        # AD-26/AD-27: a call whose arguments were unreadable is denied BEFORE any rule is read,
        # records no `args_hash` and never matches or opens an approval (AD-11, default deny)
        denial = PolicyDecision(decision=Decision.DENY, rule=None, reason=UNREADABLE_ARGUMENTS_TEXT)
        with store.transaction() as uow:
            refuse_foreign_run(uow, org.id, context.run_id)
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=EventType.POLICY_DECIDED,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload={
                        **base,
                        **call_fields,
                        "decision": denial.decision.value,
                        "rule": None,
                        "reason": denial.reason,
                        "reason_code": ReasonCode.UNREADABLE_ARGUMENTS.value,
                    },
                )
            )
        return Check(decision=denial, approval_id=None)
    verdict = evaluate(policy, action)
    args_hash = call_fields.get("args_hash")
    args_marker = dict(hasher.marker()) if args_hash is not None else {}
    # what the approval entries repeat of a tool call: its arguments hash, never its call id
    approval_fields = {"args_hash": args_hash, **args_marker} if args_hash is not None else {}
    approval_id: str | None = None
    final = verdict
    with _unit(store, uow) as uow:
        refuse_foreign_run(uow, org.id, context.run_id)  # before any write (#508)
        events: list[tuple[EventType, dict[str, Any]]] = []
        config_unreadable = False
        try:
            ensure_config(uow, org, policies, uow.at, required_suites=suites, content=content)
        except AutonomyUnavailable:
            # the autonomy records cannot be read: never more permissive than the plain verdict.
            # A plain deny stays a deny (default deny) and an ask stays an ask that says why
            # (spec 6.1, O22); a plain allow must not be used on an unrecorded configuration.
            if verdict.decision is Decision.ALLOW:
                raise
            config_unreadable = True
            if verdict.decision is Decision.ASK:
                final = verdict = unavailable(policy, action).decision
        if verdict.decision is Decision.ASK and not config_unreadable:
            overlaid = overlay_check(
                uow,
                org.id,
                policy,
                action,
                agent=context.agent_id,
                role=role,
                required_suites=suites,
                content=content,
            )
            if overlaid.grants:
                final, grants_used, verdict = overlaid.decision, overlaid.grants, overlaid.decision
            elif overlaid.decision.reason != verdict.reason:
                final = verdict = overlaid.decision  # unavailable: still an ask, with the reason
        if verdict.decision is Decision.ASK and not may_ask:
            final = PolicyDecision(
                decision=Decision.DENY,
                rule=None,
                reason=NETWORK_APPROVAL_CHAIN_TEXT,
                reason_code=NETWORK_APPROVAL_CHAIN,
            )
        elif verdict.decision is Decision.ASK:
            approved = [
                s
                for s in uow.approvals_matching(
                    context.run_id, context.agent_id, ApprovalStatus.APPROVED
                )
                if s.approval.answers(context.agent_id, context.task_id, action, args_hash)
                and _same_attempt(s.approval, tool_call)
                and not approval_due(s.approval, uow.at)  # never consumed past its deadline
            ]
            if approved:
                stored = approved[0]
                uow.put_approval(
                    consume_approval(stored.approval, at=uow.at), expected_version=stored.version
                )
                approval_id = stored.approval.id
                final = PolicyDecision(
                    decision=Decision.ALLOW,
                    rule=verdict.rule,
                    reason=f"approved by {stored.approval.decided_by}",
                )
            else:
                pending = [
                    s
                    for s in uow.approvals_matching(
                        context.run_id, context.agent_id, ApprovalStatus.PENDING
                    )
                    if s.approval.answers(context.agent_id, context.task_id, action, args_hash)
                    and _same_attempt(s.approval, tool_call)
                    and not approval_due(s.approval, uow.at)
                ]
                if pending:
                    approval_id = pending[0].approval.id
                    if action.redact_target and not pending[0].approval.action.redact_target:
                        # NG-18: the flag is OR-ed, never lowered; the earlier text is already out
                        uow.put_approval(
                            pending[0].approval.model_copy(update={"action": action}),
                            expected_version=pending[0].version,
                        )
                else:
                    approval_id = new_approval_id()
                    shown = {k: v for k, v in base.items() if k not in _WHO}
                    kept = _opened(uow, org.id, context, tool_call, args_hash, content, shown)
                    approval = Approval(
                        id=approval_id,
                        run_id=context.run_id,
                        task_id=context.task_id,
                        agent_id=context.agent_id,
                        action=action,
                        asked_because=verdict.reason[:500],
                        requested_at=uow.at,
                        args_hash=args_hash,
                        args_hash_marker=args_marker,
                        **kept,
                    )
                    uow.put_approval(approval, expected_version=None)
                    events.append(
                        (
                            EventType.APPROVAL_REQUESTED,
                            {
                                **base,
                                **approval_fields,
                                "approval": approval_id,
                                "why": verdict.reason,
                            },
                        )
                    )
        decided: dict[str, Any] = {
            **base,
            **call_fields,
            "decision": final.decision.value,
            "rule": final.rule,
            "reason": final.reason,
        }
        if approval_id is not None:
            decided["approval"] = approval_id
        if final.rule is not None and policy is not None and final.rule < len(policy.rules):
            decided["rule_hash"] = rule_hash(policy.rules[final.rule])
        if grants_used:
            decided["autonomy_grant"] = list(grants_used)
        if final.reason_code is not None:
            decided["reason_code"] = final.reason_code  # NG-10 (c), NG-11
        elif tool_call is not None and final.decision is Decision.DENY:
            decided["reason_code"] = ReasonCode.DENIED_BY_RULE.value
        if tool_call is not None and final.decision is Decision.ASK and not tool_call.waits:
            decided["reason_code"] = ReasonCode.ASK_PENDING.value
        events.insert(0, (EventType.POLICY_DECIDED, decided))
        for type_, payload in events:
            uow.append(
                EventDraft(
                    stream=run_stream(context.run_id),
                    type=type_,
                    actor=actor,
                    run_id=context.run_id,
                    task_id=context.task_id,
                    payload=payload,
                )
            )
    return Check(decision=final, approval_id=approval_id)


_WHO = frozenset({"agent", "role", "policy_contract"})


def _same_attempt(approval: Approval, tool_call: ToolCall | None) -> bool:
    """An approval a tool-call check opened answers only checks of the guarded run that opened
    it (S4 AD-20: two attempts never share a pending approval, AD-42: its outcome is counted
    for that guarded run). A check without a tool call is not bound to an attempt."""
    if tool_call is None:
        return True
    return approval.opener is not None and approval.opener.attempt == tool_call.attempt


def _opened(
    uow: UnitOfWork,
    org_id: str,
    context: CallContext,
    tool_call: ToolCall | None,
    args_hash: str | None,
    content: ContentService | None,
    action_fields: Mapping[str, Any],
) -> dict[str, Any]:
    """What a new approval of an adapter tool call keeps beyond its `args_hash`: the deadline on
    the store clock and its opener's fields (AD-46), and the id of the arguments stored as keyed
    content in this same unit of work (AD-24a). A failure to store them fails the check's
    transaction (F4). Nothing for any other check (owner answer Q4: no deadline)."""
    if tool_call is None or args_hash is None:
        return {}
    kept: dict[str, Any] = {
        "deadline": uow.at + timedelta(seconds=tool_call.deadline_seconds),
        "opener": ApprovalOpener(
            adapter=tool_call.adapter,
            call_id=tool_call.call_id,
            tool_set_hash=tool_call.tool_set_hash,
            attempt=tool_call.attempt,
            denial_limit=tool_call.denial_limit,
            action_fields={k: str(v) for k, v in action_fields.items()},
        ),
    }
    if tool_call.canonical is not None:
        if content is None:
            raise ContentError("the arguments of an ask need a content store (AD-24a)")
        kept["args_content"] = content.capture(
            uow,
            org_id,
            ContentKind.TOOL_ARGUMENTS,
            tool_call.canonical.decode("utf-8"),
            retention_days=content.reason_retention_days(),
            run_id=context.run_id,
        )
    return kept


def expire_approval_in(uow: UnitOfWork, stored: StoredApproval, org: OrgGraph) -> bool:
    """Record the expiry of an approval whose deadline has passed (S4 AD-46), once: the approval
    becomes `expired` and one `policy.decided` deny `approval_timeout` is appended, actor
    `system`, with the fields of the check that opened it; when that soft denial reaches the
    guarded run's limit, the `denial_limit` entry follows directly (AD-42). False, and nothing
    written, for an approval that is not due."""
    approval = stored.approval
    if not approval_due(approval, uow.at):
        return False
    uow.put_approval(expire_approval(approval, at=uow.at), expected_version=stored.version)
    if approval.opener is None or approval.args_hash is None or approval.task_id is None:
        return True
    role = org.role_of(approval.agent_id).id
    before = guarded_state(uow, guarded_of(approval))
    uow.append(
        deny_draft(
            approval.run_id,
            approval.task_id,
            "system",
            opener_fields(approval, role),
            ReasonCode.APPROVAL_TIMEOUT,
            approval=approval.id,
        )
    )
    count_outcome(uow, approval, role, before)
    return True


def expire_matching_in(
    uow: UnitOfWork,
    org: OrgGraph,
    context: CallContext,
    action: Action,
    args_hash: str | None,
    *,
    attempt: int | None = None,
) -> tuple[str, ...]:
    """A check records the expiry of every due approval it would match (AD-46: "any later check
    that would match it") and, when `attempt` names its guarded run, of every due approval a
    tool-call check of that guarded run opened, whatever its arguments (the check is that guarded
    run's sweep, AD-46; C78, decision T270a). The ids it expired, in order."""
    expired: list[str] = []
    for status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
        for stored in uow.approvals_matching(context.run_id, context.agent_id, status):
            approval = stored.approval
            own = (
                attempt is not None
                and approval.opener is not None
                and approval.opener.attempt == attempt
                and approval.task_id == context.task_id
            )
            if own or approval.answers(context.agent_id, context.task_id, action, args_hash):
                if expire_approval_in(uow, stored, org):
                    expired.append(approval.id)
    return tuple(expired)


def sweep_approvals(store: Store, org: OrgGraph, run_id: str | None = None) -> int:
    """The sweep of AD-46: record the expiry of every approval whose deadline has passed, each
    in its own transaction. Returns how many it expired."""
    count = 0
    for status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
        for seen in store.approvals(run_id=run_id, status=status):
            if seen.approval.deadline is None:
                continue
            try:
                org.role_of(seen.approval.agent_id)
            except KeyError:
                continue  # an agent outside this organization's graph: not this sweep's
            with store.transaction() as uow:
                stored = uow.get_approval(seen.approval.id)
                if stored is not None and expire_approval_in(uow, stored, org):
                    count += 1
    return count


def approval_status(store: Store, approval_id: str, org: OrgGraph) -> Approval:
    """A status read (AD-46): the approval, with its expiry recorded first if it is due."""
    with store.transaction() as uow:
        stored = uow.get_approval(approval_id)
        if stored is None:
            raise NotFoundError(f"approval '{approval_id}' doesn't exist")
        if expire_approval_in(uow, stored, org):
            stored = uow.get_approval(approval_id)
            assert stored is not None
        return stored.approval


def reveal_approval_arguments(
    store: Store,
    approval_id: str,
    *,
    actor: str,
    policies: PolicySet,
    org: OrgGraph,
    content: ContentService,
    owner: bool = False,
    why: str = "ui",
) -> str | None:
    """The arguments of a tool-call `ask`, for its approver (AD-24a): an audited reveal that
    writes `content.read` (ND171). Only a person the role's policy lets answer, or the
    organization's owner (`owner`, decided by the caller's authentication), may read them
    (ND173); never an agent. `None` when nothing is stored or it was erased or has expired."""
    stored = store.get_approval(approval_id)
    if stored is None:
        raise NotFoundError(f"approval '{approval_id}' doesn't exist")
    approval = stored.approval
    if not is_human_id(actor):
        raise ApprovalError("only a person can read the arguments of an approval request")
    policy = policies.policy_for(org.role_of(approval.agent_id).id)
    allowed = policy.approvers if policy is not None else ()
    # without listed approvers only the owner reads them (the arguments are content, ND173)
    if not owner and actor not in allowed:
        raise ApprovalError(f"{actor} may not read the arguments of this approval request")
    if approval.args_content is None:
        return None
    return content.reveal(org.id, approval.args_content, actor=actor, why=why)


def pending_approvals(store: Store, run_id: str | None = None) -> list[Approval]:
    """The approval inbox: requests waiting for a human, by id."""
    return [s.approval for s in store.approvals(run_id=run_id, status=ApprovalStatus.PENDING)]


def answer_approval(
    store: Store,
    approval_id: str,
    *,
    approver: str,
    approve: bool,
    reason: str,
    policies: PolicySet,
    org: OrgGraph,
    content: ContentService,
    hashing: LedgerHashing,
) -> Approval:
    """A human answers an approval request; recorded as `approval.decided` with who and the keyed
    **hash** of why. The reason text (required, never blank) goes to the content store in the same
    transaction, so it can be erased later without touching the ledger (T47)."""
    text = reason.strip()
    if not text:
        raise ApprovalError("an answer needs a reason (who and why are recorded)")
    with store.transaction() as uow:
        try:
            return answer_approval_in(
                uow,
                approval_id,
                approver=approver,
                approve=approve,
                reason=reason,
                policies=policies,
                org=org,
                content=content,
                hashing=hashing,
            )
        except ApprovalExpired as exc:
            late = exc  # the expiry it recorded is committed with this transaction (AD-46)
    raise late


def answer_approval_in(
    uow: UnitOfWork,
    approval_id: str,
    *,
    approver: str,
    approve: bool,
    reason: str,
    policies: PolicySet,
    org: OrgGraph,
    content: ContentService,
    hashing: LedgerHashing,
    dry_run: bool = False,
    solo: SoloTerms | None = None,
) -> Approval:
    """`answer_approval` inside the caller's unit of work (R1.35): opens no transaction.
    `dry_run` runs every refusal and returns before the reason is captured (no key is made,
    nothing is appended).

    `solo` (SA-2, SA-12) defaults to "never solo": without it the
    run's originator is refused (`SelfApproval`) even when nobody else could decide, and the
    mode written is `peer` or `no_originator`. Only the server route passes the terms, for a
    person in a cookie session; then a sole decide-holder's answer is a solo decision, whoever
    started the run, and an approve must meet step 7 (`SoloRefused` otherwise). The order is
    SA-12's: blank reason (5), self-approval (6), the solo terms (7), then the state checks (8)."""
    text = reason.strip()
    if not text:
        raise ApprovalError("an answer needs a reason (who and why are recorded)")
    stored = uow.get_approval(approval_id)
    if stored is None:
        raise NotFoundError(f"approval '{approval_id}' doesn't exist")
    approval = stored.approval
    originator = originator_of(uow, org.id, approval.run_id)
    standing = None if solo is None else standing_of(uow, org.id, approver)
    proof: SoloProof | None = None
    if solo is None or standing is None or standing.others:
        # R2.13 3a, before any state check: another person can decide (or the caller gave no
        # solo terms), so the person who started the work never decides it (SA-1, step 6)
        if originator == approver:
            raise SelfApproval
        mode = mode_without_solo(originator)
    else:
        # nobody else holds `decide`: the solo path, whoever started the run (SA-3). A reject is
        # free (SA-4); an approve meets step 7 before any state check, so a refused attempt can
        # never cause an entry. Whether the approver still holds `decide` is step 8, below.
        mode = ApprovalMode.SOLO
        if approve:
            proof = check_solo_approve(uow, org.id, approval, approver, solo)
    if approval_due(approval, uow.at):
        # S4 AD-46: an answer at or after the deadline is refused; it records the expiry if
        # nobody has, and nothing else. `answer_approval` commits that; a caller with its own
        # unit of work that lets this error roll it back leaves the expiry to the next reader.
        if not dry_run:
            expire_approval_in(uow, stored, org)
        raise ApprovalExpired(f"approval '{approval.id}' is already expired")
    policy = policies.policy_for(org.role_of(approval.agent_id).id)
    # every refusal first, then the capture (R1.37 K5: a refused answer never reaches the key
    # ring), then the decision, which repeats the same checks
    allowed = policy.approvers if policy is not None else ()
    check_decidable(approval, approver=approver, reason=text)  # step 8: pending, a human
    if standing is not None and not standing.holder:
        # step 8: read in this transaction; the route read the role outside it (B70)
        raise ApprovalError(f"{approver} may not answer approval requests any more")
    check_decidable(approval, approver=approver, allowed=allowed, reason=text)
    if dry_run:
        return approval
    reason_hash = content.capture_reason(uow, org.id, text)
    hasher = hashing.bind(uow, org.id)  # inside this unit of work, never a second one
    fields = approval.action.ledger_fields(hasher)
    if "target_hash" in fields:
        fields.update(hasher.marker())
    if approval.args_hash is not None:  # S4 AD-24: as recorded when the approval was opened
        fields.update({"args_hash": approval.args_hash, **approval.args_hash_marker})
    answered = decide_approval(
        approval,
        approver=approver,
        approve=approve,
        reason_hash=reason_hash,
        at=uow.at,
        mode=mode,
        allowed=allowed,
    )
    solo_fields = {} if proof is None else {"solo_proof": proof.value}
    uow.put_approval(answered, expected_version=stored.version)
    counts = not approve and approval.opener is not None and approval.task_id is not None
    before = guarded_state(uow, guarded_of(approval)) if counts else None
    uow.append(
        EventDraft(
            stream=run_stream(approval.run_id),
            type=EventType.APPROVAL_DECIDED,
            actor=approver,
            run_id=approval.run_id,
            task_id=approval.task_id,
            payload={
                "approval": approval.id,
                "agent": approval.agent_id,
                **fields,
                "approved": approve,
                "by": approver,
                "reason_hash": reason_hash,
                "approval_mode": mode.value,  # SA-14: on every new entry, every caller
                **solo_fields,  # only for a solo approve
            },
        )
    )
    if before is not None:  # S4 AD-42: a rejection is counted in the transaction that records it
        count_outcome(uow, approval, org.role_of(approval.agent_id).id, before)
    return answered


class ModelPolicy:
    """Policy checks for the model client (`call_model`'s `authorize` and `authorize_spend`)."""

    def __init__(
        self,
        store: Store,
        policies: PolicySet,
        org: OrgGraph,
        *,
        hasher: MarkedLedgerHasher,
        actor: str = "system",
        new_approval_id: Callable[[], str] = _new_id,
        content: ContentService | None = None,
        evals: EvalConfig | None = None,
    ) -> None:
        self._args: dict[str, Any] = {
            "hasher": hasher,
            "content": content,
            "required_suites": required_suites_of(evals),
            "actor": actor,
            "new_approval_id": new_approval_id,
        }
        self._store, self._policies, self._org = store, policies, org

    def _enforce(self, context: CallContext, action: Action) -> None:
        check = check_action(self._store, self._policies, self._org, context, action, **self._args)
        if check.allowed:
            return
        if check.decision.decision is Decision.ASK:
            assert check.approval_id is not None
            raise ApprovalRequired(check.approval_id, check.decision.reason)
        raise PolicyDenied(check.decision.reason)

    def authorize(self, context: CallContext, request: ModelRequest) -> None:
        """May this agent call this model?"""
        target = request.model_ref
        self._enforce(context, Action(kind=ActionKind.CALL_MODEL, target=target))

    def authorize_spend(self, context: CallContext, worst_case: Money) -> None:
        """May this agent start a call that could cost up to `worst_case`?"""
        self._enforce(context, Action(kind=ActionKind.SPEND, amount=worst_case))


__all__ = [
    "ApprovalError",
    "ApprovalExpired",
    "ApprovalRequired",
    "Check",
    "ModelPolicy",
    "ToolCall",
    "answer_approval",
    "approval_status",
    "check_action",
    "expire_approval_in",
    "expire_matching_in",
    "pending_approvals",
    "reveal_approval_arguments",
    "sweep_approvals",
]
