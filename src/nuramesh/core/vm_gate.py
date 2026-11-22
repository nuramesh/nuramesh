"""Merge Gate evaluator (spec `spec/verified-merge` GT-1 to GT-19, QA-1 to QA-3, QA-6, EB-4,
EB-8, EB-9, 6.4, 6.4a, 7.1; `tiers.md` TL-20, TL-21, TL-29, TL-31, TL-32; `gate-clarifications.md`
GC-1 to GC-7, GC-12, GC-15).

A pure function from what the gate re-read at call time to a decision. Nothing here talks to the
forge, the repository, the signer or the ledger: the service (VM-4b) reads the live facts into
`GateInputs` (the gate's own recomputation is `vm_collect.recompute`, the one computation the
collector uses, TL-24) and supplies git and signature work as `GateCallables` (the `mt-1` runner
lives in `nuramesh.engine.vm_merge_tree`). The evaluation runs the fixed order of 7.1 with trust
roots at step 3b (GC-12): the first step that does not pass decides, a pending result stops like
a failure (GT-3), and any exception or missing input is `failure gate_error` (GT-4). Every reason
code is checked against the 7.1 table of `vm_verdicts.EVALUATION_ORDER` before it is returned.

What the evaluator trusts: only its own inputs. The bundle's `head_sha`, `tree_sha`,
`change_hash`, `tier_list_sha256`, `policy_sha256`, `tier` and `tier_reasons` are compared with
the gate's own recomputation (GT-7, TL-20); step 3b reads the gate's own codes, never the
bundle's (GT-12); the bundle's `merge_tree` fields are never read (GT-15 b); check runs are the
forge's live list, the newest run per (name, pinned producer, head) decides (GC-3); the policy,
the tier list, the trust list and CODEOWNERS must have been read at the live base tip (EB-2),
otherwise `gate_error`. Approvals arrive judged for signature, binding, window and withdrawal
(VM-3's evaluator, the `ignore` field); the gate applies what only it can see: the PR's author,
the last-pusher rule, distinctness and code-owner coverage (QA-2, QA-6).

Head sharing (GT-1): `evaluate_head` evaluates every open PR that shares the head (same head sha,
head branch in the same repository, base = the protected branch) and gives success on the head
only when all pass; a fork PR or a PR to another base at the same sha is not evaluated and cannot
block.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.vm_carry import Commit, carry_forward
from nuramesh.core.vm_collect import CollectError, LiveCheck, PrFacts, Recompute
from nuramesh.core.vm_diff import DIFF_PIN
from nuramesh.core.vm_events import GATE_ACTOR
from nuramesh.core.vm_formats import (
    CommitEntry,
    EvidenceBundle,
    FormatError,
    GateDecision,
    Policy,
    Quorum,
    TrustList,
    format_time,
    parse_canonical,
    parse_time,
    payload_hash,
)
from nuramesh.core.vm_tiers import CAUSE_CODES
from nuramesh.core.vm_verdicts import (
    GATE_ERROR,
    OK,
    GateView,
    Rebind,
    ResetRecord,
    StepResult,
    UnparsedRecord,
    VerdictRecord,
    Verify,
    evaluate_verdicts,
    resolve_order,
)

State = Literal["success", "failure", "pending"]
Mode = Literal["off", "shadow", "enforce"]
HumanQuorum = Literal["evaluate", "not_built"]

GATE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATUS_NAMES: Mapping[str, str] = {"shadow": "verified-merge-shadow", "enforce": "verified-merge"}
NOT_ENFORCED = "not_enforced"
TIER_NOT_ENFORCED = "tier_not_enforced"

GATE_ERROR_DETAILS = frozenset(
    {
        "inputs_duplicate_pr",
        "inputs_wrong_repo",
        "inputs_wrong_head",
        "mode_inconsistent",
        "group_members_mismatch",
        "tier_list_read_error",
        "tier_compute_error",
        "repo_object_format_unsupported",
        "policy_invalid",
    }
)
"""README 7.1, GC-2: every detail a `gate_error` may carry. Any other failure (an exception, a
missing input, a trust list its parser refused: case GC-44) is a `gate_error` with no detail."""

COLLECT_ERROR_DETAILS: Mapping[str, str] = {
    "policy_invalid": "policy_invalid",
    # TL-29 / TC-178: "a read error of the blob is gate_error too" with the detail of 7.1 that
    # names the policy: there is no policy to evaluate with (round-2 verdict, size limits)
    "policy_read_error": "policy_invalid",
    "tier_list_read_error": "tier_list_read_error",
    "tier_compute_error": "tier_compute_error",
    "repo_object_format_unsupported": "repo_object_format_unsupported",
    # TL-32 "its output does not parse": the patch is not the pinned render, or the patch and
    # the raw path set disagree; the gate's own computation failed
    "diff_invalid": "tier_compute_error",
    "paths_mismatch": "tier_compute_error",
}
"""The `gate_error` detail for a `vm_collect.CollectError` code of the gate's own base-tip reads
and recomputation; a code not listed (`inputs_not_from_base_tip`) has no
detail. `merge_base_ambiguous` is not a `gate_error`: it is `bundle_invalid` (section 4)."""

ADMIN_CODES: tuple[str, ...] = ("trust_root", "trust_roots_invalid", "tier3_path_limit")
"""TL-20, TL-29, TL-31 (c): the gate's own tier codes that make a PR a trust-root change for
step 3b, in the order the detail of `trust_root_needs_admin` names the first one found."""

DETERMINISTIC_CODES = frozenset(
    {
        "trust_root",
        "trust_roots_invalid",
        "tier3_path",
        "tier3_submodule",
        "tier3_path_unreadable",
        "tier3_path_limit",
        *CAUSE_CODES,
    }
)
"""TL-20 rule 3a: the codes that follow from the commits and the base tip alone; the gate's set
of these must be a subset of the bundle's `tier_reasons`."""

GREEN = frozenset({"success"})
RUNNING = frozenset({"pending", "in_progress", "queued"})


def status_name(mode: str) -> str:
    """FR-2, FR-3: the check-run name for a mode that posts one."""
    return STATUS_NAMES[mode]


def gate_error_detail(code: str | None) -> str | None:
    """The detail a `gate_error` may carry for a code (README 7.1), else None."""
    if code is None:
        return None
    code = COLLECT_ERROR_DETAILS.get(code, code)
    return code if code in GATE_ERROR_DETAILS else None


def needs_admin(tier_codes: Iterable[str]) -> str | None:
    """Step 3b and the auto-merge rule (GT-12, M-9, TL-20; TC-183): the first of the gate's own
    codes that makes the PR a trust-root change, or None. A caller passes codes it computed
    itself (`vm_collect.recompute`), never the bundle's `tier_reasons`."""
    codes = set(tier_codes)
    for code in ADMIN_CODES:
        if code in codes:
            return code
    return None


def is_running(run: LiveCheck) -> bool:
    """A run that has not concluded (GT-8: `ci_pending`)."""
    return run.conclusion in RUNNING or (run.completed_at is None and run.conclusion not in GREEN)


def run_time(run: LiveCheck) -> datetime | None:
    """GC-3: a run's time is its `completed_at`, or its `started_at` when it has not completed."""
    return run.completed_at or run.started_at


def external_id(repo: str, head_sha: str, prs: Iterable[int]) -> str:
    """GT-1: `<repo>@<head>#<pr>[,<pr>...]`, the PRs the result was evaluated for."""
    return f"{repo}@{head_sha}#{','.join(str(n) for n in sorted(set(prs)))}"


# --- inputs ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalView:
    """A signed human approval after VM-3's judgement: `ignore` is None when its signature,
    binding (BR for another head), window and ledger entry are fine, else the reason (QA-7, QA-9:
    `bad_signature`, `wrong_head`, `expired`, `withdrawn`, `not_in_ledger`, `wrong_binding`)."""

    approval_id: str
    approver_id: str
    roles: frozenset[str]
    head_sha: str
    ignore: str | None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class GateCallables:
    """The work the evaluator delegates: strict Ed25519 (engine), the `mt-1` runner's
    `in_base_history`, `merge_tree` and `read_commit` (engine; `read_commit` gives a commit's
    parents and tree as the gate's own git has them, or None for a commit it cannot find,
    GC-15 e), and the signer's rebind (service; None when no signer is configured)."""

    verify: Verify
    read_commit: Callable[[str], tuple[Sequence[str], str] | None]
    in_base_history: Callable[[str], bool]
    merge_tree: Callable[[str, str], str | None]
    rebind: Rebind | None = None


@dataclass(frozen=True)
class GateInputs:
    """Everything one evaluation of one PR reads, as re-read at call time."""

    org_id: str
    repo: str
    repo_id: str
    protected_branch: str
    mode: Mode
    now: datetime
    pr: PrFacts
    sharers: tuple[PrFacts, ...]
    policy: Policy
    """The policy the gate read at the live base tip (TL-29); its hash must be the
    recomputation's `policy_sha256`."""
    trust: TrustList
    inputs_read_at: str
    """The base-tip sha the policy, trust list, tier list and CODEOWNERS were read at (EB-2)."""
    recompute: Recompute | CollectError | None
    """`vm_collect.recompute` for this PR at the live base tip, or the `CollectError` it (or the
    tier-list read) raised: a failure of the gate's own computation is judged in step 3, after
    eligibility (TL-32). None: missing, `gate_error`."""
    live_bundle: bytes | None
    """The bytes stored under the key (repo, PR, live head), or None (GT-6)."""
    ledgered_bundle_hashes: frozenset[str]
    checks: tuple[LiveCheck, ...]
    verdicts: tuple[VerdictRecord | UnparsedRecord, ...]
    """Every record the reader found for (org, repository, PR): the signer's copies, or,
    forge-only, the comment copies with the pointers and malformed records (GC-15 b)."""
    resets: tuple[ResetRecord, ...]
    approvals: tuple[ApprovalView, ...]
    owners_of: Callable[[str], frozenset[str]]
    """CODEOWNERS at the base tip, resolved through the org's team table to human ids (QA-6)."""
    task_key: str | None = None
    human_quorum: HumanQuorum = "evaluate"
    """`not_built` until VM-3 lands: a tier that wants a human (or a codeowner) is `pending
    quorum_unmet` with `ignored: human_quorum not_built` (QA-3, item 12, PROVISIONAL); a tier
    with no human part is decided by the AI part. `enforce` is refused by the service too (FR-6)."""


# --- outcome --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """One PR's result: the 7.1 state and reason, the step that decided, and every input used
    (GT-13)."""

    pr: int
    head_sha: str
    state: State
    reason: str
    step: str
    detail: str | None = None
    tier: int | None = None
    tier_list_sha256: str | None = None
    """TL-21: the hash of the tier list the decision rests on (the live bundle's, which TL-20
    made equal to the gate's own read); None when no bundle passed step 3."""
    bundle_hash: str | None = None
    counted_verdicts: tuple[str, ...] = ()
    counted_approvals: tuple[str, ...] = ()
    ignored: tuple[tuple[str, str], ...] = ()
    status_ids: tuple[str, ...] = ()
    shared_head_prs: tuple[int, ...] = ()
    would_be: tuple[State, str] | None = None
    """GT-19: the full result when the tier is not enforced and `tier_not_enforced` was posted."""
    valid_until: datetime | None = None
    """GT-18 a: the earliest `expires_at` of a counted verdict or approval, the re-evaluation
    timer; None when nothing counted."""
    needs_admin: str | None = None
    """The gate's own code that makes this PR a trust-root change (`needs_admin`), or None; also
    None when the gate's computation never ran. The auto-merge enabler refuses such a PR
    whatever the result (M-9, TC-183)."""

    def key(self) -> str:
        """What GT-18 compares: a new result is posted and ledgered only when this changes."""
        body: dict[str, Any] = {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "counted_verdicts": list(self.counted_verdicts),
            "counted_approvals": list(self.counted_approvals),
            "ignored": [list(pair) for pair in self.ignored],
            "status_ids": list(self.status_ids),
            "bundle_hash": self.bundle_hash,
            "shared_head_prs": list(self.shared_head_prs),
            "would_be": list(self.would_be) if self.would_be else None,
        }
        return hashlib.sha256(canonical_json(body)).hexdigest()


@dataclass(frozen=True)
class HeadOutcome:
    """GT-1: the one result for a head, over every open PR that shares it."""

    evaluated: bool
    head_sha: str
    state: State | None
    reason: str
    detail: str | None
    status_name: str | None
    external_id: str | None
    shared_head_prs: tuple[int, ...]
    per_pr: Mapping[int, Outcome]
    valid_until: datetime | None = None


class _Stop(Exception):
    def __init__(self, step: str, state: State, reason: str, detail: str | None = None) -> None:
        self.step, self.state, self.reason, self.detail = step, state, reason, detail


@dataclass
class _Ctx:
    tier: int | None = None
    bundle: EvidenceBundle | None = None
    counted_verdicts: list[str] = field(default_factory=list)
    counted_approvals: list[str] = field(default_factory=list)
    ignored: list[tuple[str, str]] = field(default_factory=list)
    status_ids: list[str] = field(default_factory=list)
    valid_until: datetime | None = None
    records: dict[str, VerdictRecord] = field(default_factory=dict)
    needs_admin: str | None = None

    def lapse(self, when: datetime | None) -> None:
        if when is not None and (self.valid_until is None or when < self.valid_until):
            self.valid_until = when


# --- the evaluation -------------------------------------------------------------------------------


def evaluate_pr(inputs: GateInputs, callables: GateCallables, *, in_queue: bool = False) -> Outcome:
    """Steps 2 to 9 of 7.1 for one PR (step 1, the flag, and head sharing are `evaluate_head`'s;
    step 10, the ledger, is the flow's). With `in_queue` the exact-head CI steps (4, 5) and the
    fresh-base step (9) are left to the group head (MQ-2, MQ-3). Never raises."""
    ctx = _Ctx()
    state: State
    detail: str | None
    try:
        _run_steps(inputs, callables, ctx, in_queue=in_queue)
        step, state, reason, detail = "7", "success", OK, None
    except _Stop as stop:
        step, state, reason, detail = stop.step, stop.state, stop.reason, stop.detail
    except (GateInputError, CollectError) as exc:
        step, state, reason, detail = "any", "failure", GATE_ERROR, gate_error_detail(exc.code)
    except Exception:  # GT-4: fail closed; nothing of the exception reaches the result
        step, state, reason, detail = "any", "failure", GATE_ERROR, None
    try:
        if state != "success":
            resolve_order([StepResult(step, state, reason)])
    except ValueError:
        step, state, reason, detail = "any", "failure", GATE_ERROR, None
    would_be: tuple[State, str] | None = None
    if (
        inputs.mode == "enforce"
        and reason != GATE_ERROR
        and ctx.tier is not None
        and ctx.tier not in inputs.policy.enforce_tiers
    ):
        would_be = (state, reason)
        state, reason, detail = "success", TIER_NOT_ENFORCED, None
    return Outcome(
        pr=inputs.pr.number,
        head_sha=inputs.pr.head_sha,
        state=state,
        reason=reason,
        step=step,
        detail=detail,
        tier=ctx.tier,
        tier_list_sha256=ctx.bundle.tier_list_sha256 if ctx.bundle is not None else None,
        bundle_hash=ctx.bundle.bundle_hash if ctx.bundle is not None else None,
        counted_verdicts=tuple(ctx.counted_verdicts),
        counted_approvals=tuple(ctx.counted_approvals),
        ignored=tuple(ctx.ignored),
        status_ids=tuple(ctx.status_ids),
        shared_head_prs=(inputs.pr.number,),
        would_be=would_be,
        valid_until=ctx.valid_until if state == "success" else None,
        needs_admin=ctx.needs_admin,
    )


def _run_steps(inputs: GateInputs, c: GateCallables, ctx: _Ctx, *, in_queue: bool) -> None:
    pr = inputs.pr
    # TL-29 (i), TC-178: the policy is read before step 2, so an unusable policy is answered
    # before draft, eligibility or any later check (PROVISIONAL: owner to confirm, D1)
    early = inputs.recompute
    if isinstance(early, CollectError) and early.code in ("policy_invalid", "policy_read_error"):
        raise early
    # step 2: eligibility (GT-5)
    if pr.state != "open" or pr.base_ref != inputs.protected_branch:
        raise _Stop("2", "failure", "pr_not_eligible")
    if pr.head_repo_id != inputs.repo_id:
        raise _Stop("2", "failure", "fork_head")
    if pr.draft:
        raise _Stop("2", "pending", "pr_draft")
    if pr.open_ask or any(label in inputs.policy.hold_labels for label in pr.labels):
        raise _Stop("2", "failure", "hold_label")
    # the gate's own recomputation and the base-tip inputs (GT-7, EB-2); missing: fail closed
    rc = inputs.recompute
    if isinstance(rc, CollectError):
        if rc.code == "merge_base_ambiguous":  # section 4, GT-6: its own code, not gate_error
            raise _Stop("3", "failure", "bundle_invalid", "merge_base_ambiguous")
        raise rc  # TL-32, TL-9, TL-3: no tier is invented; gate_error with the listed detail
    if rc is None or rc.head_sha != pr.head_sha:
        raise _Stop("any", "failure", GATE_ERROR)
    if not (inputs.inputs_read_at == rc.base_sha == rc.read_at):
        raise _Stop("any", "failure", GATE_ERROR)  # EB-2: not read at the live base tip
    if payload_hash(inputs.policy) != rc.policy_sha256:
        raise _Stop("any", "failure", GATE_ERROR)  # two policies in one evaluation
    ctx.needs_admin = needs_admin(rc.tier_codes)
    # step 3: the live bundle (GT-6, GT-7, EB-8)
    bundle = _bundle(inputs, rc, ctx)
    # step 3b: trust roots (GT-12, GC-12), from the gate's own codes
    _trust_roots(inputs, ctx, bundle)
    tier = ctx.tier
    assert tier is not None
    if not in_queue:
        # step 4: required checks by tier, pinned producers, exact head, newest first (GT-8)
        for name in inputs.policy.required_checks[str(tier)]:
            _required(inputs, name, pr.head_sha, ctx, prefix="ci")
        # step 5: the full tier and its proof binding (GT-8, GT-9)
        if tier in inputs.policy.full_tier_tiers:
            for name in inputs.policy.full_tier:
                _full_tier(inputs, name, ctx)
    # step 6: the verdict check (GT-10) through the binding evaluator
    quorum = inputs.policy.quorum[str(tier)]
    verdicts = _verdicts(inputs, c, ctx, bundle, rc, quorum)
    # step 7: quorum and approvals (GT-11, QA-1 to QA-3, QA-6)
    _quorum(inputs, c, ctx, bundle, rc, quorum, verdicts)
    # step 9: fresh base (GT-14), outside a queue
    if not in_queue and not rc.head_contains_base:
        raise _Stop("9", "pending", "behind_main")


def _bundle(inputs: GateInputs, rc: Recompute, ctx: _Ctx) -> EvidenceBundle:
    if inputs.live_bundle is None:
        raise _Stop("3", "pending", "bundle_missing")
    try:
        bundle = parse_canonical(inputs.live_bundle, EvidenceBundle)
    except FormatError as exc:
        raise _Stop("3", "failure", "bundle_invalid", _format_detail(exc)) from None
    if not bundle.hash_ok():
        raise _Stop("3", "failure", "bundle_invalid", "bundle_hash")
    if bundle.pins.diff != DIFF_PIN:
        # README section 4 (#944): a bundle of a superseded pin still parses (format 1 is never
        # edited) and is refused here, before any of its values is compared: its change hash was
        # made with an encoding that is not injective, and the gate's own is made with `DIFF_PIN`
        raise _Stop("3", "failure", "bundle_invalid", "diff_pin_superseded")
    if (bundle.org_id, bundle.repo, bundle.repo_id, bundle.pr) != (
        inputs.org_id,
        inputs.repo,
        inputs.repo_id,
        inputs.pr.number,
    ):
        raise _Stop("3", "failure", "bundle_invalid", "wrong_pr")
    if bundle.bundle_hash not in inputs.ledgered_bundle_hashes:
        raise _Stop("3", "pending", "bundle_missing", "not_in_ledger")
    ttl = timedelta(seconds=inputs.policy.bundle_ttl_seconds)
    if parse_time(bundle.collected_at) + ttl <= inputs.now:
        raise _Stop("3", "pending", "bundle_missing", "bundle_expired")
    if bundle.head_sha != rc.head_sha or bundle.tree_sha != rc.tree_sha:
        raise _Stop("3", "failure", "bundle_wrong_head")
    if bundle.change_hash != rc.change_hash:
        raise _Stop("3", "failure", "diff_mismatch")
    # TL-20, in its order. Same commit and other bytes is a false claim; another commit means
    # the list or the effective policy moved on the protected branch and the bundle is not live
    same_tip = bundle.base_sha == rc.base_sha
    if rc.tier_list_sha256 != bundle.tier_list_sha256:
        if same_tip:
            raise _Stop("3", "failure", "tier_mismatch", "tier_list_hash")  # rule 1
        raise _Stop("3", "pending", "bundle_missing", "tier_list_changed")  # rule 2
    if rc.policy_sha256 != bundle.policy_sha256:  # rule 2a
        if same_tip:
            raise _Stop("3", "failure", "tier_mismatch", "policy_hash")
        raise _Stop("3", "pending", "bundle_missing", "policy_changed")
    if rc.tier > bundle.tier:
        raise _Stop("3", "failure", "tier_mismatch", "tier")  # rule 3
    if not (DETERMINISTIC_CODES & set(rc.tier_codes)) <= set(bundle.tier_reasons):
        raise _Stop("3", "failure", "tier_mismatch", "tier_reasons")  # rule 3a
    ctx.bundle = bundle
    ctx.tier = bundle.tier  # EB-4: higher than computed is allowed, never lower
    return bundle


_TIME_KEYS = frozenset({"collected_at", "completed_at", "started_at"})


def _format_detail(exc: FormatError) -> str:
    """GC-15 f: a stored bundle the schema refused for one of its times is `time_invalid`; any
    other refusal carries the parser's code (never the input)."""
    cause = exc.__cause__
    if exc.code == "schema" and isinstance(cause, ValidationError):
        if any(error["loc"] and error["loc"][-1] in _TIME_KEYS for error in cause.errors()):
            return "time_invalid"
    return exc.code


def _trust_roots(inputs: GateInputs, ctx: _Ctx, bundle: EvidenceBundle) -> None:
    """Step 3b (GT-12, TL-20): a PR whose own codes, as the gate computed them over its own path
    set, hold `trust_root`, `trust_roots_invalid` (TL-29: the policy cannot say which paths it
    meant to protect, so every PR) or `tier3_path_limit` (TL-31 c: the gate cannot show that no
    trust root changed) needs a `trust_root_admin` who is not the author. The bundle's
    `tier_reasons` is never read here. The detail is that code, never a path."""
    code = ctx.needs_admin
    if code is None:
        return
    author = bundle.author.id
    for view in inputs.approvals:
        if view.ignore is None and "trust_root_admin" in view.roles and view.approver_id != author:
            return
    raise _Stop("3b", "failure", "trust_root_needs_admin", code)


class GateInputError(ValueError):
    """What the gate was given cannot be evaluated: `gate_error`, never a pass. `code` is a
    detail of README 7.1 (`gate_error_detail` drops any other)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


CheckState = Literal["missing", "wrong_producer", "running", "failed", "tied", "ok"]


def classify_check(
    policy: Policy, checks: Iterable[LiveCheck], name: str, head_sha: str
) -> tuple[CheckState, LiveCheck | None]:
    """GT-8 with GC-3 for one required name on one head: among the forge's live runs of that name
    from the pinned producer (kind and id), the newest by its latest time decides: `running`,
    `failed` or `ok` (the run is returned); none from the pinned producer is `wrong_producer`
    when another producer reports the name, else `missing`. GC-3: runs that share the latest time
    and do not all have the same conclusion (or of which one is still running) are `tied`, which
    every caller treats as failed with the detail `runs_tied`: the gate does not guess which is
    newer. The run returned is the one with the largest id (decimal ids as integers; an absent
    or non-decimal id is smaller than every decimal id). A name without a pin raises
    `GateInputError("policy_invalid")`."""
    producer = policy.producers.get(name)
    if producer is None:
        raise GateInputError("policy_invalid")  # a required name nobody is pinned for
    pinned: list[LiveCheck] = []
    others = False
    for run in checks:
        if run.name != name or run.head_sha != head_sha:
            continue
        if run.kind == producer.kind and run.producer_id == producer.id:
            pinned.append(run)
        else:
            others = True
    if not pinned:
        return ("wrong_producer" if others else "missing"), None
    floor = datetime.min.replace(tzinfo=(run_time(pinned[0]) or datetime.min).tzinfo)
    pinned.sort(key=lambda run: (run_time(run) or floor, _run_order(run.run_id)), reverse=True)
    newest = pinned[0]
    latest = run_time(newest) or floor
    sharing = [run for run in pinned if (run_time(run) or floor) == latest]
    states = {_run_state(run) for run in sharing}
    if len(states) > 1 or (states != {"running"} and len({run.conclusion for run in sharing}) > 1):
        return "tied", newest  # the time cannot say which result is the newer one
    return states.pop(), newest


def _run_state(run: LiveCheck) -> Literal["running", "failed", "ok"]:
    if is_running(run):
        return "running"
    return "ok" if run.conclusion in GREEN else "failed"


def _run_order(run_id: str | None) -> tuple[int, int, str]:
    """The tie-break among runs with one time: a forge's numeric ids compare as numbers (`10`
    after `9`) and sort above any other id; other ids compare as text."""
    text = run_id or ""
    if text.isascii() and text.isdecimal():
        return (1, int(text), "")
    return (0, 0, text)


def _required(inputs: GateInputs, name: str, head_sha: str, ctx: _Ctx, *, prefix: str) -> None:
    state, newest = classify_check(inputs.policy, inputs.checks, name, head_sha)
    if state == "wrong_producer":
        raise _Stop("4", "failure", f"{prefix}_wrong_producer")
    if state == "missing":
        raise _Stop("4", "pending", f"{prefix}_missing")
    if state == "running":
        raise _Stop("4", "pending", f"{prefix}_pending")
    if state == "tied":
        raise _Stop("4", "failure", f"{prefix}_failed", "runs_tied")
    if state == "failed":
        raise _Stop("4", "failure", f"{prefix}_failed")
    if newest is not None and newest.run_id is not None:
        ctx.status_ids.append(newest.run_id)


def _full_tier(inputs: GateInputs, name: str, ctx: _Ctx) -> None:
    pr = inputs.pr
    state, newest = classify_check(inputs.policy, inputs.checks, name, pr.head_sha)
    if state == "wrong_producer":
        raise _Stop("5", "failure", "full_ci_unbound", "wrong_producer")
    if state == "missing":
        raise _Stop("5", "pending", "full_ci_missing")
    if state == "running":
        raise _Stop("5", "pending", "full_ci_missing", "running")
    if state == "tied":
        raise _Stop("5", "failure", "full_ci_failed", "runs_tied")
    if state == "failed" or newest is None:
        raise _Stop("5", "failure", "full_ci_failed")
    if newest.binding != (pr.number, inputs.protected_branch, pr.head_sha):
        raise _Stop("5", "failure", "full_ci_unbound", "binding")
    if pr.last_base_change_at is not None and (
        newest.started_at is None or newest.started_at <= pr.last_base_change_at
    ):
        raise _Stop("5", "failure", "full_ci_unbound", "started_before_retarget")
    if newest.run_id is not None:
        ctx.status_ids.append(newest.run_id)


def _verdicts(
    inputs: GateInputs,
    c: GateCallables,
    ctx: _Ctx,
    bundle: EvidenceBundle,
    rc: Recompute,
    quorum: Quorum,
) -> list[VerdictRecord]:
    view = GateView(
        org_id=inputs.org_id,
        repo=inputs.repo,
        repo_id=inputs.repo_id,
        pr=inputs.pr.number,
        base_ref=inputs.protected_branch,
        head_sha=inputs.pr.head_sha,
        change_hash=rc.change_hash,
        policy_sha256=rc.policy_sha256,
        tier=bundle.tier,
        now=inputs.now,
        ledgered_bundle_hashes=inputs.ledgered_bundle_hashes,
        task_key=inputs.task_key,
        carry_forward=inputs.policy.carry_forward,
    )
    chain = [Commit(e.sha, tuple(e.parents), e.tree, e.merge_tree) for e in bundle.commits]
    records = {r.id: r for r in inputs.verdicts if isinstance(r, VerdictRecord)}

    def br(record_head: str) -> str:
        # Record and live share one change hash and policy hash on purpose: evaluate_verdicts
        # already filtered candidates by change_hash and the SV-5 policy binding (BR c/d).
        return carry_forward(
            chain,
            record_head=record_head,
            live_head=inputs.pr.head_sha,
            read_commit=c.read_commit,
            in_base_history=c.in_base_history,
            merge_tree=c.merge_tree,
            record_change_hash=rc.change_hash,
            live_change_hash=rc.change_hash,
            record_policy_sha256=view.policy_sha256,
            live_policy_sha256=view.policy_sha256,
        ).reason

    rebind: Rebind | None = None
    if c.rebind is not None:
        signer = c.rebind

        def rebind(record: VerdictRecord) -> VerdictRecord | None:
            fresh = signer(record)
            if fresh is not None:
                records[fresh.id] = fresh
            return fresh

    outcome = evaluate_verdicts(
        inputs.verdicts,
        view,
        inputs.trust,
        c.verify,
        inputs.resets,
        ai_required=quorum.ai >= 1,
        carry_forward=br,
        rebind=rebind,
        ai_quorum=quorum.ai,
    )
    ctx.ignored.extend(outcome.ignored)
    ctx.records = records
    if outcome.state != "success":
        raise _Stop("6", outcome.state, outcome.reason, outcome.detail or None)
    return [records[verdict_id] for verdict_id in outcome.counted if verdict_id in records]


def _is_clean_base_merge(entry: CommitEntry, c: GateCallables) -> bool:
    """GT-15 b / QA-2: a merge of the base into the PR that the gate can recompute: two parents
    (git's own, equal to the bundle's), one of them in base history (either order, as `vm_carry`
    accepts), and the commit's tree equal to the gate's own merge of the two parents. A
    hand-edited or unreadable merge is not exempt."""
    if len(entry.parents) != 2:
        return False
    first, other = entry.parents
    if not (c.in_base_history(other) or c.in_base_history(first)):
        return False
    try:
        read = c.read_commit(entry.sha)
        if read is None or tuple(read[0]) != tuple(entry.parents):
            return False
        merged = c.merge_tree(first, other)
    except Exception:
        return False
    return merged is not None and read[1] == merged


def _quorum(
    inputs: GateInputs,
    c: GateCallables,
    ctx: _Ctx,
    bundle: EvidenceBundle,
    rc: Recompute,
    quorum: Quorum,
    verdicts: Sequence[VerdictRecord],
) -> None:
    author = bundle.author
    # the AI part (QA-1, QA-2, EB-9)
    reviewers: set[str] = set()
    families: set[str] = set()
    for record in verdicts:
        family = record.payload.model_family
        if quorum.author_family_differs:
            if author.kind == "unknown" or (author.kind == "agent" and author.model_family is None):
                ctx.ignored.append((record.id, "author_family_unknown"))
                continue
            if author.kind == "agent" and family == author.model_family:
                ctx.ignored.append((record.id, "author_family_same"))
                continue
        ctx.counted_verdicts.append(record.id)
        reviewers.add(record.payload.reviewer_id)
        if family is not None:
            families.add(family)
        ctx.lapse(parse_time(record.payload.expires_at))
    unmet: list[str] = []
    if len(reviewers) < quorum.ai:
        unmet.append("ai")
    if quorum.ai >= 1 and len(families) < quorum.min_model_families:
        unmet.append("model_families")
    # the human part (QA-2, QA-6); skipped and said so until VM-3 (FR-6)
    counted_humans: set[str] = set()
    if inputs.human_quorum == "not_built":
        ctx.ignored.append(("human_quorum", "not_built"))
        # item 12 (PROVISIONAL, owner to confirm): QA-3 is all-of. A human or codeowner part that
        # cannot be judged is unmet, in the pure evaluator and in both modes; lifting the service's
        # refusal in VM-3 must not turn a skip on. Steps 1 to 6 already passed, so a report still
        # shows the AI result.
        if quorum.human > 0 or quorum.codeowner:
            unmet.append("human")
    else:
        exempt_free: set[str] = set()
        # EB-9: with no known author and a human quorum to meet, anyone who authored or pushed
        # any commit of the PR (merges of the base included) may be the author: none of them
        # counts, whatever `last_pusher_may_approve` says (fail closed)
        maybe_author: set[str] = set()
        if author.id is None and quorum.human > 0:
            for entry in bundle.commits:
                maybe_author.update((entry.author_id, entry.pusher_id))
        for entry in bundle.commits:
            br_exempt = _is_clean_base_merge(entry, c)
            if not br_exempt:
                exempt_free.update((entry.author_id, entry.pusher_id))
        for view in inputs.approvals:
            if view.ignore is not None:
                ctx.ignored.append((view.approval_id, view.ignore))
            elif author.id is not None and view.approver_id == author.id:
                ctx.ignored.append((view.approval_id, "self_approval"))
            elif view.approver_id in maybe_author:
                ctx.ignored.append((view.approval_id, "self_approval"))
            elif view.approver_id in exempt_free and not inputs.policy.last_pusher_may_approve:
                ctx.ignored.append((view.approval_id, "last_pusher"))
            elif view.approver_id in counted_humans:
                ctx.ignored.append((view.approval_id, "duplicate_approver"))
            else:
                counted_humans.add(view.approver_id)
                ctx.counted_approvals.append(view.approval_id)
                ctx.lapse(view.expires_at)
        if len(counted_humans) < quorum.human:
            unmet.append("human")
    if unmet:
        raise _Stop("7", "pending", "quorum_unmet", ",".join(unmet))
    if quorum.codeowner and inputs.human_quorum != "not_built":
        uncovered: list[str] = []
        exception_used = False
        author_signed = author.id is not None and any(
            v.ignore is None and v.approver_id == author.id for v in inputs.approvals
        )
        for path in (entry.path for entry in rc.paths):
            owners = inputs.owners_of(path)
            if owners & counted_humans:
                continue
            if (
                inputs.policy.sole_codeowner_exception
                and author_signed
                and owners == frozenset({author.id})
            ):
                exception_used = True
                continue
            uncovered.append(path)
        if exception_used:
            ctx.ignored.append(("exception", "sole_codeowner"))
        if uncovered:
            raise _Stop("7", "pending", "codeowner_uncovered", sorted(uncovered)[0])


# --- head sharing (GT-1) --------------------------------------------------------------------------


def shares_head(inputs: GateInputs) -> bool:
    """GT-1: an open PR whose head branch is in the base's repository and whose base is the
    protected branch shares its head sha with the others like it."""
    pr = inputs.pr
    return (
        pr.state == "open"
        and pr.head_repo_id == inputs.repo_id
        and pr.base_ref == inputs.protected_branch
    )


def evaluate_head(inputs: Sequence[GateInputs], callables: GateCallables) -> HeadOutcome:
    """Step 1 (the flag) and GT-1 over every PR given for one head: the sharing PRs are each
    evaluated; the head is a success only when all of them pass, and a passing PR next to a failing
    sharer shows `failure head_shared_unverified` with the sharer's PR and code. The head's state
    and reason are the lowest-numbered evaluated PR's. PRs that do not share the head (a fork, a
    PR to another base) are not evaluated when sharers exist; alone, such a PR is evaluated on its
    own and gets its eligibility code."""
    if not inputs:
        raise ValueError("evaluate_head needs at least one PR")
    head = inputs[0].pr.head_sha
    if any(i.pr.head_sha != head for i in inputs):
        raise ValueError("evaluate_head takes the PRs of one head")
    mode = inputs[0].mode
    if mode == "off":
        return HeadOutcome(False, head, None, NOT_ENFORCED, None, None, None, (), {})
    sharers = sorted((i for i in inputs if shares_head(i)), key=lambda i: i.pr.number)
    subjects = sharers or sorted(inputs, key=lambda i: i.pr.number)
    numbers = tuple(i.pr.number for i in subjects)
    per_pr: dict[int, Outcome] = {}
    for item in subjects:
        try:
            outcome = evaluate_pr(item, callables)
        except Exception:  # pragma: no cover - evaluate_pr never raises; belt and braces
            outcome = Outcome(item.pr.number, head, "failure", GATE_ERROR, "any")
        per_pr[item.pr.number] = _with_sharers(outcome, numbers)
    failing = [o for o in per_pr.values() if o.state != "success"]
    if failing:
        first = min(failing, key=lambda o: o.pr)
        for number, outcome in list(per_pr.items()):
            if outcome.state == "success":
                per_pr[number] = _replace(
                    outcome,
                    state="failure",
                    reason="head_shared_unverified",
                    step="2",
                    detail=f"{first.pr}:{first.reason}",
                    would_be=None,
                    valid_until=None,
                )
    lead = per_pr[numbers[0]]
    lapses = [o.valid_until for o in per_pr.values() if o.valid_until is not None]
    return HeadOutcome(
        evaluated=True,
        head_sha=head,
        state=lead.state,
        reason=lead.reason,
        detail=lead.detail,
        status_name=status_name(mode),
        external_id=external_id(inputs[0].repo, head, numbers),
        shared_head_prs=numbers,
        per_pr=per_pr,
        valid_until=min(lapses) if lapses and lead.state == "success" else None,
    )


def _with_sharers(outcome: Outcome, numbers: tuple[int, ...]) -> Outcome:
    return _replace(outcome, shared_head_prs=numbers)


def _replace(outcome: Outcome, **changes: Any) -> Outcome:
    values = dict(outcome.__dict__)
    values.update(changes)
    return Outcome(**values)


# --- the decision record (6.4, GT-13) and its ledger payload (LA-1) ------------------------------


def decision_body_hash(decision: GateDecision) -> str:
    """The hash the `decision_hash` field must carry: SHA-256 of the canonical object without
    that field (as `bundle_hash`, EB-1)."""
    body = decision.model_dump(mode="json")
    body.pop("decision_hash")
    return hashlib.sha256(canonical_json(body)).hexdigest()


def build_decision(
    outcome: Outcome,
    inputs: GateInputs,
    *,
    decided_at: datetime,
    gate_version: str,
    group_head_sha: str | None = None,
) -> GateDecision:
    """`vm-decision-1` for one PR: every input used, the counted and ignored records, the mode and
    its own hash. The mode must post a status (`shadow` or `enforce`)."""
    if inputs.mode == "off":
        raise ValueError("no decision is built in mode off")
    if not GATE_VERSION_PATTERN.fullmatch(gate_version):
        raise ValueError("gate_version is not a short version token")
    draft = {
        "version": "vm-decision-1",
        "org_id": inputs.org_id,
        "repo": inputs.repo,
        "repo_id": inputs.repo_id,
        "pr": outcome.pr,
        "head_sha": outcome.head_sha,
        "group_head_sha": group_head_sha,
        "shared_head_prs": list(outcome.shared_head_prs or (outcome.pr,)),
        "mode": inputs.mode,
        "state": outcome.state,
        "reason": outcome.reason,
        "counted": {
            "verdicts": list(outcome.counted_verdicts),
            "approvals": list(outcome.counted_approvals),
        },
        "ignored": [{"id": id_, "reason": reason} for id_, reason in outcome.ignored],
        "inputs": {"bundle_hash": outcome.bundle_hash, "status_ids": list(outcome.status_ids)},
        "override": False,
        "decided_at": format_time(decided_at),
        "gate_version": gate_version,
        "decision_hash": "0" * 64,
    }
    unsealed = GateDecision.model_validate(draft)
    return GateDecision.model_validate({**draft, "decision_hash": decision_body_hash(unsealed)})


PATH_DETAIL_REASONS = frozenset({"codeowner_uncovered"})
"""The reasons whose `detail` is a changed path: the author's text, so the ledger gets its
SHA-256 only (LA-1: no free text from a person). Every other detail is a code the gate wrote."""


def ledger_detail(reason: str, detail: str | None) -> tuple[str | None, str | None]:
    """`(detail, detail_sha256)` as a `merge.*` payload may carry them (LA-1)."""
    if detail is None:
        return None, None
    if reason in PATH_DETAIL_REASONS:
        return None, hashlib.sha256(detail.encode("utf-8", "surrogateescape")).hexdigest()
    return detail, None


def gate_decided_payload(
    decision: GateDecision, outcome: Outcome, check_run_id: str | None
) -> dict[str, Any]:
    """The `merge.gate_decided` payload (LA-1): hashes, ids, states, codes and times; the tier and
    the hash of the tier list that decided (TL-21; null when no bundle passed step 3); the would-be
    result (GT-19) and the forge's check-run id (F-3: the enabler compares it). Never text.

    The code is written as `reason_code` (and `would_be_code`), never under the key `reason`:
    `core.redaction` hides every `reason` value that `system` did not write, so a decision's code
    would be null on every ledger read route. `ignored` is a list of `[id, code]` pairs for the
    same reason. A detail that is a path is carried as its hash (`ledger_detail`)."""
    detail, detail_sha256 = ledger_detail(outcome.reason, outcome.detail)
    return {
        "actor": GATE_ACTOR,
        "decision_hash": decision.decision_hash,
        "org_id": decision.org_id,
        "repo": decision.repo,
        "repo_id": decision.repo_id,
        "pr": decision.pr,
        "head_sha": decision.head_sha,
        "group_head_sha": decision.group_head_sha,
        "shared_head_prs": list(decision.shared_head_prs),
        "mode": decision.mode,
        "state": decision.state,
        "reason_code": decision.reason,
        "detail": detail,
        "detail_sha256": detail_sha256,
        "step": outcome.step,
        "bundle_hash": decision.inputs.bundle_hash,
        "tier": outcome.tier,
        "tier_list_sha256": outcome.tier_list_sha256,
        "counted_verdicts": list(decision.counted.verdicts),
        "counted_approvals": list(decision.counted.approvals),
        "ignored": [[entry.id, entry.reason] for entry in decision.ignored],
        "status_ids": list(decision.inputs.status_ids),
        "would_be_state": outcome.would_be[0] if outcome.would_be else None,
        "would_be_code": outcome.would_be[1] if outcome.would_be else None,
        "valid_until": format_time(outcome.valid_until) if outcome.valid_until else None,
        "check_run_id": check_run_id,
        "decided_at": decision.decided_at,
        "gate_version": decision.gate_version,
    }
