"""One workflow per run (R1.20 PR 3, ND55, T72).

**Input:** the run id and the organization id, nothing else. The plan (task ids and dependency
edges) is read from the database by the first activity; free-text titles never enter Temporal.

**Tasks:** each task is a coroutine that waits for its dependencies, then runs its steps in one
activity (`run_task`), up to `max_parallel` at a time. A dependency that did not end `done`
cancels the task with the reason the in-process engine gives (`engine.steps`, shared code).

**Human waits:** a task that ends the activity waiting in `verifying` for a person is **not
polled**. The workflow waits on a signal (`human_decided` after a decision is recorded, `stop`) or
a durable timer, and re-evaluates from the database. The timers count from the ledger time of the
task's submit move: a warning at each lead time before the 90 days (default 14 and 3; a ledgered
`human_wait_warning`), and at 90 days the task is stopped (`human_wait_expired`, then `cancelled`).
While a task is in this wait, the tasks that depend on it (directly or through others) are held
`proposed` (R1.20a), each with a `dependency_waiting` ledger note written when the wait begins.
When the wait ends the task's final answer settles them: approved, they run; rejected for good,
stopped or expired, they are cancelled with the reason. A dependency that ended some other way
(cancelled, or `needs_human` or `blocked`, which nothing in this run can approve) cancels its
dependents at once, even while another dependency is still in a human wait. After a decision the
gates run again, including the non-human ones (Q3, option A).

**Replay:** the command sequence changed with R1.20a (a dependent now waits for the final answer
and a note activity runs when a wait begins). A `workflow.patched` guard is not used: the durable
path is not deployed (evaluation only, ND53), so no workflow history written before R1.20a exists.
Once one does, later changes need `workflow.patched`.

**Stop:** the `stop` signal, or a Stop request found in the ledger, cancels what has not started
and every waiting task; running steps stop themselves between steps (as in-process).
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from nuramesh.core.tasks import TaskStatus
    from nuramesh.engine.steps import Settled, blocking_dependency
    from nuramesh.engine.waits import WaitClock
    from nuramesh.temporal import activities as names
    from nuramesh.temporal.models import (
        DependencyRef,
        FinishRef,
        PlanView,
        RunRef,
        RunResult,
        StartView,
        StepView,
        TaskRef,
        WarnRef,
        WarnView,
    )

KEY_GATE_CODES = frozenset(
    {"org_key_destroyed", "org_key_missing", "org_key_mismatch", "key_tombstone_unavailable"}
)
"""R1.37: the fixed codes an activity fails with when the key gate refuses; `run.ended` records the
code, not a class name."""
ABORT_CODES = KEY_GATE_CODES | {"canon_integrity", "canon_rejected"}
"""Also the canon codes (R3.00): a canon that failed verification ends the run with its code."""


def _abort_code(exc: BaseException) -> str:
    """What `run.ended.error` says: the gate's code when an activity failed for one (the cause of
    the activity error carries only the code), else the class name as before."""
    cause = getattr(exc, "cause", None)
    if isinstance(cause, ApplicationError) and cause.type in ABORT_CODES:
        return cause.type
    return type(exc).__name__


WORKFLOW_NAME = "nuramesh.run"
SIGNAL_HUMAN_DECIDED = "human_decided"
SIGNAL_STOP = "stop"

STEP_TIMEOUT = timedelta(hours=1)
"""How long one activity attempt may take before Temporal presumes its worker dead and retries.
Make it longer than the slowest task."""
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=names.MAX_ATTEMPTS,
)


def _settled(view: StepView) -> Settled:
    return Settled(TaskStatus(view.status), view.waiting)


MAX_HELD_PASSES = 100  # each held pass adds about 8 history events: continue as new here


@workflow.defn(name=WORKFLOW_NAME)
class RunWorkflow:
    """Drives one durable run to its end."""

    def __init__(self) -> None:
        self._signals = 0  # wakes every waiting task (a Stop, a decision with no task named)
        self._marks: dict[str, int] = {}  # wakes one task (a decision for it)
        self._stop = False  # true only once the ledger has `run.stop_requested`
        self._stop_acted = False

    @workflow.signal(name=SIGNAL_HUMAN_DECIDED)
    def human_decided(self, task_id: str = "") -> None:
        """A decision was recorded: look at the database. Wakes the task named, or all."""
        if task_id:
            self._marks[task_id] = self._marks.get(task_id, 0) + 1
        else:
            self._signals += 1

    @workflow.signal(name=SIGNAL_STOP)
    def stop(self) -> None:
        """Wakes everyone to look at the ledger. It stops nothing by itself: a Stop is real only
        when `run.stop_requested` is in the ledger (`_stop_found`)."""
        self._signals += 1

    def _count(self, task_id: str) -> int:
        return self._signals + self._marks.get(task_id, 0)

    async def _activity(self, name: str, arg: object, result_type: type) -> Any:
        return await workflow.execute_activity(
            name,
            arg,
            result_type=result_type,
            start_to_close_timeout=STEP_TIMEOUT,
            retry_policy=RETRY,
        )

    @workflow.run
    async def run(self, ref: RunRef) -> RunResult:
        # the start request, if the API recorded one: claimed and begun here, in the worker
        passes = 0
        while True:
            start: StartView = await self._activity(names.TAKE_REQUEST, ref, StartView)
            if start.outcome == "refused":
                return RunResult()
            if start.outcome != "held":
                break
            passes += 1
            if passes >= MAX_HELD_PASSES:
                # a long hold (an org halt) adds history on every pass: carry on in a fresh
                # history with the same input; everything is read from the database anyway
                workflow.continue_as_new(ref)
            await workflow.sleep(timedelta(seconds=max(1.0, start.recheck_seconds)))
        plan: PlanView = await self._activity(names.READ_PLAN, ref, PlanView)
        if plan.ended:
            return RunResult()
        settled: dict[str, Settled] = {}
        slots = asyncio.Semaphore(max(1, plan.max_parallel))
        aborted = ""
        counts: dict[str, int] = {}
        try:
            await asyncio.gather(
                *(self._task(ref, plan, tid, settled, slots) for tid in plan.tasks)
            )
        except BaseException as exc:  # a cancel, or an activity that failed for good
            aborted = _abort_code(exc)
            raise
        finally:
            # Always try to end the run in the ledger (`run.ended`), also after a cancel: the
            # awaits below run after the cancellation was delivered. If this fails too, the
            # original error stays and the run is left un-ended; starting the workflow again
            # (same id) carries it on, because everything is read from the database.
            try:
                counts = await self._activity(
                    names.FINISH_RUN,
                    FinishRef(ref.run_id, ref.org_id, self._stop_acted, aborted),
                    dict[str, int],
                )
            except BaseException:
                if not aborted:
                    raise
        return RunResult(counts=counts, stopped=self._stop_acted)

    async def _step(self, name: str, arg: object) -> StepView:
        view: StepView = await self._activity(name, arg, StepView)
        if view.stopped:
            self._stop = self._stop_acted = True
            self._signals += 1
        return view

    async def _stop_found(self, ref: RunRef) -> bool:
        """Whether the ledger has a Stop request (the only thing that makes a Stop real)."""
        if not self._stop:
            found: bool = await self._activity(names.STOP_REQUESTED, ref, bool)
            if found:
                self._stop = True
                self._signals += 1
        return self._stop

    async def _task(
        self,
        ref: RunRef,
        plan: PlanView,
        tid: str,
        settled: dict[str, Settled],
        slots: asyncio.Semaphore,
    ) -> None:
        deps = plan.depends_on.get(tid, [])

        def decided() -> bool:
            """Every dependency ended, or one ended without `done` (the task can never run)."""
            return all(d in settled for d in deps) or any(
                d in settled and settled[d].status is not TaskStatus.DONE for d in deps
            )

        while not self._stop and not decided():
            mark = self._signals

            def ready(since: int = mark) -> bool:
                return self._stop or decided() or self._signals > since

            await workflow.wait_condition(ready)
            if self._signals > mark:
                await self._stop_found(ref)
        task = TaskRef(ref.run_id, ref.org_id, tid)
        if await self._stop_found(ref):
            view = await self._step(names.CANCEL_FOR_STOP, task)
            self._stop_acted = True
            settled[tid] = _settled(view)
            return
        bad = blocking_dependency({d: settled[d] for d in deps if d in settled})
        if bad is not None:
            view = await self._step(
                names.CANCEL_FOR_DEPENDENCY,
                DependencyRef(
                    ref.run_id,
                    ref.org_id,
                    tid,
                    bad,
                    settled[bad].status.value,
                    settled[bad].waiting,
                ),
            )
            settled[tid] = _settled(view)
            return
        seen = self._count(tid)  # before the step: a decision that lands during it is not lost
        async with slots:
            view = await self._step(names.RUN_TASK, task)
        if view.status == TaskStatus.VERIFYING.value and view.waiting:
            began = workflow.now()  # the wait began when the step returned, not after the notes
            # dependents wait for the final answer (R1.20a): settled only when the wait is over,
            # and held with a ledger note from the moment it begins
            for held in self._dependents_of(plan, tid):
                await self._step(
                    names.WAIT_FOR_DEPENDENCY,
                    DependencyRef(
                        ref.run_id, ref.org_id, held, tid, TaskStatus.VERIFYING.value, True
                    ),
                )
            view = await self._human_wait(ref, plan, task, view, seen, slots, began)
        settled[tid] = _settled(view)

    @staticmethod
    def _dependents_of(plan: PlanView, tid: str) -> list[str]:
        """Every task that depends on `tid`, directly or through others (in plan order)."""
        found = {tid}
        grew = True
        while grew:
            grew = False
            for other in plan.tasks:
                if other not in found and any(d in found for d in plan.depends_on.get(other, [])):
                    found.add(other)
                    grew = True
        return [t for t in plan.tasks if t in found and t != tid]

    async def _human_wait(
        self,
        ref: RunRef,
        plan: PlanView,
        task: TaskRef,
        view: StepView,
        seen: int,
        slots: asyncio.Semaphore,
        began: datetime,
    ) -> StepView:
        """Wait for the person without polling: a signal or a durable timer, and then **always the
        database** before acting. Nothing here trusts a signal or a timer: a warning or the expiry
        is only written after a fresh look shows the task still waits for a person, and a Stop is
        only real when the ledger has it. A decision recorded with no signal at all is therefore
        found at the next timer. `began` is when the step that found the task waiting returned: the
        90 days count from there, so the hold notes written meanwhile do not delay the expiry."""
        working = timedelta(days=plan.working_days)

        def clock_for(seen_view: StepView, since: datetime) -> WaitClock:
            waited = timedelta(seconds=seen_view.waited_seconds or 0)
            return WaitClock.start(since, waited, working, plan.warn_days)

        tid = task.task_id
        clock = clock_for(view, began)
        round_ = view.round
        while True:
            if await self._stop_found(ref):
                return await self._step(names.CANCEL_FOR_STOP, task)
            at, _ = clock.due()
            delay = (at - workflow.now()).total_seconds()
            if delay > 0:

                def woken(mark: int = seen) -> bool:
                    return self._count(tid) > mark

                try:
                    await workflow.wait_condition(woken, timeout=timedelta(seconds=delay))
                except TimeoutError:
                    pass
            woke = self._count(tid) > seen
            if woke:
                seen = self._count(tid)
            elif workflow.now() < at:
                continue
            if await self._stop_found(ref):
                continue  # cancelled at the top of the loop
            async with slots:
                view = await self._step(names.RUN_TASK, task)  # the database, before any action
            if not (view.status == TaskStatus.VERIFYING.value and view.waiting):
                return view  # a decision was applied, or the task ended some other way
            if view.round != round_:  # a rejection: a new attempt, a new clock
                round_ = view.round
                clock = clock_for(view, workflow.now())
                continue
            if woke:
                continue  # still waiting for a person (another human gate is open)
            lead = clock.fire(workflow.now())
            if lead is None:
                result = await self._step(names.EXPIRE_HUMAN_WAIT, task)
                if result.not_due:
                    # the stored deadline is later than the timer thought (a clock step): count
                    # the rest from the database's deadline; leads still ahead are kept
                    left = timedelta(seconds=result.due_in_seconds or 0)
                    clock = WaitClock(
                        deadline=workflow.now() + left,
                        leads=[d for d in plan.warn_days if timedelta(days=d) < left],
                    )
                    continue
                if result.status == TaskStatus.VERIFYING.value and not result.waiting:
                    continue  # an answer arrived meanwhile: the next pass applies it
                return result
            warn: WarnView = await self._activity(
                names.WARN_HUMAN_WAIT, WarnRef(ref.run_id, ref.org_id, tid, lead), WarnView
            )
            if not warn.written and warn.opens_in_seconds is not None:
                # the timer ran ahead of the store clock: count from the stored window and keep
                # this lead (and the later ones) for when it opens (spec 8, P08)
                opens = workflow.now() + timedelta(seconds=warn.opens_in_seconds)
                clock = WaitClock(deadline=opens + timedelta(days=lead), leads=[lead, *clock.leads])
