"""Merge Gate flow (spec `spec/verified-merge` GT-1, GT-4, GT-13, GT-14, GT-18, MQ-1 to MQ-3,
LA-1, LY-4, 6.4a; `gate-clarifications.md` GC-2, GC-6; `tiers.md` TL-20, TC-183).

`handle_event` is what the service (VM-4b) calls for every trigger (GT-18: forge events, every
`merge.*` ledger event for the PR, trust-list and policy changes, the expiry timer, pushes to the
protected branch (F-1), a mode change (F-12)). It is pure orchestration over a `GateHost`, whose
methods do the I/O: read the live inputs, post one check run per head (an update, never a second
run), append to the ledger. The order is the spec's and is tested:

1. mode `off`: nothing is read, written or posted (FR-5);
2. **pending-first** (F-3): on a PR opened, reopened, synchronized, marked ready, closed, or any
   change of the open-PR set at a head, and on a head that has no run yet, the head's check run is
   set to `pending reevaluating` and that is ledgered before anything else, so a success never
   outlives the facts it was computed from;
3. the head is evaluated over every open PR that shares it (`vm_gate.evaluate_head`); inputs
   that are not for the repository, head and mode asked about, or that name a PR twice, are
   `gate_error`; **before a success is ledgered or left standing the open-PR set at the head is
   read again from the forge**: if a PR moved away or another opened there while the gate
   evaluated, the result is `pending reevaluating` (`head_set_changed`, GC-6), never a success;
   the same read resolves the protected branch's tip again, and a tip that is no longer the one
   the policy and the tier list were read at is `pending reevaluating` (`base_tip_moved`, TL-20:
   the evaluation is repeated from the start);
4. **a result is posted only after its `merge.gate_decided` entry** (GT-4, LA-1): one decision per
   PR, each naming the head's check-run id; a failed append is `failure ledger_unavailable` and no
   success is posted;
5. a result is re-posted only when it changed (GT-18): the newest posted decision key is compared;
6. a failed forge update is reported (`forge_update: failed`) for the service to retry (F-4), never
   raised; the ledger already holds the decision.

The merge pin (GT-14) and the auto-merge enabler's rule (LY-4, F-3) are here too, as pure checks:
the merge call is pinned to the decision's head, and auto-merge is enabled only when the ledger's
newest `merge.gate_decided` for (repo, head) is a success that lists the PR and the forge's check
run id. `evaluate_group` is the merge-queue re-verification (MQ-1 to MQ-3) on the group head; the
flow first requires the reader to return exactly the event's members, each once, for the event's
repository and the host's mode (`group_members_mismatch`, `inputs_duplicate_pr`,
`inputs_wrong_repo`, `mode_inconsistent`, all `gate_error`).

**The window that stays open (F-3).** The forge has no "post this result only if the open-PR set
at the head is still X", so a PR opened at the head between the last read and the post (or any
time after it) sees the head's success until its own `pr_opened` event is handled, which sets the
run to pending before anything else. The last read shrinks the window to one forge round trip; it
cannot close it. What holds in that window: the success's `external_id` and its ledger entry list
the PRs it was computed for; `enable_allowed` refuses a PR that the newest entry does not list;
VM-4b's merge path must make the same test (`shared_head_prs` contains the PR) before it merges.
A human pressing merge on the forge in that window is stopped only by the forge's own review
rules, and is caught afterwards by the post-merge check (`would_be_unverified`, VM-5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from nuramesh.core.ledger import EventType
from nuramesh.core.vm_collect import CollectError, LiveCheck, Recompute
from nuramesh.core.vm_events import GATE_ACTOR, check_merge_actor
from nuramesh.core.vm_formats import GateDecision, format_time
from nuramesh.core.vm_gate import (
    NOT_ENFORCED,
    GateCallables,
    GateInputError,
    GateInputs,
    HeadOutcome,
    Outcome,
    State,
    build_decision,
    classify_check,
    evaluate_head,
    evaluate_pr,
    external_id,
    gate_decided_payload,
    gate_error_detail,
    needs_admin,
    status_name,
)
from nuramesh.core.vm_verdicts import GATE_ERROR

EventKind = Literal[
    "pr_opened",
    "pr_reopened",
    "pr_synchronize",
    "pr_ready_for_review",
    "pr_closed",
    "sharer_set_changed",
    "pr_labeled",
    "pr_unlabeled",
    "check_completed",
    "push_protected_branch",
    "ledger_merge_event",
    "trust_or_policy_changed",
    "timer",
    "setting_changed",
    "merge_group",
]

PENDING_FIRST_KINDS: frozenset[str] = frozenset(
    {
        "pr_opened",
        "pr_reopened",
        "pr_synchronize",
        "pr_ready_for_review",
        "pr_closed",
        "sharer_set_changed",
    }
)
"""F-3: the events after which the head's run is set to pending before anything is evaluated
(the open-PR set at the head, or the head itself, changed)."""

REEVALUATION_KINDS: frozenset[str] = PENDING_FIRST_KINDS | frozenset(
    {
        "pr_labeled",
        "pr_unlabeled",
        "check_completed",
        "push_protected_branch",
        "ledger_merge_event",
        "trust_or_policy_changed",
        "timer",
        "setting_changed",
        "merge_group",
    }
)
"""GT-18 with F-1 and F-12: every trigger re-evaluates; nothing else does (the gate never loops)."""

REEVALUATING = "reevaluating"


@dataclass(frozen=True)
class GateEvent:
    kind: EventKind
    repo: str
    pr: int | None
    head_sha: str
    group_prs: tuple[int, ...] = ()
    """`merge_group` only: the PRs in the group, in queue order."""


@dataclass(frozen=True)
class Posted:
    """The newest result the gate posted on a head, as the host remembers it."""

    head_sha: str
    state: str
    reason: str
    external_id: str
    run_id: str
    decision_key: str | None = None
    status_name: str | None = None


class GateHost(Protocol):
    """The I/O the flow delegates. Every method may raise; the flow fails closed."""

    mode: str

    def live_head(self, repo: str, pr: int) -> str | None:
        """The PR's current head sha, or None when it is not open."""

    def read_inputs(self, repo: str, head_sha: str) -> Sequence[GateInputs]:
        """Fresh inputs for every open PR at this head (GT-3: re-read, never cached)."""

    def open_prs_at(self, repo: str, head_sha: str) -> Sequence[int]:
        """The numbers of the PRs that are open with this head right now, read from the forge
        (never from what `read_inputs` returned): the confirmation before a success."""

    def base_tip(self, repo: str) -> str:
        """The protected branch's tip right now, resolved to one sha (TL-20)."""

    def read_group_inputs(self, repo: str, prs: Sequence[int]) -> Sequence[tuple[GateInputs, str]]:
        """For a merge group: each member's fresh inputs and the head it was enqueued with."""

    def read_checks(self, repo: str, head_sha: str) -> Sequence[LiveCheck]:
        """The live check runs and statuses on a commit (the group head)."""

    def post(
        self,
        head_sha: str,
        status_name: str,
        state: str,
        reason: str,
        external_id: str,
        decision_key: str | None,
    ) -> str:
        """Create or update the one check run of `status_name` on the head; returns its id."""

    def append(self, event_type: str, actor: str, payload: Mapping[str, Any]) -> int:
        """Append to the org stream; returns the sequence number."""

    def last_posted(self, head_sha: str) -> Posted | None:
        """What the gate last posted on this head, or None."""


@dataclass(frozen=True)
class FlowResult:
    evaluated: bool
    head_sha: str
    state: str | None
    reason: str
    detail: str | None = None
    status_name: str | None = None
    actions: tuple[str, ...] = ()
    decisions: tuple[GateDecision, ...] = ()
    posted: Posted | None = None
    forge_update: Literal["confirmed", "failed", "none"] = "none"
    valid_until: datetime | None = None


@dataclass(frozen=True)
class GroupOutcome:
    state: State
    reason: str
    detail: str | None
    members: Mapping[int, Outcome]


# --- pure checks: the merge pin and the enabler rule ----------------------------------------------


def merge_allowed(decision_head: str, live_head: str, expected_head_oid: str) -> bool:
    """GT-14: the merge call is pinned to the head the decision was made for; a head that moved
    since, or a call pinned to another sha, is refused."""
    return decision_head == live_head == expected_head_oid


def enable_allowed(
    newest: Mapping[str, Any] | None,
    *,
    pr: int,
    head_sha: str,
    forge_run_id: str,
    forge_state: str,
    tier_codes: Sequence[str] | None,
) -> bool:
    """LY-4 with F-3: auto-merge may be enabled for a PR only when the ledger's newest
    `merge.gate_decided` for (repo, head) is a success whose `shared_head_prs` contains the PR and
    whose `check_run_id` is the forge's run, and the forge shows that run as success.

    M-9 (TC-183): never for a PR that changes a trust root, whatever the gate's result says:
    `tier_codes` are the codes the enabler computed itself for the PR at the base tip
    (`vm_collect.recompute`, never the bundle's); `trust_root`, `trust_roots_invalid` or
    `tier3_path_limit` among them refuses, and so does None (not computed). Such a PR is merged
    by the owner."""
    if newest is None or forge_state != "success":
        return False
    if tier_codes is None or needs_admin(tier_codes) is not None:
        return False
    return bool(
        newest.get("state") == "success"
        and newest.get("head_sha") == head_sha
        and pr in (newest.get("shared_head_prs") or ())
        and newest.get("check_run_id") == forge_run_id
    )


# --- the event flow -------------------------------------------------------------------------------


def handle_event(
    event: GateEvent,
    host: GateHost,
    callables: GateCallables,
    *,
    now: datetime,
    gate_version: str,
) -> FlowResult:
    """One trigger, start to finish; never raises (GT-4)."""
    if event.kind not in REEVALUATION_KINDS:
        raise ValueError(f"unknown gate event kind {event.kind!r}")
    actions: list[str] = []
    try:
        mode = host.mode
        if mode == "off":
            return FlowResult(False, event.head_sha, None, NOT_ENFORCED)
        name = status_name(mode)
    except Exception:
        # an unreadable or unknown mode is a gate_error under the blocking name (GT-4)
        return _fail_closed(
            event.head_sha, status_name("enforce"), host, now, gate_version, actions, None
        )
    if event.kind == "merge_group":
        return _handle_group(
            event, host, callables, mode, now=now, gate_version=gate_version, name=name
        )
    try:
        head = event.head_sha
        if event.pr is not None:
            live = host.live_head(event.repo, event.pr)
            if live is not None:
                head = live
        last = host.last_posted(head)
    except Exception:
        return _fail_closed(event.head_sha, name, host, now, gate_version, actions, None)
    if event.kind in PENDING_FIRST_KINDS or last is None:
        pending = _pending_first(event, head, mode, name, host, now, gate_version, actions)
        if isinstance(pending, FlowResult):
            return pending
        last = pending
    try:
        inputs = list(host.read_inputs(event.repo, head))
        if not inputs:
            actions.append("no_open_pr")
            return FlowResult(
                False, head, "pending", REEVALUATING, actions=tuple(actions), posted=last
            )
        _check_inputs(inputs, event.repo, head, mode)
        outcome = evaluate_head(inputs, callables)
    except (GateInputError, CollectError) as exc:
        detail = gate_error_detail(exc.code)
        return _fail_closed(head, name, host, now, gate_version, actions, detail)
    except Exception:
        return _fail_closed(head, name, host, now, gate_version, actions, None)
    actions.append("evaluate")
    assert outcome.state is not None and outcome.external_id is not None
    if outcome.state == "success":
        # the facts moved while the gate evaluated (a push, a PR opened or closed at this head):
        # confirm the open-PR set from the forge before any success is ledgered or left standing
        moved = _confirm_head_set(event, head, mode, name, host, now, gate_version, actions, inputs)
        if moved is not None:
            return moved
    key = _head_key(outcome)
    if (
        last is not None
        and last.decision_key == key
        and (last.state, last.reason, last.external_id, last.status_name)
        == (outcome.state, outcome.reason, outcome.external_id, name)
    ):
        actions.append("unchanged")
        return FlowResult(
            True,
            head,
            outcome.state,
            outcome.reason,
            outcome.detail,
            name,
            tuple(actions),
            posted=last,
            valid_until=outcome.valid_until,
        )
    run_id = last.run_id if last is not None else None
    decisions: list[GateDecision] = []
    state: str = outcome.state
    reason, detail = outcome.reason, outcome.detail
    try:
        for item in inputs:
            per = outcome.per_pr.get(item.pr.number)
            if per is None:
                continue
            decision = build_decision(per, item, decided_at=now, gate_version=gate_version)
            _append(host, EventType.MERGE_GATE_DECIDED, gate_decided_payload(decision, per, run_id))
            decisions.append(decision)
        actions.append(f"ledger:{state}:{reason}")
    except Exception:
        state, reason, detail = "failure", "ledger_unavailable", None
        actions.append("ledger_failed")
    if state == "success":
        # the last read before the success reaches the forge: a PR opened at this head (or one
        # that left it) since the confirmation above turns the success into pending-first; the
        # pending entry is then the head's newest `merge.gate_decided`, after the success entries
        moved = _confirm_head_set(event, head, mode, name, host, now, gate_version, actions, inputs)
        if moved is not None:
            return moved
    posted = None
    forge_update: Literal["confirmed", "failed"] = "confirmed"
    try:
        new_id = host.post(head, name, state, reason, outcome.external_id, key)
        actions.append(f"post:{state}:{reason}")
        posted = Posted(head, state, reason, outcome.external_id, new_id, key, name)
    except Exception:
        forge_update = "failed"
        actions.append("post_failed")
    return FlowResult(
        True,
        head,
        state,
        reason,
        detail,
        name,
        tuple(actions),
        tuple(decisions),
        posted,
        forge_update,
        outcome.valid_until if state == "success" else None,
    )


def _confirm_head_set(
    event: GateEvent,
    head: str,
    mode: str,
    name: str,
    host: GateHost,
    now: datetime,
    gate_version: str,
    actions: list[str],
    inputs: Sequence[GateInputs],
) -> FlowResult | None:
    """GC-6, TL-20: None when the forge still shows exactly the evaluated PRs open at the head
    and the protected branch's tip is still the one every evaluation read its inputs at; else
    the result that replaces the success: `pending reevaluating` (`head_set_changed` or
    `base_tip_moved`), posted and ledgered, or the fail-closed result when the read or either
    write fails."""
    try:
        still = sorted(set(host.open_prs_at(event.repo, head)))
        tip = host.base_tip(event.repo)
    except Exception:
        return _fail_closed(head, name, host, now, gate_version, actions, None)
    read_at = {i.recompute.base_sha for i in inputs if isinstance(i.recompute, Recompute)}
    if still != sorted(i.pr.number for i in inputs):
        why = "head_set_changed"
    elif read_at != {tip}:
        why = "base_tip_moved"
    else:
        return None
    actions.append(why)
    pending = _pending_first(event, head, mode, name, host, now, gate_version, actions)
    if isinstance(pending, FlowResult):
        return pending
    return FlowResult(
        True,
        head,
        "pending",
        REEVALUATING,
        why,
        name,
        tuple(actions),
        posted=pending,
        forge_update="confirmed",
    )


def _pending_first(
    event: GateEvent,
    head: str,
    mode: str,
    name: str,
    host: GateHost,
    now: datetime,
    gate_version: str,
    actions: list[str],
) -> Posted | FlowResult:
    """F-3: the head's run is set to `pending reevaluating` and that is ledgered; a failure of
    either write is the fail-closed result (returned), never a standing success."""
    try:
        run_id = host.post(head, name, "pending", REEVALUATING, "", None)
        actions.append(f"post:pending:{REEVALUATING}")
    except Exception:
        return _fail_closed(head, name, host, now, gate_version, actions, None)
    try:
        _append(
            host,
            EventType.MERGE_GATE_DECIDED,
            _pending_payload(event, head, mode, run_id, now, gate_version),
        )
        actions.append(f"ledger:pending:{REEVALUATING}")
    except Exception:
        return _ledger_down(head, name, host, actions)
    return Posted(head, "pending", REEVALUATING, "", run_id, None, name)


def _check_inputs(inputs: Sequence[GateInputs], repo: str, head: str, mode: str) -> None:
    """GT-3, GT-4: what the reader returned must be for the repository, head and mode asked
    about, one entry per PR; anything else is `gate_error`, never evaluated as if it fitted."""
    if any(i.mode != mode for i in inputs):
        raise GateInputError("mode_inconsistent")
    if any(i.repo != repo for i in inputs):
        raise GateInputError("inputs_wrong_repo")
    if any(i.pr.head_sha != head for i in inputs):
        raise GateInputError("inputs_wrong_head")
    numbers = [i.pr.number for i in inputs]
    if len(set(numbers)) != len(numbers):
        raise GateInputError("inputs_duplicate_pr")


def _head_key(outcome: HeadOutcome) -> str:
    return "|".join(outcome.per_pr[n].key() for n in sorted(outcome.per_pr))


def _append(host: GateHost, event_type: EventType, payload: Mapping[str, Any]) -> int:
    problem = check_merge_actor(event_type.value, GATE_ACTOR)
    if problem is not None:  # pragma: no cover - the table grants the gate these types
        raise PermissionError(problem)
    return host.append(event_type.value, GATE_ACTOR, payload)


def _pending_payload(
    event: GateEvent, head: str, mode: str, run_id: str, now: datetime, gate_version: str
) -> dict[str, Any]:
    return {
        "actor": GATE_ACTOR,
        "repo": event.repo,
        "pr": event.pr,
        "head_sha": head,
        "mode": mode,
        "state": "pending",
        "reason_code": REEVALUATING,
        "trigger": event.kind,
        "check_run_id": run_id,
        "decided_at": format_time(now),
        "gate_version": gate_version,
    }


def _ledger_down(head: str, name: str, host: GateHost, actions: list[str]) -> FlowResult:
    """GT-4, G-42: the ledger did not take the entry, so the status is `failure
    ledger_unavailable` and no success is posted; a forge failure on top is only reported."""
    actions.append("ledger_failed")
    posted = None
    forge_update: Literal["confirmed", "failed"] = "confirmed"
    try:
        run_id = host.post(head, name, "failure", "ledger_unavailable", "", None)
        actions.append("post:failure:ledger_unavailable")
        posted = Posted(head, "failure", "ledger_unavailable", "", run_id, None, name)
    except Exception:
        forge_update = "failed"
        actions.append("post_failed")
    return FlowResult(
        True,
        head,
        "failure",
        "ledger_unavailable",
        None,
        name,
        tuple(actions),
        (),
        posted,
        forge_update,
    )


def _fail_closed(
    head: str,
    name: str,
    host: GateHost,
    now: datetime,
    gate_version: str,
    actions: list[str],
    detail: str | None,
) -> FlowResult:
    """GT-4: `failure gate_error`, ledgered when the ledger answers, posted when the forge does;
    neither failure is raised and no success is ever the result."""
    payload = {
        "actor": GATE_ACTOR,
        "head_sha": head,
        "state": "failure",
        "reason_code": GATE_ERROR,
        "detail": detail,
        "decided_at": format_time(now),
        "gate_version": gate_version,
    }
    try:
        _append(host, EventType.MERGE_GATE_DECIDED, payload)
        actions.append(f"ledger:failure:{GATE_ERROR}")
    except Exception:
        actions.append("ledger_failed")
    forge_update: Literal["confirmed", "failed"] = "confirmed"
    posted = None
    try:
        run_id = host.post(head, name, "failure", GATE_ERROR, "", None)
        actions.append(f"post:failure:{GATE_ERROR}")
        posted = Posted(head, "failure", GATE_ERROR, "", run_id, None, name)
    except Exception:
        forge_update = "failed"
        actions.append("post_failed")
    return FlowResult(
        True, head, "failure", GATE_ERROR, detail, name, tuple(actions), (), posted, forge_update
    )


# --- merge queue (MQ-1 to MQ-3) -------------------------------------------------------------------


def evaluate_group(
    members: Sequence[tuple[GateInputs, str]],
    *,
    group_head_sha: str,
    group_checks: Sequence[LiveCheck],
    callables: GateCallables,
) -> GroupOutcome:
    """The group head is what lands: every member's live head must be the one it was enqueued
    with (`group_head_mismatch`), every member must pass its own evaluation without the exact-head
    CI steps (`group_member_unverified` with the member's PR and code), and the members' required
    and full-tier names must be green on the group head from their pinned producers
    (`group_ci_missing`, `group_ci_pending`, `group_ci_failed`). Pure; never raises except on a
    policy without the pins."""
    outcomes: dict[int, Outcome] = {}
    if not members:
        return GroupOutcome("failure", GATE_ERROR, None, outcomes)
    for inputs, enqueued_head in members:
        if inputs.pr.head_sha != enqueued_head:
            return GroupOutcome("failure", "group_head_mismatch", str(inputs.pr.number), outcomes)
    for inputs, _ in members:
        outcome = evaluate_pr(inputs, callables, in_queue=True)
        outcomes[inputs.pr.number] = outcome
        if outcome.state != "success":
            return GroupOutcome(
                "failure",
                "group_member_unverified",
                f"{inputs.pr.number}:{outcome.reason}",
                outcomes,
            )
    names: list[str] = []
    for inputs, _ in members:
        tier = outcomes[inputs.pr.number].tier
        if tier is None:
            return GroupOutcome("failure", GATE_ERROR, None, outcomes)
        for name in inputs.policy.required_checks[str(tier)]:
            if name not in names:
                names.append(name)
        if tier in inputs.policy.full_tier_tiers:
            for name in inputs.policy.full_tier:
                if name not in names:
                    names.append(name)
    policy = members[0][0].policy
    for name in names:
        try:
            state, _run = classify_check(policy, group_checks, name, group_head_sha)
        except GateInputError as exc:
            return GroupOutcome("failure", GATE_ERROR, gate_error_detail(exc.code), outcomes)
        if state == "missing":
            return GroupOutcome("pending", "group_ci_missing", name, outcomes)
        if state == "running":
            return GroupOutcome("pending", "group_ci_pending", name, outcomes)
        if state == "tied":  # GC-3: equal newest times that do not agree are failed
            return GroupOutcome("failure", "group_ci_failed", f"{name}:runs_tied", outcomes)
        if state in ("failed", "wrong_producer"):
            return GroupOutcome("failure", "group_ci_failed", f"{name}:{state}", outcomes)
    return GroupOutcome("success", "ok", None, outcomes)


def _check_group(members: Sequence[tuple[GateInputs, str]], event: GateEvent, mode: str) -> None:
    """GT-3, GT-4, MQ-2: the reader must return exactly the group's members, each once, for the
    repository and mode asked about. A member left out would land unevaluated, so anything else
    is `gate_error`, never a group evaluated as if it were complete."""
    inputs = [item for item, _ in members]
    if any(i.mode != mode for i in inputs):
        raise GateInputError("mode_inconsistent")
    if any(i.repo != event.repo for i in inputs):
        raise GateInputError("inputs_wrong_repo")
    numbers = [i.pr.number for i in inputs]
    if len(set(numbers)) != len(numbers):
        raise GateInputError("inputs_duplicate_pr")
    if sorted(numbers) != sorted(event.group_prs):
        raise GateInputError("group_members_mismatch")


def _group_tip_moved(
    event: GateEvent,
    head: str,
    name: str,
    mode: str,
    host: GateHost,
    now: datetime,
    gate_version: str,
    actions: list[str],
    members: Sequence[tuple[GateInputs, str]],
) -> FlowResult | None:
    """TL-20, MQ-2, MQ-4: None while the base tip is still the one every member's inputs were
    read at; else pending `reevaluating` (`base_tip_moved`), or the fail-closed result."""
    try:
        tip = host.base_tip(event.repo)
    except Exception:
        return _fail_closed(head, name, host, now, gate_version, actions, None)
    read_at = {i.recompute.base_sha for i, _ in members if isinstance(i.recompute, Recompute)}
    if read_at == {tip}:
        return None
    actions.append("base_tip_moved")
    pending = _pending_first(event, head, mode, name, host, now, gate_version, actions)
    if isinstance(pending, FlowResult):
        return pending
    return FlowResult(
        True,
        head,
        "pending",
        REEVALUATING,
        "base_tip_moved",
        name,
        tuple(actions),
        posted=pending,
        forge_update="confirmed",
    )


def _handle_group(
    event: GateEvent,
    host: GateHost,
    callables: GateCallables,
    mode: str,
    *,
    now: datetime,
    gate_version: str,
    name: str,
) -> FlowResult:
    actions: list[str] = []
    head = event.head_sha
    try:
        members = list(host.read_group_inputs(event.repo, event.group_prs))
        _check_group(members, event, mode)
        checks = list(host.read_checks(event.repo, head))
        outcome = evaluate_group(
            members, group_head_sha=head, group_checks=checks, callables=callables
        )
    except (GateInputError, CollectError) as exc:
        detail = gate_error_detail(exc.code)
        return _fail_closed(head, name, host, now, gate_version, actions, detail)
    except Exception:
        return _fail_closed(head, name, host, now, gate_version, actions, None)
    actions.append("evaluate_group")
    if outcome.state == "success":
        moved = _group_tip_moved(event, head, name, mode, host, now, gate_version, actions, members)
        if moved is not None:
            return moved
    ext = external_id(event.repo, head, event.group_prs)
    state: str = outcome.state
    reason, detail = outcome.reason, outcome.detail
    payload = {
        "actor": GATE_ACTOR,
        "repo": event.repo,
        "group_head_sha": head,
        "prs": list(event.group_prs),
        "mode": mode,
        "state": state,
        "reason_code": reason,
        "detail": detail,
        "members": {str(n): [o.state, o.reason] for n, o in outcome.members.items()},
        "decided_at": format_time(now),
        "gate_version": gate_version,
    }
    try:
        _append(host, EventType.MERGE_QUEUE_REVERIFIED, payload)
        actions.append(f"ledger:{state}:{reason}")
    except Exception:
        state, reason, detail = "failure", "ledger_unavailable", None
        actions.append("ledger_failed")
    if state == "success":  # the last read before the success reaches the forge (single path)
        moved = _group_tip_moved(event, head, name, mode, host, now, gate_version, actions, members)
        if moved is not None:
            return moved
    posted = None
    forge_update: Literal["confirmed", "failed"] = "confirmed"
    try:
        run_id = host.post(head, name, state, reason, ext, None)
        actions.append(f"post:{state}:{reason}")
        posted = Posted(head, state, reason, ext, run_id, None, name)
    except Exception:
        forge_update = "failed"
        actions.append("post_failed")
    return FlowResult(
        True, head, state, reason, detail, name, tuple(actions), (), posted, forge_update
    )
