"""The server tick: evaluate the circuit breakers on a timer, and fail closed (E19, ND106,
`spec/e19/README.md` section 9).

`BreakerTicker.tick_once()` is one cycle and never sleeps (tests call it with a fake clock and
injected faults); `run()` is the loop the app lifespan starts. A cycle runs one evaluation on a
worker thread under a deadline (`max(3 * tick_seconds, 30)` seconds on the monotonic clock). A
**failed tick** is any `Exception` from the `read`, `evaluate` or `write` phase, or a tick that
does not finish in time (phase `timeout`); `halt_after` consecutive failed ticks halt the
organization (`cause: evaluation_unavailable`, with the diagnosis) when a halting rule exists.

A hung thread cannot be killed: it is abandoned (its late result is discarded, its transactional
writes stand) and at most one tick thread and one halt-write thread are in flight; while one is,
a cycle starts no new thread and counts as another failed tick.
"""

import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import anyio
from anyio import to_thread

from nuramesh.core.breakers import BreakerRule, check_rules, halting_rules
from nuramesh.core.org import OrgGraph
from nuramesh.engine.halt_refusal import flush_due_pending, halt_window_seconds
from nuramesh.engine.killswitch import (
    EvaluationFailure,
    EvaluationResult,
    evaluate_breakers_detailed,
    halt_by_breaker,
)
from nuramesh.stores.base import Store

_LOG = logging.getLogger(__name__)
SHUTDOWN_WAIT_SECONDS = 5.0

Waiter = Callable[[threading.Event, float], bool]
Sleeper = Callable[[float], Awaitable[None]]


class TickTimeout(Exception):  # noqa: N818 (the name is the diagnosis written to the ledger)
    """A tick did not finish within its deadline."""


def _default_wait(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


class _Job:
    """One piece of work on its own thread, with a handle that outlives its deadline."""

    def __init__(self, work: Callable[[], Any], name: str) -> None:
        self.finished = threading.Event()
        self.abandoned = False
        self.result: Any = None
        self.error: BaseException | None = None
        self.lock = threading.Lock()
        self._work = work
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def _run(self) -> None:
        try:
            result = self._work()
            with self.lock:
                self.result = result
        except BaseException as exc:  # the cycle decides; a thread never raises
            with self.lock:
                self.error = exc
        finally:
            self.finished.set()

    def start(self) -> "_Job":
        self.thread.start()
        return self


@dataclass(frozen=True)
class TickOutcome:
    """What one cycle did."""

    ok: bool
    failure: EvaluationFailure | None
    result: EvaluationResult | None
    halt_attempted: bool = False


class BreakerTicker:
    """Evaluates `rules` for one organization, one cycle at a time (module docstring)."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        rules: Sequence[BreakerRule],
        *,
        tick_seconds: int = 10,
        halt_after: int = 3,
        clock: Callable[[], datetime] | None = None,
        org: OrgGraph | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Waiter = _default_wait,
        halt_refusal_window: int | None = None,
    ) -> None:
        self.store, self.org_id, self.org = store, org_id, org
        self.rules = check_rules(rules)
        self.tick_seconds, self.halt_after = tick_seconds, halt_after
        self._clock = clock or store.now
        self._monotonic, self._wait = monotonic, wait
        self.deadline_seconds = max(3 * tick_seconds, 30)
        self.halt_refusal_window = (
            halt_window_seconds() if halt_refusal_window is None else halt_refusal_window
        )
        self.consecutive_failures = 0
        self.last_evaluated_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error_phase: str | None = None
        self.last_error_class: str | None = None
        self.last_failing_rule: str | None = None
        self._last_finished: datetime | None = None
        self._started: datetime | None = None
        self._tick_job: _Job | None = None
        self._halt_job: _Job | None = None
        self._flush_job: _Job | None = None
        self.ticks_started = 0  # tick threads started (diagnostics, tests)
        self._cycle = threading.Lock()  # ticks never overlap within a process

    # -- status ------------------------------------------------------------------------------

    def state(self, now: datetime | None = None) -> str:
        """`not_configured`, `starting`, `failing` or `ok` (spec 9.6). `failing` when a tick has
        failed since the last good one, or no tick has finished for more than the deadline."""
        if not self.rules:
            return "not_configured"
        at = now or self._clock()
        if self.consecutive_failures > 0:
            return "failing"
        reference = self._last_finished or self._started
        if reference is not None and (at - reference).total_seconds() > self.deadline_seconds:
            return "failing"
        return "starting" if self._last_finished is None else "ok"

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        """The `evaluation` object of `GET /v1/org/breakers`."""

        def iso(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "state": self.state(now),
            "tick_seconds": self.tick_seconds,
            "halt_after_failed_ticks": self.halt_after,
            "last_evaluated_at": iso(self.last_evaluated_at),
            "last_error_at": iso(self.last_error_at),
            "last_error_phase": self.last_error_phase,
            "last_error_class": self.last_error_class,
            "last_failing_rule": self.last_failing_rule,
            "consecutive_failures": self.consecutive_failures,
        }

    @property
    def in_flight(self) -> bool:
        """Whether a tick thread is still running (a hung tick)."""
        return self._tick_job is not None and not self._tick_job.finished.is_set()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait for the tick thread and the halt-write thread to end (tests, shutdown)."""
        for job in (self._tick_job, self._halt_job, self._flush_job):
            if job is not None and not job.finished.wait(timeout):
                return False
        return True

    # -- one cycle ---------------------------------------------------------------------------

    def tick_once(self) -> TickOutcome:
        """One cycle (spec 9.2, 9.5). Never sleeps beyond the deadline; never raises for an
        `Exception`."""
        with self._cycle:
            now = self._clock()
            if self._started is None:
                self._started = now
            began = self._monotonic()  # the flush and the evaluation share ONE deadline
            stuck = self._flush_halt_refusals(now)
            if stuck is not None:  # the flush hung: a failed tick (fail closed), like a hung tick
                return self._failed(stuck, None)
            if not self.rules:
                return TickOutcome(True, None, None)
            if self.in_flight:  # a hung thread: no second one, another failed tick
                return self._failed(
                    EvaluationFailure("timeout", "TickTimeout", None, TickTimeout()), None
                )
            job = _Job(
                lambda: evaluate_breakers_detailed(
                    self.store, self.org_id, self.rules, now=now, org=self.org
                ),
                "breaker-tick",
            ).start()
            self._tick_job = job
            self.ticks_started += 1
            left = max(0.0, self.deadline_seconds - (self._monotonic() - began))
            finished = self._wait(job.finished, left)
            with job.lock:
                if not job.finished.is_set():
                    job.abandoned = True
                    finished = False
            if not finished:
                return self._failed(
                    EvaluationFailure("timeout", "TickTimeout", None, TickTimeout()), None
                )
            error = job.error
            if error is not None:
                if not isinstance(error, Exception):
                    raise error  # cancellation, KeyboardInterrupt: not a failed tick
                failure = EvaluationFailure("evaluate", type(error).__name__, None, error)
                return self._failed(failure, None)
            result: EvaluationResult = job.result
            if result.failure is not None:
                return self._failed(result.failure, result)
            self.consecutive_failures = 0
            self._last_finished = self.last_evaluated_at = self._clock()
            return TickOutcome(True, None, result)

    def _flush_halt_refusals(self, now: datetime) -> EvaluationFailure | None:
        """T154 R5d: a pending count of halt refusals that is due enters the ledger even when no
        later refusal comes. It runs on its own thread under the tick deadline (at most one in
        flight): a store call that hangs is a failed tick (E19 9.2), so the breakers' fail-closed
        halt still fires. A store error here is only logged."""
        if self._flush_job is not None and not self._flush_job.finished.is_set():
            return EvaluationFailure("timeout", "TickTimeout", None, TickTimeout())
        job = _Job(
            lambda: flush_due_pending(self.store, self.org_id, now, self.halt_refusal_window),
            "halt-refusal-flush",
        ).start()
        self._flush_job = job
        finished = self._wait(job.finished, float(self.deadline_seconds))
        with job.lock:
            if not job.finished.is_set():
                job.abandoned = True
                finished = False
        if not finished:
            return EvaluationFailure("timeout", "TickTimeout", None, TickTimeout())
        error = job.error
        if error is not None:
            if not isinstance(error, Exception):
                raise error  # cancellation, KeyboardInterrupt: not a failed tick
            _LOG.warning("could not flush the halt refusals waiting for the ledger", exc_info=error)
        return None

    def _failed(self, failure: EvaluationFailure, result: EvaluationResult | None) -> TickOutcome:
        _LOG.error(
            "breaker tick failed (phase %s, %s%s)",
            failure.phase,
            failure.error_class,
            f", rule {failure.failing_rule}" if failure.failing_rule else "",
            exc_info=failure.exception if failure.phase != "timeout" else None,
        )
        self.consecutive_failures += 1
        at = self._clock()
        self.last_error_at, self._last_finished = at, at
        self.last_error_phase = failure.phase
        self.last_error_class = failure.error_class
        self.last_failing_rule = failure.failing_rule
        attempted = False
        if self.consecutive_failures >= self.halt_after:
            # the predicate is decided once, from what this tick read; a tick that never
            # finished (or an unreadable org stream) leaves the disabled state unknown
            halting = (
                result.halting if result is not None else bool(halting_rules(self.rules, None))
            )
            if halting:
                attempted = self._try_halt(failure)
        return TickOutcome(False, failure, result, halt_attempted=attempted)

    def _try_halt(self, failure: EvaluationFailure) -> bool:
        """Halt the organization with the diagnosis, on its own thread under the same deadline;
        a halt that fails or hangs is logged and retried on the next failed tick."""
        if self._halt_job is not None and not self._halt_job.finished.is_set():
            return False  # one halt-write thread in flight
        diagnosis = {
            "phase": failure.phase,
            "error_class": failure.error_class,
            "failing_rule": failure.failing_rule,
            "failed_ticks": self.consecutive_failures,
        }
        job = _Job(
            lambda: halt_by_breaker(
                self.store, self.org_id, cause="evaluation_unavailable", diagnosis=diagnosis
            ),
            "breaker-halt",
        ).start()
        self._halt_job = job
        finished = self._wait(job.finished, float(self.deadline_seconds))
        with job.lock:
            if not job.finished.is_set():
                job.abandoned = True
                finished = False
        if not finished:
            _LOG.error("the fail-closed halt did not finish in time; retrying on the next tick")
        elif job.error is not None:
            _LOG.error("the fail-closed halt failed; retrying on the next tick", exc_info=job.error)
        return True

    # -- the loop ----------------------------------------------------------------------------

    async def run(self, sleep: Sleeper = anyio.sleep) -> None:
        """Tick forever: the first tick at once, then `tick_seconds` after the previous one
        finished (a fixed delay, so ticks never overlap). Each tick runs on a worker thread so
        the event loop is never blocked. Cancelling it ends the sleep."""
        while True:
            await to_thread.run_sync(self.tick_once, abandon_on_cancel=True)
            await sleep(float(self.tick_seconds))


__all__ = ["BreakerTicker", "SHUTDOWN_WAIT_SECONDS", "TickOutcome", "TickTimeout"]
