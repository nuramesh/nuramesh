"""The begin sequence for one claimed start request (ND347 4.6), shared by every runner.

`begin_from_request` is the ONE place that checks a request (expiry, builder refusal, erased or
halted organization, keyed plan hash, plan checks, per-assignee budgets), calls `begin_run` once and
maps every failure to a fixed refusal code (E33 included). The light engine's `LightRunner` and the
Temporal intake both call it, so the two cannot drift; each passes `on_started`, which runs in the
transaction that moves the request to `started` (the light engine puts its job row there).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from nuramesh.core.budgets import BudgetExceededError
from nuramesh.core.ledger import EventType
from nuramesh.engine.admission import TEMPORAL, TEMPORAL_ADMISSION, RunnerAdmission, admit
from nuramesh.engine.budgets import NoBudgetError, start_budget_problem
from nuramesh.engine.content import WORKING_DAYS, ContentError, ContentService
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.governance import ModelGovernanceError
from nuramesh.engine.halt_refusal import REFUSAL_ACTOR
from nuramesh.engine.org_halt import HaltWatch, OrgHalted
from nuramesh.engine.org_ledger import org_stream
from nuramesh.engine.run import (
    PlannedTask,
    PlanRefused,
    RunEngine,
    StartInfo,
)
from nuramesh.engine.starter import parse_plan
from nuramesh.stores import ConcurrencyError, EventDraft, Store, UnitOfWork
from nuramesh.stores.base import run_stream
from nuramesh.stores.run_queue import START_REFUSED, START_STARTED, StartRequestRow

__all__ = [
    "REFUSALS",
    "REFUSED_TEXT_KEPT",
    "ClaimLost",
    "PreparedRun",
    "RunBuilder",
    "StartRefused",
    "begin_from_request",
    "extend_claimed_plan_text",
    "hold_request",
    "org_halted",
    "refuse_request",
]


class StartRefused(Exception):  # noqa: N818 (a decision, said with a code)
    """Raised by a `RunBuilder` when the request must not start. The code is stored on the request
    and in the ledger; it is never free text (`real_model_not_supported`, `plan_unknown_role`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedRun:
    """What a builder returns: the engine and the plan, built from the team's code."""

    engine: RunEngine
    plan: Sequence[PlannedTask]
    data_class: str | None = None


class RunBuilder(Protocol):
    """Turns a claimed start request into an engine and a plan, from the team's code (it resolves
    each `PlanItem` role to an assignee; `parse_plan(request.plan)` gives the items). Supplied by
    whoever runs the runner (the CLI worker); raises `StartRefused(code)` to refuse."""

    def build(self, request: StartRequestRow) -> PreparedRun: ...


REFUSALS: tuple[tuple[type[BaseException], str], ...] = (
    (NoBudgetError, "run_budget_missing"),
    (BudgetExceededError, "run_budget_exhausted"),
    (ModelGovernanceError, "model_governance_refused"),
    (ContentError, "org_unavailable"),
    (ValueError, "plan_invalid"),
)
"""Exceptions of `begin_run` that mean "this request is refused", in order (the first match). A
`PlanRefused` brings its own code; `OrgHalted` holds the request instead (nothing is written)."""


REFUSED_TEXT_KEPT = timedelta(days=7)
"""How long the plan text of a refused start request is kept (RET-PT (b))."""


def plan_text_ids(row: StartRequestRow) -> list[str]:
    """The content ids of the plan text a request names (templates are not plan text)."""
    try:
        items = parse_plan(row.plan)
    except ValueError:
        return []
    return [i for item in items for i in (item.title_ref, item.dod_ref) if i is not None]


def extend_claimed_plan_text(
    content: ContentService, uow: UnitOfWork, row: StartRequestRow, now: datetime
) -> None:
    """In the claim's transaction: lengthen the plan text to the first wait's value (never
    shortens); the capture expiry was only for a request nobody handles (RET-PT, R50)."""
    content.extend_plan_text(
        uow, row.org_id, plan_text_ids(row), at=now + timedelta(days=WORKING_DAYS)
    )


def org_halted(store: Store, org_id: str) -> bool:
    """A halt in force, or one that cannot be read (an unreadable halt is a halt)."""
    try:
        return HaltWatch(store, org_id).state().halted
    except Exception:
        return True


class ClaimLost(Exception):  # noqa: N818 (the claim was taken over: roll back)
    pass


def begin_from_request(
    store: Store,
    builder: RunBuilder,
    row: StartRequestRow,
    owner: str,
    *,
    start_ttl: timedelta,
    recheck: timedelta,
    on_started: Callable[[UnitOfWork, StartRequestRow], object],
    keep: Callable[[StartRequestRow], None] = lambda row: None,
    content: ContentService | None = None,
    admission: RunnerAdmission | None = None,
) -> None:
    """The checks of ND347 4.6 in order, then `begin_run` once (module docstring). After the plan
    checks and before `begin_run` the admission checks of R10 decide (the
    budget check is its check 6); `admission` says what this runner is (None: the most closed
    one for the request's engine: fake models only, no sandbox). The
    organization's state comes first (4.6): an erased or key-lost organization gets no entry, not
    even a refusal, and a halted one keeps the request. Expiry is judged on the store's time (the
    unit of work's `at`, never the host clock) and only for a run that has not started: a run
    whose `run.started` exists is always completed or ended (section 1(3))."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        refuse_request(store, *args, content=content, **kwargs)

    denial = deny_checks(store, row.org_id)
    if denial is not None:
        _deny(store, row, owner, denial, recheck, content)
        return
    started = _run_started(store, row.run_id)  # BEFORE the builder: a dead claimer may have begun
    if started is not None and not _ours(started, row):
        # the run id belongs to another organization or was not started by this request: the run
        # is left completely alone (no cancel, no run.ended, no write to its stream)
        refuse(row, owner, "run_id_conflict", REFUSAL_ACTOR)
        return
    try:
        prepared = builder.build(row)
    except StartRefused as refused:
        _refuse(
            store, row, owner, refused.code, REFUSAL_ACTOR,
            started=started is not None, content=content,
        )  # fmt: skip
        return
    keep(row)
    engine = prepared.engine
    denial = deny_checks(
        store,
        row.org_id,
        content=getattr(engine, "content", None),
        hashing=(lambda: engine.hashing) if getattr(engine, "hashing", None) else None,
    )
    if denial is not None:
        _deny(store, row, owner, denial, recheck, content)
        return
    started = _run_started(store, row.run_id)  # read again: the build took time
    if started is not None and not _ours(started, row):
        refuse(row, owner, "run_id_conflict", engine.settings.actor)
        return
    if started is None:
        with store.transaction() as uow:
            expired = row.created_at + start_ttl <= uow.at
        if expired:
            refuse(row, owner, "run_start_expired", REFUSAL_ACTOR)
            return
    if engine.keyed_plan_hash(row.plan) != row.plan_hash:
        _refuse(
            store, row, owner, "plan_hash_mismatch", engine.settings.actor,
            started=started is not None, engine=engine, content=content,
        )  # fmt: skip
        return
    stored = engine.stored_plan(row.run_id) if started is not None else None
    if stored is not None:
        if stored.plan_hash != row.plan_hash:
            _refuse(
                store, row, owner, "plan_hash_mismatch", engine.settings.actor,
                started=True, engine=engine, content=content,
            )  # fmt: skip
            return
        if stored.ended:  # begin_run aborted: no task is created in an ended run (E33)
            refuse(row, owner, "run_start_aborted", engine.settings.actor)
            return
        keep(row)
        engine.complete_begin(row.run_id, prepared.plan)  # ... and died among the tasks
    else:
        try:
            planned = engine.check_start(row.run_id, prepared.plan)
            runner = admission or (
                TEMPORAL_ADMISSION if row.engine == TEMPORAL else RunnerAdmission()
            )
            code = admit(  # R10: checks 1 to 5, the budget check, the sandbox
                engine,
                row.org_id,
                planned,
                data_class=row.data_class,
                runner=runner,
                at=store.now(),
                budget_check=lambda: start_budget_problem(
                    store,
                    engine.org,
                    row.run_id,
                    {p.task.id: p.task.assignee for p in planned if p.task.assignee},
                ),
            )
            if code is not None:
                refuse(row, owner, code, engine.settings.actor)
                return
            keep(row)
            engine.begin_run(
                row.run_id,
                planned,
                data_class=row.data_class,
                start=StartInfo(
                    started_by=row.started_by,
                    engine=row.engine,
                    plan_text=row.plan,
                    idempotency_key_hash=row.idempotency_key_hash,
                ),
            )
        except ConcurrencyError:
            raise
        except OrgHalted:  # halted between the check and the start: wait, write nothing more
            hold_request(store, row, owner, recheck)
            return
        except Exception as exc:
            code = exc.code if isinstance(exc, PlanRefused) else None
            code = code or next((c for t, c in REFUSALS if isinstance(exc, t)), None)
            if _ended_by_abort(engine, row.run_id):  # begin_run wrote run.ended (E33)
                refuse(row, owner, "run_start_aborted", engine.settings.actor)
                return
            if code is None:
                raise
            reason = None
            if isinstance(exc, ModelGovernanceError) and exc.blocks:
                reason = str(exc.blocks[0].reason)
            refuse(row, owner, code, engine.settings.actor, reason_code=reason)
            return
    with store.transaction() as uow:
        on_started(uow, row)
        if not uow.finish_start_request(row.org_id, row.run_id, owner, START_STARTED):
            raise ClaimLost  # the claim was taken over: the new claimer finishes it


def _refuse(
    store: Store,
    row: StartRequestRow,
    owner: str,
    code: str,
    actor: str,
    *,
    started: bool,
    engine: Any = None,
    content: ContentService | None = None,
) -> None:
    """Refuse a request. Once `run.started` exists the run is never left started without an end:
    it ends `aborted` (one `run.ended`; the engine, when there is one, also cancels the tasks) and
    the request is `run_start_aborted`, the cause kept as the entry's `reason_code` (section 1(3),
    E31, E33). Before `run.started` it is the plain refusal."""
    if not started:
        refuse_request(store, row, owner, code, actor, content=content)
        return
    _end_aborted(store, row, engine, code)
    refuse_request(store, row, owner, "run_start_aborted", actor, reason_code=code, content=content)


def _end_aborted(store: Store, row: StartRequestRow, engine: Any, code: str) -> None:
    """Write the one `run.ended` of a started run that will not go on (nothing if it ended)."""
    if _run_ended(store, row.run_id):
        return
    if engine is not None:
        try:
            engine.finish_run(row.run_id, stop_acted=False, aborted=code)
        except Exception:  # noqa: S110 (the direct write below still ends the run)
            pass
        if _run_ended(store, row.run_id):
            return
    with store.transaction() as uow:  # no engine (the builder refused): the end alone
        uow.append(
            EventDraft(
                stream=run_stream(row.run_id),
                type=EventType.RUN_ENDED,
                actor=REFUSAL_ACTOR,
                run_id=row.run_id,
                payload={"aborted": True, "error": code},
            )
        )


def _ours(started: Any, row: StartRequestRow) -> bool:
    """A started run is this request's only when it is the row's organization and the run was
    started with the row's plan hash (a plain text compare); any other run is not ours (n6)."""
    return bool(
        started.payload.get("org") == row.org_id
        and started.payload.get("plan_hash") == row.plan_hash
    )


def _run_ended(store: Store, run_id: str) -> bool:
    return any(
        e.type == EventType.RUN_ENDED for e in store.read_stream(run_stream(run_id), after_seq=0)
    )


def _deny(
    store: Store,
    row: StartRequestRow,
    owner: str,
    denial: Any,
    recheck: timedelta,
    content: ContentService | None = None,
) -> None:
    if denial.kind in ("erased", "key_lost"):
        refuse_request(
            store, row, owner, f"org_{denial.kind}", REFUSAL_ACTOR, entry=False, content=content
        )
    else:  # halted since the claim (or unsure): the request waits, nothing is written
        hold_request(store, row, owner, recheck)


def _run_started(store: Store, run_id: str) -> Any:
    """The first `run.started` of the run's stream, or None."""
    for event in store.read_stream(run_stream(run_id), after_seq=0):
        if event.type == EventType.RUN_STARTED:
            return event
    return None


def _ended_by_abort(engine: Any, run_id: str) -> bool:
    """`begin_run` failed after `run.started` and ended the run `aborted`: the request is
    `run_start_aborted` whatever the exception was, and no task is created (E33)."""
    try:
        stored = engine.stored_plan(run_id)
    except Exception:
        return False
    return stored is not None and stored.ended


def hold_request(store: Store, row: StartRequestRow, owner: str, recheck: timedelta) -> None:
    """Keep the request `pending` for a later look (a halt in force): nothing is written."""
    with store.transaction() as uow:
        uow.hold_start_request(row.org_id, row.run_id, owner, recheck)


def refuse_request(
    store: Store,
    row: StartRequestRow,
    owner: str,
    code: str,
    actor: str,
    *,
    entry: bool = True,
    reason_code: str | None = None,
    content: ContentService | None = None,
) -> None:
    """End the request `refused` with a fixed code and, unless the organization's stream is
    closed (`entry=False`), write the one ledger entry, in one transaction."""
    payload = {"kind": "run_start_refused", "run_id": row.run_id, "code": code}
    if reason_code is not None:
        payload["reason_code"] = reason_code
    with store.transaction() as uow:
        if content is not None:  # RET-PT (b): the text goes 7 days after the refusal
            content.set_plan_text_expiry(
                uow, row.org_id, plan_text_ids(row), at=store.now() + REFUSED_TEXT_KEPT
            )
        if entry:
            uow.append(
                EventDraft(
                    stream=org_stream(row.org_id),
                    type=EventType.DECISION_RECORDED,
                    actor=actor,
                    payload=payload,
                )
            )
        if not uow.finish_start_request(row.org_id, row.run_id, owner, START_REFUSED, code=code):
            raise ClaimLost
