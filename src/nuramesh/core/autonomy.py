"""Earned autonomy: grant states, demotion and void derivation (E13, `spec/autonomy/README.md`).

Pure functions over ledger entries; nothing here reads a store, a clock or the network. A grant is
a human decision recorded in the ledger (`decision.recorded`, kind `autonomy_grant`); whether it is
**active** at a time `as_of` is *derived* from the ledger alone (spec 6.2), so a missing write can
never keep a grant alive. Tightening is automatic and fail-closed: the first failing check, in the
spec's order, names the state.

Inputs are `Entry` values: a ledger event and its **commit position** (the store's total order of
commits, spec section 2). Whether a trigger comes after a grant is decided only by that position;
the clock (`at`) decides only what exists at `as_of`.

This module has the maths for sections 2, 6.2 to 6.5 and 8. The scorecard, the proposal and the
overlay build on `Verdict` and `GrantState` (later phases, `docs/lanes/e13-e14-build-state.md`).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from nuramesh.core.canonical import content_hash
from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.org import OrgGraph
from nuramesh.core.policy import POLICY_CONTRACT, PolicyRule, PolicySet

# -- constants (spec 4.3, 5.1, 6.3, 8) ---------------------------------------------------------

MIN_APPROVED = 30
MIN_VERDICTS = 20
MIN_PASS_RATE = Decimal("0.95")
WINDOW_DAYS = 30
COOLDOWN_DAYS = 14
DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 90
STREAK_FAILS = 3
STREAK_LENGTH = 10
SCAN_BOUND = 100_000

GRANTABLE_KINDS = frozenset({"delegate", "call_model", "use_tool", "run_command", "write_file"})
NEVER_GRANTABLE_KINDS = frozenset({"spend", "external_message", "merge", "network"})

VOID_REASONS = (
    "promotion",
    "rule_changed",
    "contract_changed",
    "role_widened",
    "agent_moved",
    "model_changed",
)
"""The only void vocabulary (spec 6.2 step 5), in the order they are checked."""

# `decision.recorded` kinds this feature adds (spec section 7)
KIND_GRANT = "autonomy_grant"
KIND_REVOKE = "autonomy_revoke"
KIND_CEILING = "autonomy_ceiling_set"
KIND_SETTING = "autonomy_setting_set"
KIND_DEMOTION = "autonomy_demotion"
KIND_VOID = "autonomy_void"
KIND_CONFIG = "config_loaded"
KIND_CONFIG_SWAP = "config_content_replaced"  # ND220: the scrub's new content for a config version
KIND_PROMOTION = "promotion"
AUTONOMY_KINDS = frozenset(
    {
        KIND_GRANT,
        KIND_REVOKE,
        KIND_CEILING,
        KIND_SETTING,
        KIND_DEMOTION,
        KIND_VOID,
        KIND_CONFIG,
        KIND_CONFIG_SWAP,
    }
)

SETTING_NAMES = (
    "MIN_APPROVED",
    "MIN_VERDICTS",
    "MIN_PASS_RATE",
    "COOLDOWN_DAYS",
    "MAX_EXPIRY_DAYS",
    "STREAK_FAILS",
    "STREAK_LENGTH",
)
_HIGHER_IS_STRICTER = frozenset(
    {"MIN_APPROVED", "MIN_VERDICTS", "MIN_PASS_RATE", "COOLDOWN_DAYS", "STREAK_LENGTH"}
)


class Entry(NamedTuple):
    """A ledger entry and its commit position (a larger position was committed later)."""

    pos: int
    event: LedgerEvent


@dataclass(frozen=True)
class Settings:
    """The thresholds in force. Organizations may only make them stricter (spec 8)."""

    min_approved: int = MIN_APPROVED
    min_verdicts: int = MIN_VERDICTS
    min_pass_rate: Decimal = MIN_PASS_RATE
    cooldown_days: int = COOLDOWN_DAYS
    max_expiry_days: int = MAX_EXPIRY_DAYS
    streak_fails: int = STREAK_FAILS
    streak_length: int = STREAK_LENGTH


DEFAULT_SETTINGS = Settings()


def setting_problem(name: str, value: Decimal | int, current: Settings) -> tuple[str, str] | None:
    """Why `value` cannot be the new value of setting `name` right now: `(code, message)`, or None.

    `WINDOW_DAYS` is not settable (`not_settable`); a looser value than the default is
    `looser_than_default`; the relations between the streak settings are `invalid`."""
    if name == "WINDOW_DAYS":
        return "not_settable", "the evidence window is fixed in v1"
    if name not in SETTING_NAMES:
        return "invalid", f"unknown setting '{name}'"
    defaults = DEFAULT_SETTINGS
    table: dict[str, Decimal | int] = {
        "MIN_APPROVED": defaults.min_approved,
        "MIN_VERDICTS": defaults.min_verdicts,
        "MIN_PASS_RATE": defaults.min_pass_rate,
        "COOLDOWN_DAYS": defaults.cooldown_days,
        "MAX_EXPIRY_DAYS": defaults.max_expiry_days,
        "STREAK_FAILS": defaults.streak_fails,
        "STREAK_LENGTH": defaults.streak_length,
    }
    default = table[name]
    if name == "MIN_PASS_RATE":
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()  # NaN and the infinities cannot be compared or rounded
            or value < 0  # range first: quantize raises on huge exponents
            or value > 1
            or value != value.quantize(Decimal("0.0001"))
        ):
            return "invalid", "MIN_PASS_RATE is at most 1 with at most 4 decimal places"
    elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return "invalid", f"{name} is a whole number of at least 1"
    number, floor = Decimal(value), Decimal(default)
    stricter = number >= floor if name in _HIGHER_IS_STRICTER else number <= floor
    if not stricter:
        return "looser_than_default", f"{name} cannot be looser than {default}"
    fails = value if name == "STREAK_FAILS" else current.streak_fails
    length = value if name == "STREAK_LENGTH" else current.streak_length
    if length < fails:
        return "invalid", "STREAK_LENGTH is at least STREAK_FAILS"
    return None


# -- rule identity and the configuration record (spec 2) --------------------------------------


def rule_hash(rule: PolicyRule) -> str:
    """`content_hash` of the rule (kind, decision, target, `over`); any edit changes it."""
    return content_hash(
        {
            "action": rule.action.value,
            "decision": rule.decision.value,
            "target": rule.target,
            "over": None if rule.over is None else rule.over.to_json(),
        }
    )


def config_payload(org: OrgGraph, policies: PolicySet) -> dict[str, Any]:
    """The `config_loaded` payload for a configuration (ids and hashes only, A7).

    `roles` maps every role to its rule hashes in rule-key order (null when it has no policy);
    `agents` maps each agent to its role and model; `rule_kinds` (an additive field, decision
    T135) gives each rule's kind, decision and whether it is a never-matching allow shape, so a
    read at a past `as_of` can classify cells without the policy text."""
    roles: dict[str, Any] = {}
    kinds: dict[str, Any] = {}
    for role in org.roles:
        policy = policies.policy_for(role.id)
        roles[role.id] = None if policy is None else [rule_hash(r) for r in policy.rules]
        kinds[role.id] = (
            None
            if policy is None
            else [
                [r.action.value, r.decision.value, _never_matching_shape(r)] for r in policy.rules
            ]
        )
    body: dict[str, Any] = {
        "policy_contract": POLICY_CONTRACT,
        "roles": roles,
        "agents": {a.id: {"role": a.role, "model": org.model_for(a.id)} for a in org.agents},
        "rule_kinds": kinds,
    }
    return {"kind": KIND_CONFIG, "config_hash": content_hash(body), **body}


def config_document(org: OrgGraph, policies: PolicySet) -> dict[str, Any]:
    """The FULL configuration a `config_loaded` entry stands for (ND118): the organization graph
    and every role's policy as JSON, with the hash of the entry. It goes to the content store
    (kind `config`, content-addressed); the ledger keeps only ids and hashes (A7)."""
    return {
        "config_hash": config_payload(org, policies)["config_hash"],
        "org": org.model_dump(mode="json"),
        "policies": policies.model_dump(mode="json"),
    }


def _never_matching_shape(rule: PolicyRule) -> bool:
    """A `run_command` shape pattern (one with a shell control character) that also has `*` or
    `?`: it can never match under allow semantics (spec 4.3, `never_grantable`)."""
    from nuramesh.core.policy import ActionKind, has_control_character

    return (
        rule.action is ActionKind.RUN_COMMAND
        and has_control_character(rule.target)
        and ("*" in rule.target or "?" in rule.target)
    )


# -- parsed entries ---------------------------------------------------------------------------


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def entry_id(event: LedgerEvent) -> str:
    """The ledger entry id: its stream and sequence."""
    return f"{event.stream}#{event.seq}"


def decision_kind(event: LedgerEvent) -> str | None:
    """The `kind` of a `decision.recorded` entry, else None."""
    if event.type != "decision.recorded":
        return None
    kind = event.payload.get("kind")
    return kind if isinstance(kind, str) else None


@dataclass(frozen=True)
class Grant:
    """A parsed `autonomy_grant` entry."""

    id: str
    pos: int
    entry: str
    granted_at: datetime
    expires_at: datetime
    subject_kind: str
    subject_id: str
    role: str
    rule_key: int
    rule_hash: str
    action_kind: str
    policy_contract: int

    @property
    def agent_level(self) -> bool:
        return self.subject_kind == "agent"


def parse_grant(item: Entry) -> Grant | None:
    """The grant an entry records, or None if it is not a well-formed grant (never active)."""
    event, p = item.event, item.event.payload
    if decision_kind(event) != KIND_GRANT:
        return None
    days = p.get("expires_in_days")
    if isinstance(days, int) and not isinstance(days, bool):
        # the entry's own `at` is the grant time; the expiry is derived from it (ND230)
        try:
            expires = expires_at(event.at, days) if days >= 1 else None
        except OverflowError:
            expires = None
    else:
        expires = _when(p.get("expires_at"))  # an entry that stored the expiry itself
    key, contract = p.get("rule_key"), p.get("policy_contract")
    texts = [p.get(k) for k in ("grant_id", "subject_kind", "subject_id", "role", "rule_hash")]
    if (
        expires is None
        or not all(isinstance(t, str) for t in texts)
        or not isinstance(key, int)
        or isinstance(key, bool)
        or not isinstance(contract, int)
        or p.get("subject_kind") not in ("agent", "role")
    ):
        return None
    grant_id, subject_kind, subject_id, role, hash_ = (str(t) for t in texts)
    return Grant(
        id=grant_id,
        pos=item.pos,
        entry=entry_id(event),
        granted_at=event.at,
        expires_at=expires,
        subject_kind=subject_kind,
        subject_id=subject_id,
        role=role,
        rule_key=key,
        rule_hash=hash_,
        action_kind=str(p.get("action_kind", "")),
        policy_contract=contract,
    )


def swapped_config_content(events: Sequence[LedgerEvent]) -> dict[str, str]:
    """Old content id -> new content id for every `config_content_replaced` entry (ND220), in
    ledger order. The erasure scrub writes the cleaned configuration as new content and records
    the swap; a reader follows it with `current_config_content`."""
    swaps: dict[str, str] = {}
    for event in events:
        if decision_kind(event) != KIND_CONFIG_SWAP:
            continue
        old, new = event.payload.get("old_content"), event.payload.get("new_content")
        if isinstance(old, str) and isinstance(new, str):
            swaps[old] = new
    return swaps


def current_config_content(swaps: Mapping[str, str], content_id: str) -> str:
    """The content that now stands for this configuration version: follow the swaps from
    `content_id` to the end of the chain (bounded, so a malformed cycle cannot loop)."""
    seen = {content_id}
    while (new := swaps.get(content_id)) is not None and new not in seen:
        seen.add(new)
        content_id = new
    return content_id


@dataclass(frozen=True)
class Config:
    """A parsed `config_loaded` record."""

    pos: int
    at: datetime
    entry: str
    contract: int | None
    roles: Mapping[str, Sequence[str] | None]
    agents: Mapping[str, tuple[str, str | None]]
    rule_kinds: Mapping[str, Sequence[tuple[str, str, bool]] | None] = field(default_factory=dict)

    def rule_kind_at(self, role: str, key: int) -> tuple[str, str, bool] | None:
        """`(kind, decision, never_matching_shape)` of the rule at `key`, or None."""
        kinds = self.rule_kinds.get(role)
        if kinds is None or not 0 <= key < len(kinds):
            return None
        return kinds[key]

    def rule_hash_at(self, role: str, key: int) -> str | None:
        """The hash of the rule at `key` in `role`'s policy, or None (no policy, no such key)."""
        hashes = self.roles.get(role)
        if hashes is None or not 0 <= key < len(hashes):
            return None
        return hashes[key]

    def members(self, role: str) -> frozenset[str]:
        return frozenset(a for a, (r, _) in self.agents.items() if r == role)


def parse_config(item: Entry) -> Config | None:
    p = item.event.payload
    if decision_kind(item.event) != KIND_CONFIG:
        return None
    roles, agents = p.get("roles"), p.get("agents")
    if not isinstance(roles, Mapping) or not isinstance(agents, Mapping):
        return None
    parsed_agents: dict[str, tuple[str, str | None]] = {}
    for name, info in agents.items():
        if not isinstance(info, Mapping) or not isinstance(info.get("role"), str):
            return None
        model = info.get("model")
        parsed_agents[str(name)] = (str(info["role"]), model if isinstance(model, str) else None)
    contract = p.get("policy_contract")
    return Config(
        pos=item.pos,
        at=item.event.at,
        entry=entry_id(item.event),
        contract=contract if isinstance(contract, int) else None,
        roles={
            str(k): (list(v) if isinstance(v, (list, tuple)) else None) for k, v in roles.items()
        },
        agents=parsed_agents,
        rule_kinds=_parse_kinds(p.get("rule_kinds")),
    )


def _parse_kinds(raw: Any) -> dict[str, list[tuple[str, str, bool]] | None]:
    out: dict[str, list[tuple[str, str, bool]] | None] = {}
    if not isinstance(raw, Mapping):
        return out
    for role, rules in raw.items():
        if not isinstance(rules, (list, tuple)):
            out[str(role)] = None
            continue
        out[str(role)] = [
            (str(r[0]), str(r[1]), r[2] is True)
            for r in rules
            if isinstance(r, (list, tuple)) and len(r) == 3
        ]
    return out


# -- verdicts (spec 2) ------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """A `gate.completed` entry that is a verdict, credited to the task's assignee at that time."""

    pos: int
    at: datetime
    run_id: str
    seq: int
    entry: str
    task: str | None
    agent: str | None
    gate_kind: str
    passed: bool


GateKindOf = Callable[[LedgerEvent], str | None]


def payload_gate_kind(event: LedgerEvent) -> str | None:
    """The gate kind from the entry's own `kind` field (fixtures); the engine passes a reader
    that looks the kind up in the task's definition of done."""
    kind = event.payload.get("kind")
    return kind if isinstance(kind, str) else None


def verdicts_of(
    entries: Sequence[Entry], gate_kind_of: GateKindOf = payload_gate_kind
) -> list[Verdict]:
    """Every verdict in `entries` (given in commit order), ordered by `(at, run_id, seq)`.

    `error` outcomes, `human` gates and any other outcome are not verdicts. The assignee is that
    of the task's latest `task.transitioned` before the entry (none yet: credited to nobody)."""
    assignee: dict[tuple[str, str], str | None] = {}
    found: list[Verdict] = []
    for item in sorted(entries, key=lambda e: e.pos):
        event = item.event
        if event.run_id is None or event.task_id is None:
            continue
        key = (event.run_id, event.task_id)
        if event.type == "task.transitioned":
            who = event.payload.get("assignee")
            assignee[key] = who if isinstance(who, str) else None
        elif event.type == "gate.completed":
            outcome = event.payload.get("outcome")
            kind = gate_kind_of(event)
            if outcome not in ("passed", "failed") or kind is None or kind == "human":
                continue
            found.append(
                Verdict(
                    item.pos,
                    event.at,
                    event.run_id,
                    event.seq,
                    entry_id(event),
                    event.task_id,
                    assignee.get(key),
                    kind,
                    outcome == "passed",
                )
            )
    return sorted(found, key=lambda v: (v.at, v.run_id, v.seq))


def streak_demotes(failed: Sequence[bool], fails: int, length: int) -> bool:
    """Spec 6.3 `failure_streak`: some window of `length` consecutive verdicts (fewer if fewer
    exist so far) holds `fails` or more failures. `failed` is in verdict order."""
    for i in range(len(failed)):
        if sum(failed[max(0, i - length + 1) : i + 1]) >= fails:
            return True
    return False


# -- grant state (spec 6.2, 6.3) --------------------------------------------------------------


@dataclass(frozen=True)
class GrantState:
    """The state of one grant at `as_of` and why: `active`, `not_yet`, `expired`, `revoked`,
    `demoted`, `void` or `ceiling`."""

    grant: Grant
    state: str
    void_reason: str | None = None
    promotion_entry: str | None = None
    trigger: str | None = None
    trigger_entry: str | None = None

    @property
    def active(self) -> bool:
        return self.state == "active"


def _visible(entries: Sequence[Entry], as_of: datetime) -> list[Entry]:
    return sorted((e for e in entries if e.event.at <= as_of), key=lambda e: e.pos)


def ceiling_of(entries: Sequence[Entry], role: str, action_kind: str, as_of: datetime) -> str:
    """The effective ceiling of `(role, kind)`: the latest `autonomy_ceiling_set` by ledger order
    at `as_of`, default `none` (spec 5.3)."""
    value = "none"
    for item in _visible(entries, as_of):
        p = item.event.payload
        if (
            decision_kind(item.event) == KIND_CEILING
            and p.get("role") == role
            and p.get("action_kind") == action_kind
        ):
            value = "allow" if p.get("new") == "allow" else "none"
    return value


def _void_shown(config: Config, base: Config | None, grant: Grant) -> frozenset[str]:
    """Every void reason (5b to 5f) a voiding configuration record shows for the grant."""
    found: set[str] = set()
    if config.rule_hash_at(grant.role, grant.rule_key) != grant.rule_hash:
        found.add("rule_changed")
    if config.contract != grant.policy_contract:
        found.add("contract_changed")
    if base is None:
        return frozenset(found)
    if not grant.agent_level:
        if config.members(grant.role) - base.members(grant.role):
            found.add("role_widened")
        if any(
            config.agents[a][1] != base.agents[a][1]
            for a in config.members(grant.role) & base.members(grant.role)
        ):
            found.add("model_changed")
        return frozenset(found)
    now_in = config.agents.get(grant.subject_id)
    if now_in is None or now_in[0] != grant.role:
        found.add("agent_moved")
    else:
        before = base.agents.get(grant.subject_id)
        if before is not None and before[1] != now_in[1]:
            found.add("model_changed")
    return frozenset(found)


def void_reason(
    grant: Grant, entries: Sequence[Entry], as_of: datetime
) -> tuple[str, str | None] | None:
    """`(reason, promotion entry id)` if the grant is void at `as_of` (6.2 step 5), else None.

    5a is a promotion naming the grant; 5b to 5f hold when an `autonomy_void` entry gives that
    reason or a voiding configuration record shows it. Every condition is "some entry with
    `at <= as_of` exists", so a void is permanent."""
    visible = _visible(entries, as_of)
    for item in visible:
        p = item.event.payload
        if decision_kind(item.event) == KIND_PROMOTION and grant.id in (
            p.get("voided_grants") or ()
        ):
            return "promotion", entry_id(item.event)
    configs = [c for c in (parse_config(i) for i in sorted(entries, key=lambda e: e.pos)) if c]
    before = [c for c in configs if c.pos < grant.pos]
    base = before[-1] if before else None
    voiding = [c for c in configs if c.at <= as_of and (c is base or c.pos > grant.pos)]
    recorded = {
        item.event.payload.get("reason")
        for item in visible
        if decision_kind(item.event) == KIND_VOID and item.event.payload.get("grant_id") == grant.id
    }
    shown = set().union(*(_void_shown(c, base, grant) for c in voiding)) if voiding else set()
    for reason in VOID_REASONS[1:]:
        if reason in recorded or reason in shown:
            return reason, None
    return None


def _agents_of(configs: Sequence[Config]) -> Mapping[str, str]:
    return {a: r for a, (r, _) in configs[-1].agents.items()} if configs else {}


def demotion_trigger(
    grant: Grant,
    entries: Sequence[Entry],
    as_of: datetime,
    *,
    required_suites: Mapping[str, frozenset[str]],
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings = DEFAULT_SETTINGS,
) -> tuple[str, str] | None:
    """`(trigger, entry id)` of the first demotion trigger of section 6.3 for the grant, or None.

    Only entries committed after the grant entry (by position) and existing at `as_of` count."""
    visible = _visible(entries, as_of)
    configs = [c for c in (parse_config(i) for i in visible) if c]
    role_of = _agents_of(configs)
    members = {a for a, r in role_of.items() if r == grant.role}

    def hits(agent: str | None) -> bool:
        """Whether a trigger attributable to `agent` reaches this grant (a role-level grant:
        every agent of the role)."""
        if agent is None:
            return True
        if grant.agent_level:
            return agent == grant.subject_id
        return agent in members

    after = [i for i in visible if i.pos > grant.pos]
    for item in after:
        event, p = item.event, item.event.payload
        if event.type == "eval.scored" and p.get("passed") is False:
            if p.get("suite_id") not in required_suites.get(grant.role, frozenset()):
                continue
            subject = p.get("subject_id")
            if p.get("subject_kind") == "role":
                if subject == grant.role:
                    return "eval_failed", entry_id(event)
            elif isinstance(subject, str) and hits(subject):
                return "eval_failed", entry_id(event)
        elif event.type == "budget.breached":
            agent = p.get("agent")
            if agent is None or (isinstance(agent, str) and hits(agent)):
                return "budget_breached", entry_id(event)
        elif event.type == "approval.decided" and p.get("approved") is False:
            agent = p.get("agent")
            if (
                isinstance(agent, str)
                and hits(agent)
                and _joined(p.get("approval"), grant, visible)
            ):
                return "approval_denied", entry_id(event)
    mine = [
        v
        for v in verdicts_of(visible, gate_kind_of)
        if v.pos > grant.pos and v.agent is not None and hits(v.agent)
    ]
    if streak_demotes([not v.passed for v in mine], settings.streak_fails, settings.streak_length):
        return "failure_streak", mine[-1].entry
    return None


def _joined(approval: Any, grant: Grant, visible: Sequence[Entry]) -> bool:
    """Whether the approval's `policy.decided` entries name the grant's rule key and hash (4.2)."""
    return approval is not None and any(
        i.event.type == "policy.decided"
        and i.event.payload.get("approval") == approval
        and i.event.payload.get("rule") == grant.rule_key
        and i.event.payload.get("rule_hash") == grant.rule_hash
        for i in visible
    )


def grant_states(
    entries: Sequence[Entry],
    as_of: datetime,
    *,
    required_suites: Mapping[str, frozenset[str]],
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings = DEFAULT_SETTINGS,
) -> dict[str, GrantState]:
    """The state of every grant at `as_of`, by grant id, in the order of 6.2: the first check that
    fails names the state. A grant id recorded twice keeps its first entry."""
    ordered = sorted(entries, key=lambda e: e.pos)
    grants: dict[str, Grant] = {}
    for item in ordered:
        grant = parse_grant(item)
        if grant is not None:
            grants.setdefault(grant.id, grant)
    out: dict[str, GrantState] = {}
    visible = _visible(ordered, as_of)
    for grant in grants.values():
        if grant.granted_at > as_of:
            out[grant.id] = GrantState(grant, "not_yet")
        elif as_of >= grant.expires_at:
            out[grant.id] = GrantState(grant, "expired")
        elif any(
            decision_kind(i.event) == KIND_REVOKE and i.event.payload.get("grant_id") == grant.id
            for i in visible
        ):
            out[grant.id] = GrantState(grant, "revoked")
        else:
            out[grant.id] = _remaining(
                grant, ordered, visible, as_of, required_suites, gate_kind_of, settings
            )
    return out


def _remaining(
    grant: Grant,
    ordered: Sequence[Entry],
    visible: Sequence[Entry],
    as_of: datetime,
    required_suites: Mapping[str, frozenset[str]],
    gate_kind_of: GateKindOf,
    settings: Settings,
) -> GrantState:
    trigger = demotion_trigger(
        grant,
        ordered,
        as_of,
        required_suites=required_suites,
        gate_kind_of=gate_kind_of,
        settings=settings,
    )
    if trigger is not None:
        return GrantState(grant, "demoted", trigger=trigger[0], trigger_entry=trigger[1])
    recorded = next(
        (
            i
            for i in visible
            if decision_kind(i.event) == KIND_DEMOTION
            and i.event.payload.get("grant_id") == grant.id
        ),
        None,
    )
    if recorded is not None:
        return GrantState(
            grant,
            "demoted",
            trigger=str(recorded.event.payload.get("trigger")),
            trigger_entry=str(recorded.event.payload.get("trigger_entry")),
        )
    voided = void_reason(grant, ordered, as_of)
    if voided is not None:
        return GrantState(grant, "void", void_reason=voided[0], promotion_entry=voided[1])
    if ceiling_of(ordered, grant.role, grant.action_kind, as_of) != "allow":
        return GrantState(grant, "ceiling")
    return GrantState(grant, "active")


def voids_for_new_config(
    entries: Sequence[Entry],
    new_config: Mapping[str, Any],
    now: datetime,
    *,
    required_suites: Mapping[str, frozenset[str]],
    gate_kind_of: GateKindOf = payload_gate_kind,
    settings: Settings = DEFAULT_SETTINGS,
    event_factory: Callable[[int, datetime, Mapping[str, Any]], LedgerEvent],
) -> list[dict[str, Any]]:
    """The `autonomy_void` payloads the load path writes with a new `config_loaded` record
    (spec 6.5 step 1): one for every grant that is not expired, revoked, demoted or already void
    now and for which 5b to 5f hold against the new record, with the first reason that holds.

    `event_factory(pos, at, payload)` builds the hypothetical `config_loaded` entry (a pure
    module cannot build a valid ledger event on its own)."""
    last = max((e.pos for e in entries), default=0)
    probe = Entry(last + 1, event_factory(last + 1, now, new_config))
    before = grant_states(
        entries, now, required_suites=required_suites, gate_kind_of=gate_kind_of, settings=settings
    )
    after = grant_states(
        [*entries, probe],
        now,
        required_suites=required_suites,
        gate_kind_of=gate_kind_of,
        settings=settings,
    )
    seen = parse_config(probe)
    voids: list[dict[str, Any]] = []
    for grant_id, state in before.items():
        if state.state not in ("active", "ceiling", "not_yet"):
            continue
        new = after[grant_id]
        if new.state != "void" or new.void_reason is None or new.void_reason == "promotion":
            continue
        grant = state.grant
        assert seen is not None
        voids.append(
            {
                "kind": KIND_VOID,
                "grant_id": grant_id,
                "reason": new.void_reason,
                "rule_hash_seen": seen.rule_hash_at(grant.role, grant.rule_key),
                "policy_contract_seen": seen.contract,
            }
        )
    return voids


def expires_at(granted_at: datetime, days: int) -> datetime:
    """A grant's expiry: `granted_at + days`; at exactly that instant it is expired."""
    return granted_at + timedelta(days=days)


def void_payload(
    grant: Grant, entries: Sequence[Entry], reason: str, as_of: datetime
) -> dict[str, Any] | None:
    """The `autonomy_void` payload for a grant void by 5b to 5f (spec 6.5): `rule_hash_seen` and
    `policy_contract_seen` come from the **voiding configuration**, the first configuration record
    (ledger order, existing at `as_of`) that shows the reason. None if no record shows it."""
    ordered = sorted(entries, key=lambda e: e.pos)
    configs = [c for c in (parse_config(i) for i in ordered) if c and c.at <= as_of]
    before = [c for c in configs if c.pos < grant.pos]
    base = before[-1] if before else None
    for config in configs:
        if (config is base or config.pos > grant.pos) and reason in _void_shown(
            config, base, grant
        ):
            return {
                "kind": KIND_VOID,
                "grant_id": grant.id,
                "reason": reason,
                "rule_hash_seen": config.rule_hash_at(grant.role, grant.rule_key),
                "policy_contract_seen": config.contract,
            }
    return None


def missing_voids(
    entries: Sequence[Entry], states: Mapping[str, GrantState], as_of: datetime
) -> list[dict[str, Any]]:
    """The backstop (spec 6.1, 6.5 step 2): an `autonomy_void` payload for every grant that is void
    by 5b to 5f and has no `autonomy_void` entry yet. Never one for a promotion void."""
    written = {
        i.event.payload.get("grant_id") for i in entries if decision_kind(i.event) == KIND_VOID
    }
    found: list[dict[str, Any]] = []
    for grant_id, state in states.items():
        if state.state != "void" or state.void_reason in (None, "promotion"):
            continue
        if grant_id in written:
            continue
        payload = void_payload(state.grant, entries, state.void_reason, as_of)
        if payload is not None:
            found.append(payload)
    return found
