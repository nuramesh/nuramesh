"""The org kill switch, role pause and circuit breakers: the pure rules (E19, ND106).

Everything here reads ledger entries and returns facts; nothing writes. The engine
(`engine/killswitch.py`) appends the entries these rules call for.

- **Halt state** of an organization comes from its `org:<id>` stream: it is halted when the latest
  honored one of `org.halted` / `org.resumed` is `org.halted`. **Role state** is the same fold per
  role (`org.role_paused` / `org.role_resumed`). Anything that loosens protection (`org.resumed`,
  `org.role_resumed`, `breaker.reset`, `breaker.disabled`, `breaker.enabled`) is honored only when
  its actor is a person (`human:` id); an entry from anyone else changes nothing at all.
- A **breaker rule** is data: a signal counted in a window and a limit. It trips when the observed
  value is **more than** the limit. A tripped breaker stays tripped until a person records
  `breaker.reset`; a reset (and an honored enable) also starts the count afresh. A rule with
  `dry_run` records its trip but stops nothing. A **disabled** rule is not evaluated.
- Counting window: entries with `start < at <= now`, where `start` is the later of `now - window`
  and the last honored reset or enable. An unknown cost is never guessed: it adds nothing and is
  counted separately. Signal readers are total: a malformed entry does not count, it never raises.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nuramesh.core.canonical import decimal_text
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.money import Money
from nuramesh.core.org import Slug
from nuramesh.core.people import is_human_id

MAX_WINDOW_SECONDS = 86_400
MAX_RULES = 100


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Signal(StrEnum):
    """What a breaker counts."""

    POLICY_DENIED = "policy_denied"  # `policy.decided` with decision `deny`
    GATE_FAILED = "gate_failed"  # `gate.completed` with outcome `failed` or `error`
    MODEL_BLOCKED = "model_blocked"  # `model.blocked`, whatever the reason
    MODEL_CALLED = "model_called"  # `model.called` (any cost)
    SPEND = "spend"  # sum of the `cost` of `model.called`


class BreakerAction(StrEnum):
    """What a tripped (non dry-run) rule does."""

    HALT_ORG = "halt_org"
    PAUSE_ROLE = "pause_role"


class DisableReason(StrEnum):
    """Why a person switched a breaker off: a fixed code, never free text."""

    FALSE_POSITIVE = "false_positive"
    PLANNED_WORK = "planned_work"
    RULE_BEING_REPLACED = "rule_being_replaced"
    OTHER = "other"


_NOT_DECIMAL = (
    'max_spend must be a finite decimal, written as a quoted string such as "25" or an integer'
)


class BreakerRule(_Frozen):
    """One breaker. Exactly one of `max_events` (count signals) or `max_spend` (spend)."""

    id: Slug
    signal: Signal
    window_seconds: int = Field(ge=1, le=MAX_WINDOW_SECONDS)
    max_events: int | None = Field(default=None, ge=0)
    max_spend: Money | None = None
    action: BreakerAction = BreakerAction.HALT_ORG
    role: Slug | None = None
    dry_run: bool = False

    @field_validator("max_spend", mode="before")
    @classmethod
    def _amount(cls, value: object) -> object:
        # rules are written as data: "5" or 5 mean that many dollars (never a float or a boolean)
        if isinstance(value, bool | float):
            raise ValueError(
                "max_spend must be a quoted decimal string (or an integer), not a float"
            )
        if isinstance(value, str | int):
            try:
                amount = Decimal(value)
            except (InvalidOperation, ValueError):
                raise ValueError(_NOT_DECIMAL) from None
            if not amount.is_finite():
                raise ValueError(_NOT_DECIMAL)
            return Money.of(amount)
        return value

    @model_validator(mode="after")
    def _limit_matches_signal(self) -> "BreakerRule":
        spend = self.signal is Signal.SPEND
        if spend and (self.max_spend is None or self.max_events is not None):
            raise ValueError("a spend breaker needs max_spend (and no max_events)")
        if not spend and (self.max_events is None or self.max_spend is not None):
            raise ValueError("a count breaker needs max_events (and no max_spend)")
        if self.max_spend is not None and self.max_spend.amount < 0:
            raise ValueError("max_spend must not be negative")
        if self.action is BreakerAction.PAUSE_ROLE and self.role is None:
            raise ValueError("a pause_role breaker needs role")
        return self

    @property
    def limit_text(self) -> str:
        """The configured limit as written (`"1.5"` stays `"1.5"`)."""
        if self.signal is Signal.SPEND:
            assert self.max_spend is not None
            return decimal_text(self.max_spend.amount)
        assert self.max_events is not None
        return str(self.max_events)


def check_rules(rules: Iterable[BreakerRule]) -> tuple[BreakerRule, ...]:
    """The rules as a tuple; duplicate ids are refused."""
    out = tuple(rules)
    seen: set[str] = set()
    for r in out:
        if r.id in seen:
            raise ValueError(f"duplicate breaker rule id '{r.id}'")
        seen.add(r.id)
    return out


class HaltState(_Frozen):
    """Whether the organization is halted, and the entry that says so."""

    halted: bool
    seq: int | None = None
    by: str | None = None
    at: datetime | None = None
    cause: str | None = None  # "human", "breaker" or "evaluation_unavailable"
    rule: str | None = None
    phase: str | None = None
    error_class: str | None = None
    failing_rule: str | None = None
    failed_ticks: int | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def counts_for_halt(e: LedgerEvent) -> bool:
    """Whether `e` can change the halt state: any `org.halted`, but an `org.resumed` only when
    its actor is a person (a breaker, agent or service entry never lifts a halt)."""
    return e.type == EventType.ORG_HALTED.value or (
        e.type == EventType.ORG_RESUMED.value and is_human_id(e.actor)
    )


def halt_state(org_events: Iterable[LedgerEvent]) -> HaltState:
    """Fold an `org:` stream (in order) into its halt state."""
    state = HaltState(halted=False)
    for e in org_events:
        if not counts_for_halt(e):
            continue
        if e.type == EventType.ORG_HALTED.value:
            p = e.payload
            state = HaltState(
                halted=True,
                seq=e.seq,
                by=e.actor,
                at=e.at,
                cause=_text(p.get("cause")) or "human",
                rule=_text(p.get("rule")),
                phase=_text(p.get("phase")),
                error_class=_text(p.get("error_class")),
                failing_rule=_text(p.get("failing_rule")),
                failed_ticks=_whole(p.get("failed_ticks")),
            )
        else:
            state = HaltState(halted=False, seq=e.seq, by=e.actor, at=e.at)
    return state


class RoleState(_Frozen):
    """Whether one role is paused, and the entry that says so."""

    role: str
    paused: bool
    seq: int | None = None
    by: str | None = None
    at: datetime | None = None
    cause: str | None = None  # "human" or "breaker"
    rule: str | None = None


def counts_for_role(e: LedgerEvent) -> bool:
    """Whether `e` can change a role's state: any `org.role_paused`, but an `org.role_resumed`
    only from a person."""
    return e.type == EventType.ORG_ROLE_PAUSED.value or (
        e.type == EventType.ORG_ROLE_RESUMED.value and is_human_id(e.actor)
    )


def role_states(org_events: Iterable[LedgerEvent]) -> dict[str, RoleState]:
    """Fold an `org:` stream into the state of every role it mentions (paused or resumed)."""
    states: dict[str, RoleState] = {}
    for e in org_events:
        if not counts_for_role(e):
            continue
        role = _text(e.payload.get("role"))
        if role is None:
            continue
        if e.type == EventType.ORG_ROLE_PAUSED.value:
            states[role] = RoleState(
                role=role,
                paused=True,
                seq=e.seq,
                by=e.actor,
                at=e.at,
                cause=_text(e.payload.get("cause")) or "human",
                rule=_text(e.payload.get("rule")),
            )
        else:
            states[role] = RoleState(role=role, paused=False, seq=e.seq, by=e.actor, at=e.at)
    return states


def paused_roles(org_events: Iterable[LedgerEvent]) -> tuple[str, ...]:
    """The names of the paused roles, sorted."""
    return tuple(sorted(r for r, s in role_states(org_events).items() if s.paused))


class BreakerState(_Frozen):
    """A rule's latch and switch, folded from the org stream by rule name.

    `floor` is the later of the last honored reset and the last honored enable."""

    tripped: bool
    floor: datetime | None = None
    last_reset_at: datetime | None = None
    tripped_event: LedgerEvent | None = None
    disabled: bool = False
    disabled_event: LedgerEvent | None = None


def breaker_state(org_events: Iterable[LedgerEvent], rule_id: str) -> BreakerState:
    """Fold the org stream into one rule's state (spec sections 2, 6 and 6.1)."""
    tripped: LedgerEvent | None = None
    disabled: LedgerEvent | None = None
    floor: datetime | None = None
    reset_at: datetime | None = None
    for e in org_events:
        if e.payload.get("rule") != rule_id:
            continue
        human = is_human_id(e.actor)
        if e.type == EventType.BREAKER_TRIPPED.value:
            tripped = e
        elif e.type == EventType.BREAKER_RESET.value and human:
            tripped, reset_at = None, e.at
            floor = e.at if floor is None else max(floor, e.at)
        elif e.type == EventType.BREAKER_DISABLED.value and human:
            disabled = e
        elif e.type == EventType.BREAKER_ENABLED.value and human and disabled is not None:
            disabled = None
            floor = e.at if floor is None else max(floor, e.at)
    return BreakerState(
        tripped=tripped is not None,
        floor=floor,
        last_reset_at=reset_at,
        tripped_event=tripped,
        disabled=disabled is not None,
        disabled_event=disabled,
    )


def breaker_names(org_events: Iterable[LedgerEvent]) -> list[str]:
    """Every rule name the org stream mentions through a breaker entry, in order of first use."""
    seen: dict[str, None] = {}
    for e in org_events:
        if e.type.startswith("breaker."):
            name = _text(e.payload.get("rule"))
            if name is not None:
                seen.setdefault(name)
    return list(seen)


def halting_rules(
    rules: Sequence[BreakerRule], org_events: Sequence[LedgerEvent] | None
) -> tuple[BreakerRule, ...]:
    """The rules that can stop work: not a dry run and not known to be disabled. With
    `org_events=None` (the org stream could not be read) the disabled state is unknown and every
    rule that is not a dry run counts (fail closed, spec 9.5)."""
    return tuple(
        r
        for r in rules
        if not r.dry_run and (org_events is None or not breaker_state(org_events, r.id).disabled)
    )


class Trip(_Frozen):
    """A breaker that tripped: the numbers behind it."""

    rule: str
    signal: Signal
    action: BreakerAction = BreakerAction.HALT_ORG
    role: str | None = None
    observed: str  # event count, or the spend as exact decimal text
    limit: str
    window_seconds: int = 0
    window_start: datetime
    dry_run: bool
    unknown_cost_calls: int = 0


def _signal_counts(signal: Signal, e: LedgerEvent) -> bool:
    payload = e.payload
    match signal:
        case Signal.POLICY_DENIED:
            return e.type == EventType.POLICY_DECIDED.value and payload.get("decision") == "deny"
        case Signal.GATE_FAILED:
            return e.type == EventType.GATE_COMPLETED.value and payload.get("outcome") in (
                "failed",
                "error",
            )
        case Signal.MODEL_BLOCKED:
            return e.type == EventType.MODEL_BLOCKED.value
        case Signal.MODEL_CALLED | Signal.SPEND:
            return e.type == EventType.MODEL_CALLED.value
    return False  # pragma: no cover


def _known_cost(e: LedgerEvent) -> Money | None:
    """The entry's cost if it is a usable amount (a finite, non-negative USD `Money`)."""
    cost: Any = e.cost
    try:
        if isinstance(cost, Money) and cost.amount.is_finite() and cost.amount >= 0:
            return cost
    except Exception:  # noqa: S110 (a malformed amount is an unknown cost, never an error)
        pass
    return None


class Observation(_Frozen):
    """What a rule counted in its window."""

    count: int
    spent: Money
    unknown_cost_calls: int
    start: datetime

    def observed_text(self, rule: BreakerRule) -> str:
        """The `observed` text: a count, or the spend as exact decimal text."""
        return decimal_text(self.spent.amount) if rule.signal is Signal.SPEND else str(self.count)

    def over(self, rule: BreakerRule) -> bool:
        """Whether the observed value is more than the limit."""
        if rule.signal is Signal.SPEND:
            assert rule.max_spend is not None
            return self.spent > rule.max_spend
        assert rule.max_events is not None
        return self.count > rule.max_events


def window_start(rule: BreakerRule, floor: datetime | None, now: datetime) -> datetime:
    """Where the rule's window starts: the later of `now - window` and the floor."""
    start = now - timedelta(seconds=rule.window_seconds)
    return floor if floor is not None and floor > start else start


def observe(
    rule: BreakerRule,
    events: Iterable[LedgerEvent],
    now: datetime,
    floor: datetime | None = None,
) -> Observation:
    """Count what `rule` sees in `events` (the organization's run entries). Pure and total."""
    start = window_start(rule, floor, now)
    count, spent, unknown = 0, Money.zero(), 0
    for e in events:
        if not (start < e.at <= now) or not _signal_counts(rule.signal, e):
            continue
        if rule.role is not None and e.payload.get("role") != rule.role:
            continue
        count += 1
        if rule.signal is Signal.SPEND:
            cost = _known_cost(e)
            if cost is None:
                unknown += 1
            else:
                try:
                    spent = spent + cost
                except Exception:  # an amount that cannot be added exactly is unknown
                    unknown += 1
    return Observation(count=count, spent=spent, unknown_cost_calls=unknown, start=start)


def evaluate(
    rules: Sequence[BreakerRule],
    org_events: Sequence[LedgerEvent],
    events: Sequence[LedgerEvent],
    now: datetime,
    *,
    only: Iterable[str] | None = None,
) -> tuple[Trip, ...]:
    """The rules that trip now, in config order. `org_events` is the org stream (latches, resets,
    disables), `events` the organization's run entries. Rules that are tripped or disabled are
    skipped. `only` limits the evaluation to those rule names. Pure."""
    wanted = None if only is None else set(only)
    trips: list[Trip] = []
    for rule in rules:
        if wanted is not None and rule.id not in wanted:
            continue
        state = breaker_state(org_events, rule.id)
        if state.tripped or state.disabled:
            continue
        seen = observe(rule, events, now, state.floor)
        if seen.over(rule):
            trips.append(
                Trip(
                    rule=rule.id,
                    signal=rule.signal,
                    action=rule.action,
                    role=rule.role,
                    observed=seen.observed_text(rule),
                    limit=rule.limit_text,
                    window_seconds=rule.window_seconds,
                    window_start=seen.start,
                    dry_run=rule.dry_run,
                    unknown_cost_calls=seen.unknown_cost_calls,
                )
            )
    return tuple(trips)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _from_trip(rule: BreakerRule | None, e: LedgerEvent, name: str) -> dict[str, Any]:
    """The status fields of a tripped rule: the numbers recorded at the trip."""
    p: Mapping[str, Any] = e.payload
    signal = _text(p.get("signal")) or (rule.signal.value if rule else None)
    return {
        "name": name,
        "signal": signal,
        "action": _text(p.get("action")) or (rule.action.value if rule else None),
        "role": _text(p.get("role")) if "role" in p else (rule.role if rule else None),
        "window_seconds": _whole(p.get("window_seconds"))
        or (rule.window_seconds if rule else None),
        "dry_run": (
            p["dry_run"] if isinstance(p.get("dry_run"), bool) else bool(rule and rule.dry_run)
        ),
        "limit": _text(p.get("limit")) or (rule.limit_text if rule else None),
        "observed": _text(p.get("observed")),
        "observed_as": "at_trip",
        "unknown_cost_calls": _whole(p.get("unknown_cost_calls")) or 0,
        "window_start": _text(p.get("window_start")),
        "tripped_at": _iso(e.at),
        "tripped_seq": e.seq,
        "tripped_by": e.actor,
        "manual": p.get("manual") is True,
    }


def rule_status(
    rule: BreakerRule | None,
    name: str,
    org_events: Sequence[LedgerEvent],
    events: Sequence[LedgerEvent],
    now: datetime,
) -> dict[str, Any]:
    """The section 11 object for one rule (`rule=None`: a rule that is no longer in the config).
    Tripped wins over disabled (spec 6.1). Pure; writes nothing."""
    state = breaker_state(org_events, name)
    base: dict[str, Any]
    shown = "ok"
    if state.tripped and state.tripped_event is not None:
        shown = "tripped"
        base = _from_trip(rule, state.tripped_event, name)
    else:
        shown = "disabled" if state.disabled else "ok"
        if rule is None:
            base = {
                "name": name,
                "signal": None,
                "action": None,
                "role": None,
                "window_seconds": None,
                "dry_run": False,
                "limit": None,
                "observed": None,
                "observed_as": None,
                "unknown_cost_calls": 0,
                "window_start": None,
                "tripped_at": None,
                "tripped_seq": None,
                "tripped_by": None,
                "manual": False,
            }
        else:
            seen = observe(rule, events, now, state.floor)
            base = {
                "name": name,
                "signal": rule.signal.value,
                "action": rule.action.value,
                "role": rule.role,
                "window_seconds": rule.window_seconds,
                "dry_run": rule.dry_run,
                "limit": rule.limit_text,
                "observed": seen.observed_text(rule),
                "observed_as": "live",
                "unknown_cost_calls": seen.unknown_cost_calls,
                "window_start": _iso(seen.start),
                "tripped_at": None,
                "tripped_seq": None,
                "tripped_by": None,
                "manual": False,
            }
    d = state.disabled_event if shown == "disabled" else None
    base.update(
        {
            "state": shown,
            "last_reset_at": _iso(state.last_reset_at),
            "disabled_at": _iso(d.at) if d else None,
            "disabled_by": d.actor if d else None,
            "disabled_reason": _text(d.payload.get("reason")) if d else None,
            "in_config": rule is not None,
        }
    )
    return base


def breaker_statuses(
    rules: Sequence[BreakerRule],
    org_events: Sequence[LedgerEvent],
    events: Sequence[LedgerEvent],
    now: datetime,
) -> list[dict[str, Any]]:
    """Section 11's `breakers` list: config order, then orphans (removed rules that are still
    tripped or disabled) sorted by name."""
    out = [rule_status(r, r.id, org_events, events, now) for r in rules]
    configured = {r.id for r in rules}
    for name in sorted(n for n in breaker_names(org_events) if n not in configured):
        state = breaker_state(org_events, name)
        if state.tripped or state.disabled:
            out.append(rule_status(None, name, org_events, events, now))
    return out
