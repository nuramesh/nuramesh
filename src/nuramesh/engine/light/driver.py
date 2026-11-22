"""One pass over a durable run (R1.20 PR A): the step loop of the light engine.

A pass is stateless. It reads the database before it acts, drives every task that can move with the
same per-task entry points the Temporal activities call (`run_task_step`, `cancel_for_dependency`,
`wait_for_dependency`, `cancel_for_stop`, `warn_human_wait`, `expire_human_wait`, `finish_run`; each
is idempotent by `step_key`), and ends in one of three ways:

- the run is over (`done`): every task settled and `run.ended` written, or the run had ended;
- a task waits for a person: the job parks (`waiting_human`) until the next timer of its
  `WaitClock` (a warning at each lead time, then the expiry), or until a wake;
- nothing can move now (an organization halted, a step that failed): the job is tried again later.

The 90-day arithmetic is `WaitClock`'s and nothing else's. A wake, a timer and a lease are hints:
what is written is decided by a fresh read of the store.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.tasks import TaskStatus
from nuramesh.engine.content import WORKING_DAYS
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.run import RunEngine, TaskStepResult
from nuramesh.engine.steps import HUMAN_WAIT_WARNING, Settled, blocking_dependency
from nuramesh.engine.waits import WaitClock
from nuramesh.stores import Store
from nuramesh.stores.run_queue import JOB_DONE, JOB_READY, JOB_WAITING

_LOG = logging.getLogger(__name__)

EngineFactory = Callable[[str, str], RunEngine]
"""Builds a **fresh** engine for an organization id and a run id (the engine keeps per-run state,
as in the Temporal activities): store, workers, content service, policies."""

_ENDED = (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.NEEDS_HUMAN, TaskStatus.BLOCKED)
"""A task in one of these will not move again in this run."""


@dataclass(frozen=True)
class PassResult:
    """How a pass ended. `state` None keeps the job's state; `due_at` None means now."""

    state: str | None
    due_at: datetime | None = None
    interrupted: bool = False
    """The pass stopped at a step boundary because the runner is shutting down or lost the lead."""


class StepFailed(Exception):  # noqa: N818 (a signal to the runner)
    """A step raised an engine-side error (the store is down, a bug). The pass ends and the
    runner tries the job again later; on the last try the engine itself puts the task in front
    of a human (`run_task_step(last_try=True)`)."""

    def __init__(self, task_id: str, cause: BaseException) -> None:
        super().__init__(type(cause).__name__)
        self.task_id = task_id
        self.cause = cause


def dependents_of(order: tuple[str, ...], depends_on: Any, tid: str) -> list[str]:
    """Every task that depends on `tid`, directly or through others (in plan order)."""
    found = {tid}
    grew = True
    while grew:
        grew = False
        for other in order:
            if other not in found and any(d in found for d in depends_on.get(other, ())):
                found.add(other)
                grew = True
    return [t for t in order if t in found and t != tid]


def _denial(store: Store, org_id: str, eng: Any) -> Any:
    return deny_checks(
        store,
        org_id,
        content=getattr(eng, "content", None),
        hashing=(lambda: eng.hashing) if getattr(eng, "hashing", None) is not None else None,
    )


class _Pass:
    def __init__(
        self,
        factory: EngineFactory,
        store: Store,
        org_id: str,
        run_id: str,
        *,
        should_stop: Callable[[], bool],
        tries: dict[tuple[str, str, str], int],
        max_tries: int,
        recheck: timedelta,
    ) -> None:
        self.factory, self.store = factory, store
        self.org_id, self.run_id = org_id, run_id
        self.should_stop, self.tries = should_stop, tries
        self.max_tries, self.recheck = max_tries, recheck
        self.stop_acted = False
        self.stop_seen = False

    def eng(self) -> Any:
        return self.factory(self.org_id, self.run_id)

    def ended(self, tid: str) -> Settled | None:
        stored = self.store.get_task(tid)
        if stored is not None and stored.task.status in _ENDED:
            return Settled(stored.task.status, False)
        return None

    def stopping(self) -> bool:
        if not self.stop_seen:
            self.stop_seen = bool(self.eng().stop_requested(self.run_id))
        return self.stop_seen

    def step(self, tid: str) -> TaskStepResult:
        key = (self.org_id, self.run_id, tid)
        last = self.tries.get(key, 0) >= self.max_tries - 1
        try:
            result: TaskStepResult = self.eng().run_task_step(self.run_id, tid, last_try=last)
        except Exception as exc:
            self.tries[key] = self.tries.get(key, 0) + 1
            raise StepFailed(tid, exc) from exc
        self.tries.pop(key, None)
        return result

    def hold(self, order: tuple[str, ...], deps: Any, tid: str) -> None:
        """Note, once each, that tasks depending on `tid` wait for its human answer (R1.20a)."""
        for held in dependents_of(order, deps, tid):
            self.eng().wait_for_dependency(
                self.run_id, held, tid, Settled(TaskStatus.VERIFYING, True)
            )

    def timer(self, tid: str, result: TaskStepResult) -> tuple[datetime | None, bool]:
        """The next timer of a task waiting for a person: `(due, acted)`. Fires every timer that
        is due now through `WaitClock` (a warning, or the expiry); the store is read again after
        each, so a decision that arrived meanwhile wins."""
        eng = self.eng()
        working = timedelta(days=WORKING_DAYS)
        acted = False
        spent: set[int] = set()  # leads the engine refused for good in this call: never asked twice
        while True:
            now = self.store.now()
            since = result.waiting_since or now
            warned = {
                e.payload.get("days_left")
                for e in self.store.task_events(self.run_id, tid)
                if e.payload.get("kind") == HUMAN_WAIT_WARNING
                and e.payload.get("attempt") == result.attempt
            }
            leads = [
                d for d in eng.settings.human_wait_warn_days if d not in warned and d not in spent
            ]
            clock = WaitClock.start(now, max(timedelta(0), now - since), working, leads)
            at, _ = clock.due()
            if now < at:
                return at, acted
            lead = clock.fire(now)
            if lead is None:
                stopped = eng.expire_human_wait(self.run_id, tid)
                if stopped.not_due:  # the engine's stored deadline is later: it decides (spec 8)
                    return stopped.due_at or now + self.recheck, acted  # nothing was written
                return None, True
            warn = eng.warn_human_wait_result(self.run_id, tid, lead)
            if not warn.written:
                if warn.opens_at is not None and warn.opens_at > now:
                    # the timer ran ahead of the store clock: wait for the stored window (spec 8)
                    return warn.opens_at, acted
                spent.add(lead)  # answered, over or already warned: nothing more to ask
            else:
                acted = True


def run_pass(
    factory: EngineFactory,
    store: Store,
    org_id: str,
    run_id: str,
    *,
    should_stop: Callable[[], bool],
    tries: dict[tuple[str, str, str], int],
    max_tries: int = 5,
    recheck: timedelta = timedelta(seconds=30),
) -> PassResult:
    """One pass over the run (module docstring). Raises `StepFailed` for an engine-side failure."""
    p = _Pass(
        factory, store, org_id, run_id,
        should_stop=should_stop, tries=tries, max_tries=max_tries, recheck=recheck,
    )  # fmt: skip
    eng = p.eng()
    denial = _denial(store, org_id, eng)
    if denial is not None:
        if denial.kind in ("erased", "key_lost"):
            return PassResult(JOB_DONE)  # E17: nothing can run in this organization again
        return PassResult(None, store.now() + recheck)  # E15: halted (or unsure): the job stays
    plan = eng.stored_plan(run_id)
    if plan.ended:
        return PassResult(JOB_DONE)  # E22
    order, deps = plan.tasks, plan.depends_on
    settled: dict[str, Settled] = {}
    waiting: dict[str, TaskStepResult] = {}
    progress = True
    while progress:
        progress = False
        for tid in order:
            if tid in settled or tid in waiting:
                continue
            if should_stop():
                return PassResult(None, interrupted=True)
            done = p.ended(tid)
            if done is not None:
                settled[tid] = done
                progress = True
                continue
            task_deps = deps.get(tid, ())
            if p.stopping():
                result = p.eng().cancel_for_stop(run_id, tid)
                p.stop_acted = True
                settled[tid] = result.settled
                progress = True
                continue
            known = {d: settled[d] for d in task_deps if d in settled}
            bad = blocking_dependency(known)
            if bad is not None:
                result = p.eng().cancel_for_dependency(run_id, tid, bad, known[bad])
                settled[tid] = result.settled
                progress = True
                continue
            if any(d not in settled for d in task_deps):
                continue  # a dependency is still to run or waits for a person: held
            try:
                result = p.step(tid)
            except StepFailed as failed:
                if p.tries[(org_id, run_id, tid)] < max_tries:
                    raise
                # even the last try (which hands the task to a human) raised: end the run
                p.eng().finish_run(run_id, stop_acted=False, aborted=type(failed.cause).__name__)
                return PassResult(JOB_DONE)
            p.stop_acted = p.stop_acted or result.stopped
            if result.awaits_human:
                waiting[tid] = result
                p.hold(order, deps, tid)
            else:
                settled[tid] = result.settled
            progress = True
    if waiting:
        due: list[datetime] = []
        for tid, result in waiting.items():
            at, acted = p.timer(tid, result)
            if acted:
                return PassResult(JOB_READY, store.now())  # settle what the timer changed
            assert at is not None
            due.append(at)
        return PassResult(JOB_WAITING, min(due))
    if len(settled) < len(order):  # nothing can move and nothing waits: look again later
        _LOG.warning("light run %s has tasks that cannot move", run_id)
        return PassResult(JOB_READY, store.now() + recheck)
    stopped = p.stop_acted or (
        p.stop_seen and any(s.status is TaskStatus.CANCELLED for s in settled.values())
    )
    p.eng().finish_run(run_id, stop_acted=stopped)
    return PassResult(JOB_DONE)
