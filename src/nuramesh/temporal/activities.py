"""The activities: each one calls a public entry of the run engine and returns ids and statuses.

They are idempotent because the engine is (T63): a task step already acknowledged in the store is
not repeated, so a retry, a worker restart or a duplicate delivery never calls a worker, a model
or an action again. A step cut short runs again in a new epoch, recorded in the ledger, and the
reservations that dead try left open are closed first. **Not promised (ND55):** the calls the
dead try already made may be made again.

Every activity builds a fresh engine from the factory (`EngineFactory`, given the organization
id and the run id): the engine keeps per-run state, and tasks of one run run in parallel.
Failures leave as coded errors only (`errors.guarded`).
"""

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from temporalio import activity

from nuramesh.core.tasks import TaskStatus
from nuramesh.engine.content import WORKING_DAYS, ContentService
from nuramesh.engine.light import RunBuilder
from nuramesh.engine.run import RunEngine, TaskStepResult
from nuramesh.engine.steps import Settled
from nuramesh.temporal.errors import guarded
from nuramesh.temporal.intake import (
    DEFAULT_LEASE,
    DEFAULT_RECHECK,
    DEFAULT_START_TTL,
    take_start_request,
)
from nuramesh.temporal.models import (
    DependencyRef,
    FinishRef,
    PlanView,
    RunRef,
    StartView,
    StepView,
    TaskRef,
    WarnRef,
    WarnView,
)

EngineFactory = Callable[[str, str], RunEngine]
"""Builds the engine for an organization id and the run id the activity is about: store, workers,
content service, policies."""

MAX_ATTEMPTS = 5
"""Tries per activity (the workflow's retry policy says the same). On the last one a task step
puts the task in front of a human instead of failing the run (`RunEngine.run_task_step`)."""

TAKE_REQUEST = "nuramesh.take_start_request"
READ_PLAN = "nuramesh.read_plan"
RUN_TASK = "nuramesh.run_task"
CANCEL_FOR_DEPENDENCY = "nuramesh.cancel_for_dependency"
WAIT_FOR_DEPENDENCY = "nuramesh.wait_for_dependency"
CANCEL_FOR_STOP = "nuramesh.cancel_for_stop"
WARN_HUMAN_WAIT = "nuramesh.warn_human_wait"
EXPIRE_HUMAN_WAIT = "nuramesh.expire_human_wait"
STOP_REQUESTED = "nuramesh.stop_requested"
FINISH_RUN = "nuramesh.finish_run"


def _view(result: TaskStepResult, now: datetime) -> StepView:
    """The step as the workflow sees it. `now` is the engine's store time, the same clock as the
    ledger's `waiting_since`, so the wait clock and the 90-day expiry never mix two clocks."""
    waited = (
        max(0.0, (now - result.waiting_since).total_seconds())
        if result.waiting_since is not None
        else None
    )
    return StepView(
        task_id=result.task_id,
        status=result.status.value,
        waiting=result.waiting,
        stopped=result.stopped,
        waited_seconds=waited,
        round=f"{result.attempt}.{result.submission}",
        not_due=result.not_due,
        due_in_seconds=(
            max(0.0, (result.due_at - now).total_seconds()) if result.due_at is not None else None
        ),
    )


def _step(engine: RunEngine, call: Callable[[RunEngine], TaskStepResult]) -> StepView:
    """Run one engine step and view it at the engine's store time."""
    return _view(call(engine), engine.store.now())


class RunActivities:
    """The activity implementations over one engine factory."""

    def __init__(
        self,
        factory: EngineFactory,
        builder: RunBuilder | None = None,
        *,
        lease: timedelta = DEFAULT_LEASE,
        start_ttl: timedelta = DEFAULT_START_TTL,
        recheck: timedelta = DEFAULT_RECHECK,
        content: ContentService | None = None,
    ) -> None:
        self._content = content
        self._factory = factory
        self._builder = builder
        self._lease, self._start_ttl, self.recheck = lease, start_ttl, recheck
        self._owner = secrets.token_hex(8)  # random: never a host name or a process id

    def all(self) -> list[Callable[..., object]]:
        return [
            self.take_start_request,
            self.read_plan,
            self.run_task,
            self.cancel_for_dependency,
            self.wait_for_dependency,
            self.cancel_for_stop,
            self.warn_human_wait,
            self.expire_human_wait,
            self.stop_requested,
            self.finish_run,
        ]

    @activity.defn(name=TAKE_REQUEST)
    def take_start_request(self, ref: RunRef) -> StartView:
        """The run's start request, claimed and begun here (ND347 6.1). `none` when there is no
        request: the caller began the run itself."""

        def call() -> StartView:
            outcome = take_start_request(
                self._factory(ref.org_id, ref.run_id).store,
                self._builder,
                ref.org_id,
                ref.run_id,
                owner=self._owner,
                lease=self._lease,
                start_ttl=self._start_ttl,
                recheck=self.recheck,
                content=self._content,
            )
            return StartView(outcome=outcome, recheck_seconds=self.recheck.total_seconds())

        return guarded(call)

    @activity.defn(name=READ_PLAN)
    def read_plan(self, ref: RunRef) -> PlanView:
        def call() -> PlanView:
            engine = self._factory(ref.org_id, ref.run_id)
            plan = engine.stored_plan(ref.run_id)
            return PlanView(
                tasks=list(plan.tasks),
                depends_on={k: list(v) for k, v in plan.depends_on.items()},
                ended=plan.ended,
                max_parallel=engine.settings.max_parallel,
                warn_days=list(engine.settings.human_wait_warn_days),
                working_days=WORKING_DAYS,
            )

        return guarded(call)

    @activity.defn(name=RUN_TASK)
    def run_task(self, ref: TaskRef) -> StepView:
        last = activity.info().attempt >= MAX_ATTEMPTS
        return guarded(
            lambda: _step(
                self._factory(ref.org_id, ref.run_id),
                lambda e: e.run_task_step(ref.run_id, ref.task_id, last_try=last),
            )
        )

    @activity.defn(name=CANCEL_FOR_DEPENDENCY)
    def cancel_for_dependency(self, ref: DependencyRef) -> StepView:
        settled = Settled(TaskStatus(ref.dependency_status), ref.dependency_waiting)
        return guarded(
            lambda: _step(
                self._factory(ref.org_id, ref.run_id),
                lambda e: e.cancel_for_dependency(ref.run_id, ref.task_id, ref.dependency, settled),
            )
        )

    @activity.defn(name=WAIT_FOR_DEPENDENCY)
    def wait_for_dependency(self, ref: DependencyRef) -> StepView:
        settled = Settled(TaskStatus(ref.dependency_status), ref.dependency_waiting)
        return guarded(
            lambda: _step(
                self._factory(ref.org_id, ref.run_id),
                lambda e: e.wait_for_dependency(ref.run_id, ref.task_id, ref.dependency, settled),
            )
        )

    @activity.defn(name=CANCEL_FOR_STOP)
    def cancel_for_stop(self, ref: TaskRef) -> StepView:
        return guarded(
            lambda: _step(
                self._factory(ref.org_id, ref.run_id),
                lambda e: e.cancel_for_stop(ref.run_id, ref.task_id),
            )
        )

    @activity.defn(name=WARN_HUMAN_WAIT)
    def warn_human_wait(self, ref: WarnRef) -> WarnView:
        def call() -> WarnView:
            engine = self._factory(ref.org_id, ref.run_id)
            result = engine.warn_human_wait_result(ref.run_id, ref.task_id, ref.days_left)
            opens = result.opens_at
            return WarnView(
                result.written,
                max(0.0, (opens - engine.store.now()).total_seconds())
                if opens is not None
                else None,
            )

        return guarded(call)

    @activity.defn(name=EXPIRE_HUMAN_WAIT)
    def expire_human_wait(self, ref: TaskRef) -> StepView:
        return guarded(
            lambda: _step(
                self._factory(ref.org_id, ref.run_id),
                lambda e: e.expire_human_wait(ref.run_id, ref.task_id),
            )
        )

    @activity.defn(name=STOP_REQUESTED)
    def stop_requested(self, ref: RunRef) -> bool:
        return guarded(lambda: self._factory(ref.org_id, ref.run_id).stop_requested(ref.run_id))

    @activity.defn(name=FINISH_RUN)
    def finish_run(self, ref: FinishRef) -> dict[str, int]:
        return guarded(
            lambda: self._factory(ref.org_id, ref.run_id).finish_run(
                ref.run_id, stop_acted=ref.stop_acted, aborted=ref.aborted
            )
        )
