"""The org kill switch, role pause and circuit breakers: the actions (E19, ND106).

The rules are pure and live in `core.breakers`; this module reads the ledger, appends what they
call for, and applies the existing cooperative Stop (`run_control.request_stop`, T57) to the
organization's open runs.

- `halt_org` / `pause_role` / `trip_breaker`: restricting, only a `human:` actor.
- `resume_org` / `resume_role` / `reset_breaker` / `disable_breaker` / `enable_breaker`: loosening,
  only a `human:` actor (`HumanRequired` otherwise, nothing written).
- `evaluate_breakers`: counts ledger signals; a trip is recorded (`breaker.tripped`) and, unless
  the rule is a dry run, halts the org or pauses the rule's role in the same transaction. The read
  only finds candidates: the trip is decided again under the write lock from fresh numbers.

Nothing is stored outside the ledger. A model call or tool already running finishes (Stop is
cooperative); what stops at once is new work (`call_model`, `StopWatch`).
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.breakers import (
    BreakerAction,
    BreakerRule,
    DisableReason,
    HaltState,
    RoleState,
    Trip,
    breaker_state,
    check_rules,
    evaluate,
    halt_state,
    halting_rules,
    observe,
    role_states,
)
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.org import OrgGraph
from nuramesh.core.people import is_human_id
from nuramesh.core.tasks import TaskStatus
from nuramesh.engine.halt_refusal import flush_for_resume
from nuramesh.engine.org_halt import PAGE, org_stream_id, read_org_events
from nuramesh.engine.run_control import RunFinished, request_stop
from nuramesh.stores.base import BadTimeError, EventDraft, NotFoundError, Store, UnitOfWork

_LOG = logging.getLogger(__name__)

BREAKER_ACTOR = "service:breaker"
HALT_VIA_STOP = "org_halt"
ROLE_VIA_STOP = "role_pause"
RUNNING_TASKS = (TaskStatus.IN_PROGRESS, TaskStatus.SUBMITTED, TaskStatus.VERIFYING)


class HumanRequired(PermissionError):
    """Only a person (`human:` id) may do this."""


class NoSuchRole(LookupError):
    """The role is not in the configuration (and, for a resume, not paused either)."""


class NoSuchBreaker(LookupError):
    """The breaker is not in the configuration (and has no state in the ledger)."""


class BreakerDisabled(Exception):  # noqa: N818 (a state, said in words)
    """A manual trip of a disabled rule is refused (spec 6.1)."""


class StateConflict(Exception):  # noqa: N818 (a state, said in words)
    """The action does not fit the current state; `code` is the API's 409 code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HaltOutcome:
    """What `halt_org` did."""

    state: HaltState
    already_halted: bool
    stopped_runs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeOutcome:
    """What `resume_org` did."""

    state: HaltState
    already_running: bool
    # the org stream as folded by `resume_org_in`: what the unit read plus `org.resumed`; the T154
    # refusal summaries the flush appends are in the ledger but not in this tuple
    events: tuple[LedgerEvent, ...] = ()


@dataclass(frozen=True)
class RolePauseOutcome:
    """What `pause_role` did: the role's state, and the tasks asked to stop."""

    state: RoleState
    already_paused: bool
    stopped_tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleResumeOutcome:
    """What `resume_role` did."""

    state: RoleState | None
    not_paused: bool
    events: tuple[LedgerEvent, ...] = ()  # the org stream as folded by `resume_role_in`


@dataclass(frozen=True)
class EntryOutcome:
    """The ledger entry an action wrote (sequence number in the org stream, and its time)."""

    seq: int
    at: datetime
    halted: bool = False  # whether the organization is halted (set by the `_in` variants)


@dataclass(frozen=True)
class TripOutcome:
    """What a manual trip did."""

    already_tripped: bool
    seq: int | None = None
    halted: bool = False
    paused_role: str | None = None


# -- reading -----------------------------------------------------------------------------------


def _events(uow: UnitOfWork, stream: str) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = uow.events_after(stream, cursor, PAGE)
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        cursor = batch[-1].seq


def _stream_events(store: Store, stream: str, after: int = 0) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    cursor = after
    while True:
        batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        cursor = batch[-1].seq


def _run_org(store: Store, stream: str) -> str | None:
    """The organization of a run stream, read from the raw first row: a run whose stored time is
    not real keeps its owner (ND238)."""
    return store.run_owner(stream)


def open_runs(store: Store, org_id: str) -> list[str]:
    """Ids of the organization's runs with no `run.ended`. A run that cannot be read (a stored
    time that is not real) cannot be shown to have ended: it is listed as open, so it is flagged
    and stopped, and only for its own organization (ND238)."""
    found: list[str] = []
    for stream in store.streams():
        if not stream.startswith("run:") or _run_org(store, stream) != org_id:
            continue
        try:
            ended = any(e.type == EventType.RUN_ENDED.value for e in _stream_events(store, stream))
        except BadTimeError:
            _LOG.warning("run stream %s holds a time that is not real; treated as open", stream)
            ended = False
        if not ended:
            found.append(stream.removeprefix("run:"))
    return found


def _human(actor: str, what: str) -> None:
    if not is_human_id(actor):
        raise HumanRequired(f"only a person can {what}")


# -- stop requests -----------------------------------------------------------------------------


def _stop_open_runs(store: Store, org_id: str, actor: str) -> tuple[str, ...]:
    stopped: list[str] = []
    for run_id in open_runs(store, org_id):
        try:
            outcome = request_stop(store, run_id, actor=actor, via=HALT_VIA_STOP)
        except (RunFinished, NotFoundError):
            continue  # finished meanwhile: nothing to stop
        except BadTimeError:
            # a damaged run takes no entry (not even a stop request) and cannot write; it is
            # stopped by that, flagged by `damaged_streams`, and reported here (ND238)
            _LOG.warning("run %s holds a time that is not real; it can write no more", run_id)
            stopped.append(run_id)
            continue
        if not outcome.already_requested:
            stopped.append(run_id)
    return tuple(stopped)


def _stop_role_tasks(store: Store, org: OrgGraph | None, role: str, actor: str) -> tuple[str, ...]:
    """Ask every started, unfinished task of an open run whose agent has `role` to stop
    (`task.stop_requested`, `via: role_pause`). The runs themselves are not stopped."""
    if org is None:
        return ()
    agents = {a.id for a in org.agents if a.role == role}
    stopped: list[str] = []
    for run_id in open_runs(store, org.id):
        tasks = [
            s.task
            for s in store.tasks_for_run(run_id)
            if s.task.assignee in agents and s.task.status in RUNNING_TASKS
        ]
        for task in tasks:
            with store.transaction() as uow:
                stream = f"run:{run_id}"
                events = _events(uow, stream)
                if any(e.type == EventType.RUN_ENDED.value for e in events):
                    break
                if any(
                    e.type == EventType.TASK_STOP_REQUESTED.value
                    and e.payload.get("task") == task.id
                    and e.payload.get("role") == role
                    for e in events
                ):
                    continue
                current = uow.get_task(task.id)
                if current is None or current.task.status not in RUNNING_TASKS:
                    continue  # finished meanwhile
                uow.append(
                    EventDraft(
                        stream=stream,
                        type=EventType.TASK_STOP_REQUESTED,
                        actor=actor,
                        run_id=run_id,
                        task_id=task.id,
                        payload={"task": task.id, "role": role, "via": ROLE_VIA_STOP},
                    )
                )
                stopped.append(f"{run_id}/{task.id}")
    return tuple(stopped)


# -- halting -----------------------------------------------------------------------------------


def _append_halt(
    uow: UnitOfWork, org_id: str, actor: str, payload: dict[str, Any]
) -> LedgerEvent | None:
    """Append `org.halted` unless the org is already halted (read inside this transaction)."""
    if halt_state(_events(uow, org_stream_id(org_id))).halted:
        return None
    return uow.append(
        EventDraft(
            stream=org_stream_id(org_id),
            type=EventType.ORG_HALTED,
            actor=actor,
            payload=payload,
        )
    )


def halt_org(
    store: Store,
    org_id: str,
    *,
    actor: str,
    via: str,
    cause: str = "human",
    rule: str | None = None,
) -> HaltOutcome:
    """Halt the organization (spec section 3). Only a person may press it (`HumanRequired`
    otherwise, nothing written); a breaker halts through `evaluate_breakers`, the tick through
    `halt_by_breaker`."""
    _human(actor, "halt the organization")
    payload: dict[str, Any] = {"cause": cause, "via": via}
    if rule is not None:
        payload["rule"] = rule
    with store.transaction() as uow:
        written = _append_halt(uow, org_id, actor, payload)
    state = halt_state(read_org_events(store, org_id))
    if written is None:
        return HaltOutcome(state, already_halted=True)
    return HaltOutcome(state, False, _stop_open_runs(store, org_id, actor))


def halt_by_breaker(
    store: Store,
    org_id: str,
    *,
    cause: str,
    rule: str | None = None,
    diagnosis: dict[str, Any] | None = None,
) -> HaltOutcome:
    """The service path of a halt: actor `service:breaker`, `via: breaker`. Used by the tick's
    fail-closed halt (`cause: evaluation_unavailable`, with the diagnosis: `phase`, `error_class`,
    `failing_rule`, `failed_ticks`). Reads the halt state inside its transaction; halting a halted
    org writes nothing."""
    payload: dict[str, Any] = {"cause": cause, "via": "breaker"}
    if rule is not None:
        payload["rule"] = rule
    payload.update({k: v for k, v in (diagnosis or {}).items() if v is not None})
    with store.transaction() as uow:
        written = _append_halt(uow, org_id, BREAKER_ACTOR, payload)
    state = halt_state(read_org_events(store, org_id))
    if written is None:
        return HaltOutcome(state, already_halted=True)
    return HaltOutcome(state, False, _stop_open_runs(store, org_id, BREAKER_ACTOR))


class OrgErasedError(Exception):
    """The organization was erased (or whether it was could not be read: unsure = erased), so its
    halt cannot be lifted (deny-checks spec 8.7). Nothing was appended."""


def resume_org(
    store: Store, org_id: str, *, actor: str, via: str, content: Any | None = None
) -> ResumeOutcome:
    """Lift the halt. Only a person may (spec section 4); anyone else raises `HumanRequired`.
    An erased organization is refused (`OrgErasedError`, nothing appended), and so is one whose
    erasure cannot be ruled out (fail closed)."""
    _human(actor, "resume a halted organization")
    with store.transaction() as uow:
        written = _resume_org_in(uow, org_id, actor=actor, via=via, content=content)
    state = halt_state(read_org_events(store, org_id))
    return ResumeOutcome(state, already_running=written.already_running)


def resume_org_in(
    uow: UnitOfWork,
    org_id: str,
    *,
    actor: str,
    via: str,
    content: Any | None = None,
) -> ResumeOutcome:
    """`resume_org` inside the caller's unit of work (R1.35): opens no transaction. The time of
    every record comes from the store's clock (`uow.now()`), never from the caller. The returned
    outcome carries the org stream folded from what this unit read plus `org.resumed`; the T154
    refusal summaries the flush appends are in the ledger, not in `events` (a read after the commit
    would see the old state on the Memory store)."""
    _human(actor, "resume a halted organization")
    return _resume_org_in(uow, org_id, actor=actor, via=via, content=content)


def _resume_org_in(
    uow: UnitOfWork,
    org_id: str,
    *,
    actor: str,
    via: str,
    content: Any | None = None,
) -> ResumeOutcome:
    try:
        erased = uow.key_tombstone(org_id) is not None or (
            content is not None and content.retired(org_id) is not None
        )
    except Exception:
        erased = True  # unsure = erased (fail closed)
    if erased:
        raise OrgErasedError("the organization was erased; its halt cannot be lifted")
    stream = org_stream_id(org_id)
    events = _events(uow, stream)
    halted = halt_state(events)
    if not halted.halted:
        return ResumeOutcome(halted, already_running=True, events=tuple(events))
    # T154: the refusals counted during this halt enter the ledger first, in this same
    # transaction (summaries of actor `system`, never the resumer)
    flush_for_resume(uow, org_id, halted.seq, uow.now())
    written = uow.append(
        EventDraft(stream=stream, type=EventType.ORG_RESUMED, actor=actor, payload={"via": via})
    )
    folded = [*events, written]
    return ResumeOutcome(halt_state(folded), already_running=False, events=tuple(folded))


# -- role pause --------------------------------------------------------------------------------


def _append_role_pause(
    uow: UnitOfWork, org_id: str, role: str, actor: str, payload: dict[str, Any]
) -> LedgerEvent | None:
    """Append `org.role_paused` unless the role is already paused (read inside this transaction)."""
    state = role_states(_events(uow, org_stream_id(org_id))).get(role)
    if state is not None and state.paused:
        return None
    return uow.append(
        EventDraft(
            stream=org_stream_id(org_id),
            type=EventType.ORG_ROLE_PAUSED,
            actor=actor,
            payload={"role": role, **payload},
        )
    )


def _role_outcome(
    store: Store,
    org: OrgGraph,
    role: str,
    written: LedgerEvent | None,
    actor: str,
) -> RolePauseOutcome:
    state = role_states(read_org_events(store, org.id))[role]
    if written is None:
        return RolePauseOutcome(state, already_paused=True)
    return RolePauseOutcome(state, False, _stop_role_tasks(store, org, role, actor))


def pause_role(
    store: Store,
    org: OrgGraph,
    role: str,
    *,
    actor: str,
    via: str,
    cause: str = "human",
    rule: str | None = None,
) -> RolePauseOutcome:
    """Pause one role (spec section 5). A person only (`HumanRequired`); the role must be in the
    config (`NoSuchRole`); an already paused role writes nothing. Stop requests go to the role's
    running tasks; the runs are not stopped."""
    _human(actor, "pause a role")
    if role not in {r.id for r in org.roles}:
        raise NoSuchRole(role)
    payload: dict[str, Any] = {"cause": cause, "via": via}
    if rule is not None:
        payload["rule"] = rule
    with store.transaction() as uow:
        written = _append_role_pause(uow, org.id, role, actor, payload)
    return _role_outcome(store, org, role, written, actor)


def pause_role_by_breaker(store: Store, org: OrgGraph, role: str, *, rule: str) -> RolePauseOutcome:
    """The service path of a role pause (`service:breaker`, `via: breaker`)."""
    payload = {"cause": "breaker", "via": "breaker", "rule": rule}
    with store.transaction() as uow:
        written = _append_role_pause(uow, org.id, role, BREAKER_ACTOR, payload)
    return _role_outcome(store, org, role, written, BREAKER_ACTOR)


def resume_role(
    store: Store, org: OrgGraph, role: str, *, actor: str, via: str
) -> RoleResumeOutcome:
    """Lift a role pause. A person only. The role may be an orphan (removed from the config but
    still paused); a name that is neither in the config nor paused is `NoSuchRole`."""
    _human(actor, "resume a role")
    with store.transaction() as uow:
        outcome = _resume_role_in(uow, org, role, actor=actor, via=via)
    if outcome.not_paused:
        return RoleResumeOutcome(outcome.state, not_paused=True)
    return RoleResumeOutcome(role_states(read_org_events(store, org.id))[role], not_paused=False)


def resume_role_in(
    uow: UnitOfWork, org: OrgGraph, role: str, *, actor: str, via: str
) -> RoleResumeOutcome:
    """`resume_role` inside the caller's unit of work (R1.35): opens no transaction; the outcome
    carries the folded org stream (see `resume_org_in`)."""
    _human(actor, "resume a role")
    return _resume_role_in(uow, org, role, actor=actor, via=via)


def _resume_role_in(
    uow: UnitOfWork, org: OrgGraph, role: str, *, actor: str, via: str
) -> RoleResumeOutcome:
    stream = org_stream_id(org.id)
    events = _events(uow, stream)
    state = role_states(events).get(role)
    paused = state is not None and state.paused
    if not paused and role not in {r.id for r in org.roles}:
        raise NoSuchRole(role)
    if not paused:
        return RoleResumeOutcome(state, not_paused=True, events=tuple(events))
    written = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.ORG_ROLE_RESUMED,
            actor=actor,
            payload={"role": role, "via": via},
        )
    )
    folded = [*events, written]
    return RoleResumeOutcome(role_states(folded)[role], not_paused=False, events=tuple(folded))


# -- breakers ----------------------------------------------------------------------------------


def _trip_payload(trip: Trip) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rule": trip.rule,
        "signal": trip.signal.value,
        "action": trip.action.value,
        "observed": trip.observed,
        "limit": trip.limit,
        "window_seconds": trip.window_seconds,
        "window_start": trip.window_start.isoformat(),
        "dry_run": trip.dry_run,
        "unknown_cost_calls": trip.unknown_cost_calls,
    }
    if trip.role is not None:
        payload["role"] = trip.role
    return payload


# The phases of one evaluation (spec 9.5): the ticker blames the first failing one.
PHASE_READ, PHASE_EVALUATE, PHASE_WRITE = "read", "evaluate", "write"


@dataclass(frozen=True)
class EvaluationFailure:
    """Why an evaluation failed: the phase, the exception class and (for `evaluate`) the rule."""

    phase: str
    error_class: str
    failing_rule: str | None
    exception: Exception


@dataclass(frozen=True)
class EvaluationResult:
    """What one evaluation did. `ran` is False when it was skipped (no halting rule, spec 9.7);
    `halting` says whether a halting rule exists, decided from what this evaluation read (an
    unreadable org stream makes every non-dry-run rule halting, spec 9.5)."""

    trips: tuple[Trip, ...]
    failure: EvaluationFailure | None
    ran: bool
    halting: bool


class BreakerEvaluationFailed(Exception):  # noqa: N818 (a state, said in words)
    """An evaluation failed in a phase; `failure` has the diagnosis, `__cause__` the exception."""

    def __init__(self, failure: EvaluationFailure) -> None:
        super().__init__(f"breaker evaluation failed ({failure.phase}: {failure.error_class})")
        self.failure = failure


def read_org_window(store: Store, org_id: str) -> list[LedgerEvent]:
    """Phase `read`, part one: the org stream (latches, resets, disables, halt state)."""
    return read_org_events(store, org_id)


def read_run_window(store: Store, org_id: str, since: datetime) -> dict[str, list[LedgerEvent]]:
    """Phase `read`, part two: entries of the organization's run streams that can still matter
    (head newer than `since`), by stream."""
    for stream in store.damaged_heads():  # a head that cannot be read is not in `heads()`
        if stream.startswith("run:") and _run_org(store, stream) == org_id:
            raise BadTimeError(stream, None)  # fails closed for this organization only (ND238)
    out: dict[str, list[LedgerEvent]] = {}
    for stream, head in store.heads().items():
        if not stream.startswith("run:") or head.at <= since:
            continue
        if _run_org(store, stream) == org_id:
            out[stream] = _stream_events(store, stream)  # BadTimeError: this organization's run
    return out


def _flat(runs: dict[str, list[LedgerEvent]]) -> list[LedgerEvent]:
    return [e for events in runs.values() for e in events]


def _write_trips(
    store: Store,
    org_id: str,
    rules: Sequence[BreakerRule],
    candidates: Sequence[Trip],
    runs: dict[str, list[LedgerEvent]],
    at: datetime,
) -> tuple[tuple[Trip, ...], bool, tuple[str, ...]]:
    """Phase `write`: under the write lock re-read the org stream and the run window, decide again
    from fresh numbers, and write only the trips still above their limit (spec 9.3)."""
    stream = org_stream_id(org_id)
    recorded: list[Trip] = []
    halted_now = False
    paused: list[str] = []
    with store.transaction() as uow:
        current = _events(uow, stream)
        fresh_runs: list[LedgerEvent] = []
        for name, events in runs.items():
            tail = events[-1].seq if events else 0
            more: list[LedgerEvent] = []
            cursor = tail
            while True:
                batch = uow.events_after(name, cursor, PAGE)
                more.extend(batch)
                if len(batch) < PAGE:
                    break
                cursor = batch[-1].seq
            fresh_runs.extend(events + more)
        wanted = {c.rule for c in candidates}
        for trip in evaluate([r for r in rules if r.id in wanted], current, fresh_runs, at):
            tripped = uow.append(
                EventDraft(
                    stream=stream,
                    type=EventType.BREAKER_TRIPPED,
                    actor=BREAKER_ACTOR,
                    payload=_trip_payload(trip),
                )
            )
            current.append(tripped)
            recorded.append(trip)
            if trip.dry_run:
                continue
            if trip.action is BreakerAction.PAUSE_ROLE:
                assert trip.role is not None
                entry = _append_role_pause(
                    uow,
                    org_id,
                    trip.role,
                    BREAKER_ACTOR,
                    {"cause": "breaker", "via": "breaker", "rule": trip.rule},
                )
                if entry is not None:
                    current.append(entry)
                    paused.append(trip.role)
            else:
                halt = _append_halt(
                    uow,
                    org_id,
                    BREAKER_ACTOR,
                    {"cause": "breaker", "via": "breaker", "rule": trip.rule},
                )
                if halt is not None:
                    current.append(halt)
                    halted_now = True
    return tuple(recorded), halted_now, tuple(paused)


def evaluate_breakers_detailed(
    store: Store,
    org_id: str,
    rules: Sequence[BreakerRule],
    *,
    now: datetime | None = None,
    org: OrgGraph | None = None,
    halting_only: bool = False,
) -> EvaluationResult:
    """Count the signals and record every trip, in three phases (`read`, `evaluate`, `write`).
    Never raises for an `Exception`: a failure comes back as the first failing phase (every rule
    is still evaluated, so a healthy rule trips while another fails). With `halting_only` the
    evaluation is skipped unless a halting rule exists (spec 9.7)."""
    checked = check_rules(rules)
    if not checked:
        return EvaluationResult((), None, ran=False, halting=False)
    at = now or store.now()
    failure: EvaluationFailure | None = None
    org_events: list[LedgerEvent] | None = None
    try:
        org_events = read_org_window(store, org_id)
    except Exception as exc:
        failure = EvaluationFailure(PHASE_READ, type(exc).__name__, None, exc)
    halting = bool(halting_rules(checked, org_events))
    if halting_only and not halting:
        return EvaluationResult((), None, ran=False, halting=False)
    runs: dict[str, list[LedgerEvent]] = {}
    if org_events is not None:
        since = at - timedelta(seconds=max(r.window_seconds for r in checked))
        try:
            runs = read_run_window(store, org_id, since)
        except Exception as exc:
            failure = EvaluationFailure(PHASE_READ, type(exc).__name__, None, exc)
    if failure is not None or org_events is None:
        return EvaluationResult((), failure, ran=True, halting=halting)
    candidates: list[Trip] = []
    flat = _flat(runs)
    for rule in checked:
        try:
            state = breaker_state(org_events, rule.id)
            if state.tripped or state.disabled:
                continue  # never evaluated: a disabled rule cannot fail or trip (spec 6.1)
            candidates.extend(evaluate([rule], org_events, flat, at))
        except Exception as exc:
            if failure is None:
                failure = EvaluationFailure(PHASE_EVALUATE, type(exc).__name__, rule.id, exc)
    recorded: tuple[Trip, ...] = ()
    if candidates:
        try:
            recorded, halted_now, paused = _write_trips(
                store, org_id, checked, candidates, runs, at
            )
            if halted_now:
                _stop_open_runs(store, org_id, BREAKER_ACTOR)
            for role in paused:
                _stop_role_tasks(store, org, role, BREAKER_ACTOR)
        except Exception as exc:
            if failure is None:
                failure = EvaluationFailure(PHASE_WRITE, type(exc).__name__, None, exc)
    return EvaluationResult(recorded, failure, ran=True, halting=halting)


def evaluate_breakers(
    store: Store,
    org_id: str,
    rules: Sequence[BreakerRule],
    *,
    now: datetime | None = None,
    org: OrgGraph | None = None,
    halting_only: bool = False,
) -> tuple[Trip, ...]:
    """`evaluate_breakers_detailed`, raising `BreakerEvaluationFailed` (from the original
    exception) when any phase failed. Returns the trips recorded by this call."""
    result = evaluate_breakers_detailed(
        store, org_id, rules, now=now, org=org, halting_only=halting_only
    )
    if result.failure is not None:
        raise BreakerEvaluationFailed(result.failure) from result.failure.exception
    return result.trips


def reset_breaker(
    store: Store, org_id: str, rule_id: str, *, actor: str, via: str
) -> EntryOutcome | None:
    """A person un-trips a breaker (`breaker.reset`). None if it was not tripped (nothing
    written). Does not resume the organization or the role."""
    _human(actor, "reset a breaker")
    with store.transaction() as uow:
        return reset_breaker_in(uow, org_id, rule_id, actor=actor, via=via)


def reset_breaker_in(
    uow: UnitOfWork, org_id: str, rule_id: str, *, actor: str, via: str
) -> EntryOutcome | None:
    """`reset_breaker` inside the caller's unit of work (R1.35); `halted` says whether the
    organization is halted (a reset never changes that)."""
    _human(actor, "reset a breaker")
    stream = org_stream_id(org_id)
    events = _events(uow, stream)
    if not breaker_state(events, rule_id).tripped:
        return None
    entry = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.BREAKER_RESET,
            actor=actor,
            payload={"rule": rule_id, "via": via},
        )
    )
    return EntryOutcome(entry.seq, entry.at, halted=halt_state(events).halted)


def disable_breaker(
    store: Store, org_id: str, rule_id: str, reason: str, *, actor: str, via: str
) -> EntryOutcome:
    """A person switches a rule off (spec 6.1). `reason` is one of the four fixed codes
    (`ValueError` otherwise). A tripped rule cannot be disabled (`StateConflict`
    `breaker_tripped`), nor a disabled one (`already_disabled`)."""
    _human(actor, "disable a breaker")
    DisableReason(reason)
    with store.transaction() as uow:
        return disable_breaker_in(uow, org_id, rule_id, reason, actor=actor, via=via)


def disable_breaker_in(
    uow: UnitOfWork, org_id: str, rule_id: str, reason: str, *, actor: str, via: str
) -> EntryOutcome:
    """`disable_breaker` inside the caller's unit of work (R1.35): opens no transaction."""
    _human(actor, "disable a breaker")
    code = DisableReason(reason).value
    stream = org_stream_id(org_id)
    state = breaker_state(_events(uow, stream), rule_id)
    if state.tripped:
        raise StateConflict("breaker_tripped")
    if state.disabled:
        raise StateConflict("already_disabled")
    entry = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.BREAKER_DISABLED,
            actor=actor,
            payload={"rule": rule_id, "reason": code, "via": via},
        )
    )
    return EntryOutcome(entry.seq, entry.at)


def enable_breaker(
    store: Store, org_id: str, rule_id: str, *, actor: str, via: str
) -> EntryOutcome:
    """A person switches a disabled rule back on; its count starts afresh (the window floor is
    the enable time). A rule that is not disabled is `StateConflict` `not_disabled`."""
    _human(actor, "enable a breaker")
    with store.transaction() as uow:
        return enable_breaker_in(uow, org_id, rule_id, actor=actor, via=via)


def enable_breaker_in(
    uow: UnitOfWork, org_id: str, rule_id: str, *, actor: str, via: str
) -> EntryOutcome:
    """`enable_breaker` inside the caller's unit of work (R1.35): opens no transaction."""
    _human(actor, "enable a breaker")
    stream = org_stream_id(org_id)
    if not breaker_state(_events(uow, stream), rule_id).disabled:
        raise StateConflict("not_disabled")
    entry = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.BREAKER_ENABLED,
            actor=actor,
            payload={"rule": rule_id, "via": via},
        )
    )
    return EntryOutcome(entry.seq, entry.at)


def trip_breaker(
    store: Store,
    org: OrgGraph,
    rules: Collection[BreakerRule],
    name: str,
    *,
    actor: str,
    via: str,
    now: datetime | None = None,
) -> TripOutcome:
    """Trip a rule by hand (drills, spec section 10). A person only. Records `breaker.tripped`
    with the person as actor and `manual: true`, the rule's live numbers, then does what an
    automatic trip does (halt the org or pause the role) as that person, unless a dry run. A
    tripped rule writes nothing; a disabled one is refused (`BreakerDisabled`), re-checked inside
    the write transaction so a disable that lands first wins."""
    _human(actor, "trip a breaker")
    rule = next((r for r in rules if r.id == name), None)
    if rule is None:
        raise NoSuchBreaker(name)
    at = now or store.now()
    since = at - timedelta(seconds=rule.window_seconds)
    runs = _flat(read_run_window(store, org.id, since))
    stream = org_stream_id(org.id)
    halted = False
    paused: str | None = None
    with store.transaction() as uow:
        current = _events(uow, stream)
        state = breaker_state(current, rule.id)
        if state.tripped:
            return TripOutcome(already_tripped=True)
        if state.disabled:
            raise BreakerDisabled(rule.id)
        seen = observe(rule, runs, at, state.floor)
        payload: dict[str, Any] = {
            "rule": rule.id,
            "signal": rule.signal.value,
            "action": rule.action.value,
            "observed": seen.observed_text(rule),
            "limit": rule.limit_text,
            "window_seconds": rule.window_seconds,
            "window_start": seen.start.isoformat(),
            "dry_run": rule.dry_run,
            "unknown_cost_calls": seen.unknown_cost_calls,
            "manual": True,
        }
        if rule.role is not None:
            payload["role"] = rule.role
        entry = uow.append(
            EventDraft(stream=stream, type=EventType.BREAKER_TRIPPED, actor=actor, payload=payload)
        )
        if not rule.dry_run:
            extra = {"cause": "breaker", "via": via, "rule": rule.id}
            if rule.action is BreakerAction.PAUSE_ROLE:
                assert rule.role is not None
                paused_entry = _append_role_pause(uow, org.id, rule.role, actor, extra)
                paused = rule.role if paused_entry is not None else None
            else:
                halted = _append_halt(uow, org.id, actor, extra) is not None
    if halted:
        _stop_open_runs(store, org.id, actor)
    if paused is not None:
        _stop_role_tasks(store, org, paused, actor)
    return TripOutcome(False, entry.seq, halted, paused)


def known_roles(org: OrgGraph) -> frozenset[str]:
    """The role ids of the configuration."""
    return frozenset(r.id for r in org.roles)


__all__ = [
    "BREAKER_ACTOR",
    "BreakerDisabled",
    "BreakerEvaluationFailed",
    "EntryOutcome",
    "EvaluationFailure",
    "EvaluationResult",
    "HaltOutcome",
    "HumanRequired",
    "NoSuchBreaker",
    "NoSuchRole",
    "ResumeOutcome",
    "RolePauseOutcome",
    "RoleResumeOutcome",
    "StateConflict",
    "TripOutcome",
    "disable_breaker",
    "enable_breaker",
    "evaluate_breakers",
    "evaluate_breakers_detailed",
    "halt_by_breaker",
    "halt_org",
    "known_roles",
    "open_runs",
    "pause_role",
    "pause_role_by_breaker",
    "read_org_window",
    "read_run_window",
    "reset_breaker",
    "resume_org",
    "OrgErasedError",
    "resume_role",
    "trip_breaker",
]
