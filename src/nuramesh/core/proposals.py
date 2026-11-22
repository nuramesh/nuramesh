"""Autonomy proposals (E13, `spec/autonomy/README.md` section 4): a read of the ledger.

A proposal says whether one cell `(role, rule_key)` has earned a grant for one subject (an agent or
a role). It is a pure function of the ledger entries, the configuration recorded at `as_of`, the
ceilings and the settings. It appends nothing and applies nothing; only a human decision (section
5) creates a grant. `evidence_hash` binds the evidence a person looked at to the grant they sign.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from nuramesh.core.autonomy import (
    DEFAULT_SETTINGS,
    GRANTABLE_KINDS,
    KIND_SETTING,
    NEVER_GRANTABLE_KINDS,
    WINDOW_DAYS,
    Config,
    Entry,
    GateKindOf,
    Grant,
    GrantState,
    Settings,
    ceiling_of,
    decision_kind,
    entry_id,
    grant_states,
    payload_gate_kind,
    verdicts_of,
)
from nuramesh.core.canonical import content_hash
from nuramesh.core.policy import POLICY_CONTRACT
from nuramesh.core.scorecard import (
    SCORECARD_VERSION,
    SOLO_MODE,
    ScorecardError,
    SubjectScope,
    config_at,
    scope_of,
    solo_share,
)

REASON_ORDER = (
    "never_grantable",
    "no_such_rule",
    "ceiling_none",
    "already_granted",
    "approved_below_min",
    "verdicts_below_min",
    "denied_in_window",
    "pass_rate_below_min",
    "eval_blocked",
    "eval_failed_in_cooldown",
    "breach_in_cooldown",
    "demotion_in_cooldown",
)

EvalBlocked = Callable[[SubjectScope], bool]
"""Whether the subject is not eligible by `check_eligibility` at `as_of` (an agent: agent-level
and its role; a role: any agent of the role, fail closed)."""


@dataclass(frozen=True)
class Proposal:
    subject_kind: str
    subject_id: str
    role: str
    rule_key: int
    action_kind: str | None
    status: str
    reasons: tuple[str, ...]
    evidence: Mapping[str, Any]
    evidence_hash: str
    approved_asks_solo: int = 0
    """How many of the approved asks of `evidence` were solo decisions (ND396, SA-18 item 2).
    Shown beside the evidence; never part of `evidence` or `evidence_hash`, and never a reason."""

    @property
    def solo(self) -> dict[str, Any]:
        """`{approved_asks_solo, solo_share}` over the same approved asks as the evidence."""
        approved = int(self.evidence.get("approved_asks", 0))
        return {
            "approved_asks_solo": self.approved_asks_solo,
            "solo_share": solo_share(self.approved_asks_solo, approved),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "cell": {"role": self.role, "rule_key": self.rule_key},
            "kind": self.action_kind,
            "status": self.status,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "evidence_hash": self.evidence_hash,
            "solo": self.solo,
        }


def settings_at(entries: Sequence[Entry], as_of: datetime) -> Settings:
    """The thresholds in force at `as_of`: the latest `autonomy_setting_set` per name (ledger order)
    over the defaults (spec 8)."""
    values: dict[str, Any] = {}
    for item in sorted(entries, key=lambda e: e.pos):
        p = item.event.payload
        if item.event.at <= as_of and decision_kind(item.event) == KIND_SETTING:
            values[str(p.get("name"))] = p.get("new")
    fields: dict[str, Any] = {}
    names = {
        "MIN_APPROVED": ("min_approved", int),
        "MIN_VERDICTS": ("min_verdicts", int),
        "MIN_PASS_RATE": ("min_pass_rate", Decimal),
        "COOLDOWN_DAYS": ("cooldown_days", int),
        "MAX_EXPIRY_DAYS": ("max_expiry_days", int),
        "STREAK_FAILS": ("streak_fails", int),
        "STREAK_LENGTH": ("streak_length", int),
    }
    for name, (attr, kind) in names.items():
        if name in values:
            try:
                parsed = kind(str(values[name]))
                if isinstance(parsed, Decimal) and not parsed.is_finite():
                    continue  # NaN or an infinity stored: unreadable, keep the default
                fields[attr] = parsed
            except (ValueError, ArithmeticError):
                continue  # an unreadable setting never loosens anything: keep the default
    return Settings(**{**DEFAULT_SETTINGS.__dict__, **fields})


def _in(at: datetime, start: datetime, end: datetime) -> bool:
    return start <= at <= end


def evaluate_cell(
    entries: Sequence[Entry],
    *,
    kind: str,
    subject: str,
    rule_key: int,
    as_of: datetime,
    required_suites: Mapping[str, frozenset[str]],
    eval_blocked: EvalBlocked,
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings | None = None,
) -> Proposal:
    """The proposal for one cell and one subject. `ScorecardError` 404 if the subject is unknown."""
    config = config_at(entries, as_of)
    scope = scope_of(config, kind, subject)
    assert config is not None
    cfg = settings if settings is not None else settings_at(entries, as_of)
    visible = sorted((e for e in entries if e.event.at <= as_of), key=lambda e: e.pos)
    role = scope.role
    rule = config.rule_kind_at(role, rule_key)
    current_hash = config.rule_hash_at(role, rule_key)
    base: dict[str, Any] = {
        "subject": {"kind": kind, "id": subject},
        "cell": {"role": role, "rule_key": rule_key},
        "as_of": _iso(as_of),
        "scorecard_version": SCORECARD_VERSION,
        "policy_contract": POLICY_CONTRACT,
        "rule_hash": current_hash,
    }
    if rule is None or rule[1] != "ask":
        return _done(scope, rule_key, None, ["no_such_rule"], base, {})
    action_kind = rule[0]
    if action_kind in NEVER_GRANTABLE_KINDS or rule[2]:
        return _done(scope, rule_key, action_kind, ["never_grantable"], base, {})

    window_start = as_of - timedelta(days=WINDOW_DAYS)
    cooldown_start = as_of - timedelta(days=cfg.cooldown_days)

    # 4.2 item 1: asks, joined to their answers
    approval_ids: set[str] = set()
    task_of: dict[str, tuple[str, str] | None] = {}
    for item in visible:
        event, p = item.event, item.event.payload
        if (
            event.type == "policy.decided"
            and p.get("rule") == rule_key
            and p.get("rule_hash") == current_hash
            and p.get("policy_contract") == POLICY_CONTRACT
            and p.get("decision") in ("ask", "allow")
            and isinstance(p.get("approval"), str)
            and "autonomy_grant" not in p
            and scope.has(p.get("agent"))
        ):
            approval = str(p["approval"])
            approval_ids.add(approval)
            if event.run_id is not None and event.task_id is not None:
                task_of[approval] = (event.run_id, event.task_id)
            else:
                task_of.setdefault(approval, None)
    approved: set[str] = set()
    denied: set[str] = set()
    solo: set[str] = set()  # the approvals with a solo decision (shown, never a reason)
    for item in visible:
        event, p = item.event, item.event.payload
        if event.type == "approval.decided" and _in(event.at, window_start, as_of):
            approval = str(p.get("approval"))
            if approval in approval_ids:
                (approved if p.get("approved") is True else denied).add(approval)
                if p.get("approval_mode") == SOLO_MODE:
                    solo.add(approval)
    tasks = sorted({t for a in approved if (t := task_of.get(a)) is not None})
    linked = set(tasks)
    verdicts = [
        v
        for v in verdicts_of(visible, gate_kind_of)
        if _in(v.at, window_start, as_of) and v.task is not None and (v.run_id, v.task) in linked
    ]
    passed = sum(1 for v in verdicts if v.passed)

    # 4.2 items 5 to 7
    suites = required_suites.get(role, frozenset())
    failed_eval = False
    breach = False
    for item in visible:
        event, p = item.event, item.event.payload
        if event.type == "eval.scored" and p.get("passed") is False:
            if event.at >= cooldown_start and p.get("suite_id") in suites:
                who = p.get("subject_id")
                if (p.get("subject_kind") == "role" and who == role) or scope.has(who):
                    failed_eval = True
        elif event.type == "budget.breached" and event.at >= cooldown_start:
            agent = p.get("agent")
            if agent is None or scope.has(agent):
                breach = True
    states = grant_states(
        entries, as_of, required_suites=required_suites, gate_kind_of=gate_kind_of, settings=cfg
    )
    by_entry = {entry_id(e.event): e.event for e in visible}
    demoted_recently = False
    already = False
    for state in states.values():
        grant = state.grant
        if grant.role != role or grant.rule_key != rule_key:
            continue
        if state.state == "demoted":
            trigger = by_entry.get(state.trigger_entry or "")
            when = trigger.at if trigger is not None else as_of
            if when >= cooldown_start:
                demoted_recently = True
        if (
            state.active
            and _same_subject(grant, kind, subject, role)
            and (grant.rule_hash == current_hash)
        ):
            already = True

    reasons: list[str] = []
    if ceiling_of(entries, role, action_kind, as_of) != "allow":
        reasons.append("ceiling_none")
    if already:
        reasons.append("already_granted")
    if len(approved) < cfg.min_approved:
        reasons.append("approved_below_min")
    if len(verdicts) < cfg.min_verdicts:
        reasons.append("verdicts_below_min")
    if denied:
        reasons.append("denied_in_window")
    if len(verdicts) >= cfg.min_verdicts and Decimal(passed) < cfg.min_pass_rate * len(verdicts):
        reasons.append("pass_rate_below_min")
    if eval_blocked(scope):
        reasons.append("eval_blocked")
    if failed_eval:
        reasons.append("eval_failed_in_cooldown")
    if breach:
        reasons.append("breach_in_cooldown")
    if demoted_recently:
        reasons.append("demotion_in_cooldown")
    counts = {
        "approved_asks": len(approved),
        "denied_asks": len(denied),
        "verdicts": len(verdicts),
        "passed": passed,
        "window": {"from": _iso(window_start), "to": _iso(as_of)},
        "approval_ids": sorted(approved | denied),
        "linked_tasks": [[run, task] for run, task in tasks],
        "verdict_entries": sorted(v.entry for v in verdicts),
    }
    return _done(scope, rule_key, action_kind, reasons, base, counts, len(approved & solo))


def solo_lines(approved: int, solo: int, share: str | None) -> list[str]:
    """What a person is shown of a proposal's solo share (spec/rbac/solo-approval.md SA-18 item
    2): `approved asks: N (solo M, P %)` and, when a solo one exists, that nobody else reviewed
    them. `P` is the share as a percentage without trailing zeros; with no approved ask there
    is no share and no percentage."""
    if share is None:
        return [f"approved asks: {approved} (solo {solo})"]
    percent = f"{(Decimal(share) * 100).normalize():f}"
    lines = [f"approved asks: {approved} (solo {solo}, {percent} %)"]
    if solo > 0:
        lines.append("no second person reviewed the solo ones")
    return lines


def _same_subject(grant: Grant, kind: str, subject: str, role: str) -> bool:
    """Whether `grant` covers the subject: the same subject, or (for an agent) a role-level grant
    of its role covering the cell."""
    if grant.subject_kind == kind and grant.subject_id == subject:
        return True
    return kind == "agent" and not grant.agent_level and grant.subject_id == role


def _done(
    scope: SubjectScope,
    rule_key: int,
    action_kind: str | None,
    reasons: list[str],
    base: dict[str, Any],
    counts: dict[str, Any],
    approved_solo: int = 0,
) -> Proposal:
    ordered = tuple(r for r in REASON_ORDER if r in reasons)
    if "never_grantable" in ordered:
        ordered = ("never_grantable",)
    elif "no_such_rule" in ordered:
        ordered = ("no_such_rule",)
    status: str
    first: str | None = next(
        (
            r
            for r in ("never_grantable", "no_such_rule", "ceiling_none", "already_granted")
            if r in ordered
        ),
        None,
    )
    if first is not None:
        status = "ceiling" if first == "ceiling_none" else first
    elif "approved_below_min" in ordered or "verdicts_below_min" in ordered:
        status = "not_enough_evidence"
    elif ordered:
        status = "blocked"
    else:
        status = "proposable"
    evidence = {**base, **counts}
    return Proposal(
        scope.kind,
        scope.id,
        scope.role,
        rule_key,
        action_kind,
        status,
        ordered,
        evidence,
        content_hash(evidence),
        approved_solo,
    )


def proposals_for(
    entries: Sequence[Entry],
    *,
    kind: str,
    subject: str,
    as_of: datetime,
    required_suites: Mapping[str, frozenset[str]],
    eval_blocked: EvalBlocked,
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings | None = None,
) -> list[Proposal]:
    """One proposal per `ask` rule of grantable kind of the subject's role (spec 4.4)."""
    config: Config | None = config_at(entries, as_of)
    scope = scope_of(config, kind, subject)
    assert config is not None
    kinds = config.rule_kinds.get(scope.role) or []
    found: list[Proposal] = []
    for key, (action_kind, decision, _shape) in enumerate(kinds):
        if decision == "ask" and action_kind in GRANTABLE_KINDS:
            found.append(
                evaluate_cell(
                    entries,
                    kind=kind,
                    subject=subject,
                    rule_key=key,
                    as_of=as_of,
                    required_suites=required_suites,
                    eval_blocked=eval_blocked,
                    gate_kind_of=gate_kind_of,
                    settings=settings,
                )
            )
    return found


def _iso(moment: datetime) -> str:
    from nuramesh.core.ledger import format_time

    return format_time(moment)


__all__ = [
    "REASON_ORDER",
    "GrantState",
    "Proposal",
    "ScorecardError",
    "evaluate_cell",
    "proposals_for",
    "settings_at",
    "solo_lines",
]
