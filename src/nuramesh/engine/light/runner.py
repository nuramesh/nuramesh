"""The runner of the light engine (R1.20 PR A, ND347 section 5, as revised).

One scheduler plus a bounded worker pool, in a **separate process** from the API. Truth is the
store and the ledger; the runner keeps a cache only.

- **Leader lock.** One runner per database executes: the holder of the `light` leader lease (a row
  with an owner, an epoch and a time; the owner is a random id, never a host name or a process
  id). A second runner is `standby`: it serves nothing and builds no engine. A leader that stops
  beating loses the lease after `lease` (60 s); a standby then takes over and frees the old
  leader's job leases, and the old leader's late writes are refused by the job epoch.
- **Start requests.** The leader claims a start request atomically (`claim_start_request`), has the
  `RunBuilder` build the engine and plan from the team's code, and calls `begin_run` once; a
  request whose claimer died after `begin_run` finds the run and only schedules it.
- **Jobs.** Due jobs are leased (`claim_light_jobs`, epoch +1) up to the free worker slots and
  passed to `run_pass`. A parked job is due again at its next `WaitClock` timer or on a wake.
- **Real models on (R6).** A runner started without `--fake` writes one
  `runner.real_models_on` to the stream of each organization it serves, once per leadership term
  and before the first claim of that organization's request; if the entry cannot be written it
  claims nothing of that organization. A `--fake` runner writes none.
- **Shutdown.** Stop taking work, let steps finish for `grace` seconds, release the leases of the
  rest (they are re-run on the next start, idempotently), give up the lead.
"""

import logging
import math
import secrets
import signal
import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from nuramesh.core.ledger import EventType
from nuramesh.engine.admission import RunnerAdmission
from nuramesh.engine.content import ContentService
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.light.begin import (
    ClaimLost,
    PreparedRun,
    RunBuilder,
    StartRefused,
    begin_from_request,
    extend_claimed_plan_text,
    org_halted,
)
from nuramesh.engine.light.driver import EngineFactory, PassResult, StepFailed, run_pass
from nuramesh.engine.light.starter import ENGINE
from nuramesh.engine.org_ledger import org_stream
from nuramesh.engine.run_engine import refuse_light_when_hosted
from nuramesh.env_alias import read_renamed
from nuramesh.stores import ConcurrencyError, EventDraft, FenceLost, Store, UnitOfWork, fenced
from nuramesh.stores.run_queue import LightJobRow, StartRequestRow

_LOG = logging.getLogger(__name__)

MAX_WORKERS = 32
INTAKE_PER_TICK = 16


class LightConfigError(ValueError):
    """A setting is out of range. The CLI turns it into exit code 2 before anything starts."""


@dataclass(frozen=True)
class LightSettings:
    workers: int = 4
    """Parallel runs (`NURAMESH_LIGHT_WORKERS`, 1 to 32)."""
    lease: timedelta = timedelta(seconds=60)
    """How long a leader, job or request lease lasts without a renewal."""
    grace: float = 30.0
    """Seconds a shutdown waits for steps in flight (`NURAMESH_SHUTDOWN_GRACE_SECONDS`)."""
    poll: float = 0.5
    """Seconds between scheduler ticks: a timer is late by at most this much."""
    leader: str = "light"
    max_tries: int = 5
    retry_after: timedelta = timedelta(seconds=2)
    """A failed step is tried again after `retry_after` times the number of failures."""
    recheck: timedelta = timedelta(seconds=30)
    """How soon a job of a halted organization looks again."""
    start_ttl: timedelta = timedelta(hours=1)
    """A start request not picked up within this long ends `refused`
    (`NURAMESH_RUN_START_TTL_SECONDS`)."""

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= MAX_WORKERS:
            raise LightConfigError(f"workers must be between 1 and {MAX_WORKERS}")
        if not (math.isfinite(self.grace) and self.grace >= 0):
            raise LightConfigError("the shutdown grace must be a number of seconds, 0 or more")
        if self.start_ttl <= timedelta(0):
            raise LightConfigError("the start request TTL must be above 0")
        if not (math.isfinite(self.poll) and self.poll > 0):
            raise LightConfigError("the poll interval must be above 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "LightSettings":
        """Read the `NURAMESH_LIGHT_WORKERS`, `_SHUTDOWN_GRACE_SECONDS` and `_RUN_START_TTL_SECONDS`
        settings; a bad value raises."""
        kw: dict[str, Any] = {}
        if "NURAMESH_LIGHT_WORKERS" in environ:
            try:
                kw["workers"] = int(environ["NURAMESH_LIGHT_WORKERS"])
            except ValueError:
                raise LightConfigError("NURAMESH_LIGHT_WORKERS must be a whole number") from None
        ttl = read_renamed(environ, "NURAMESH_RUN_START_TTL_SECONDS", "NURAMESH_RUN_START_TTL")
        if ttl is not None:
            try:
                kw["start_ttl"] = timedelta(seconds=float(ttl[1]))
            except (ValueError, OverflowError):
                raise LightConfigError(f"{ttl[0]} must be seconds") from None
        grace = read_renamed(environ, "NURAMESH_SHUTDOWN_GRACE_SECONDS", "NURAMESH_SHUTDOWN_GRACE")
        if grace is not None:
            try:
                kw["grace"] = float(grace[1])
            except ValueError:
                raise LightConfigError(f"{grace[0]} must be a number") from None
        return cls(**kw)


@dataclass(frozen=True)
class TickReport:
    leader: bool
    """This runner holds the lead (a standby does nothing)."""
    busy: bool
    """Something was started, claimed or run in this tick."""


class LightRunner:
    """Runs the light engine (module docstring)."""

    def __init__(
        self,
        store: Store,
        factory: EngineFactory,
        *,
        builder: RunBuilder | None = None,
        content: ContentService | None = None,
        settings: LightSettings | None = None,
        owner: str | None = None,
        admission: RunnerAdmission | None = None,
    ) -> None:
        refuse_light_when_hosted()
        self.admission = admission or RunnerAdmission()  # the default: fake-only, no sandbox
        if self.admission.engine != ENGINE:
            raise LightConfigError("the light runner needs a light admission")
        self.store = store
        self.factory = factory
        self.builder = builder
        self.content = content
        self.settings = settings or LightSettings()
        self.owner = owner or secrets.token_hex(16)
        self._pool = ThreadPoolExecutor(
            max_workers=self.settings.workers, thread_name_prefix="nuramesh-light"
        )
        self._stopping = threading.Event()
        self._fenced = threading.Event()
        self._tick_lock = threading.Lock()
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str], tuple[LightJobRow, Future[None]]] = {}
        self._tries: dict[tuple[str, str, str], int] = {}
        self._epoch = 0
        self._announced: tuple[int, set[str]] = (0, set())  # R6: the term and its organizations
        self._role = "standby"
        self._thread: threading.Thread | None = None

    @property
    def role(self) -> str:
        """`leader`, `standby` or `stopped` (for `GET /v1/health`'s `light_engine`)."""
        return "stopped" if self._stopping.is_set() else self._role

    # -- one scheduler tick ---------------------------------------------------------------------

    def tick(self, wait_for_passes: bool = True) -> TickReport:
        """Beat, take start requests, lease due jobs and run their passes. With
        `wait_for_passes` the call returns when the passes it started are over."""
        if self._stopping.is_set():
            return TickReport(False, False)
        with self._tick_lock:
            if not self._lead():
                return TickReport(False, False)
            busy = self._renew()
            for n in range(INTAKE_PER_TICK):
                if self._stopping.is_set():
                    break
                if n and not self._keep_lead():  # a slow build must not let the leases lapse
                    return TickReport(False, False)
                if not self._intake():
                    break
                busy = True
            futures = self._dispatch()
            busy = busy or bool(futures)
        if wait_for_passes:
            wait(futures)
        return TickReport(True, busy)

    def _keep_lead(self) -> bool:
        """Beat again between two intakes: the leader lease and the leases of the passes in
        flight (`_intake` builds and begins a run on this thread, which can take a while)."""
        if not self._lead():
            return False
        self._renew()
        return True

    def _lead(self) -> bool:
        with self.store.transaction() as uow:
            epoch = uow.take_leader(self.settings.leader, self.owner, self.settings.lease)
            if epoch is not None and epoch != self._epoch:
                uow.free_light_leases(self.owner)  # the old leader is gone; its writes will fail
        if epoch is None:
            if self._role == "leader":
                self._fenced.set()  # lost the lead: running passes stop at the next step
            self._role = "standby"
            return False
        if epoch != self._epoch:
            self._epoch = epoch
        self._fenced.clear()
        self._role = "leader"
        return True

    def _renew(self) -> bool:
        with self._lock:
            held = [job for job, fut in self._inflight.values() if not fut.done()]
        for job in held:
            with self.store.transaction() as uow:
                uow.renew_light_job(
                    job.org_id, job.run_id, self.owner, job.epoch, self.settings.lease
                )
        return bool(held)

    def _dispatch(self) -> list[Future[None]]:
        with self._lock:
            free = self.settings.workers - sum(
                1 for _, f in self._inflight.values() if not f.done()
            )
            self._inflight = {k: v for k, v in self._inflight.items() if not v[1].done()}
        if free <= 0:
            return []
        with self.store.transaction() as uow:
            jobs = uow.claim_light_jobs(self.owner, self.settings.lease, free)
        futures: list[Future[None]] = []
        for job in jobs:
            with self._lock:  # registered before a shutdown can look
                future: Future[None] = self._pool.submit(self._work, job)
                self._inflight[(job.org_id, job.run_id)] = (job, future)
            futures.append(future)
        return futures

    # -- a pass -----------------------------------------------------------------------------------

    def _work(self, job: LightJobRow) -> None:
        try:

            def fence(uow: UnitOfWork) -> None:
                if not uow.light_job_held(job.org_id, job.run_id, self.owner, job.epoch):
                    raise FenceLost

            try:
                with fenced(fence):
                    result = run_pass(
                        self.factory,
                        self.store,
                        job.org_id,
                        job.run_id,
                        should_stop=lambda: self._stopping.is_set() or self._fenced.is_set(),
                        tries=self._tries,
                        max_tries=self.settings.max_tries,
                        recheck=self.settings.recheck,
                    )
            except FenceLost:  # E25: the lease is gone; the new holder decides what happens
                _LOG.warning("run %s: the lease was lost; abandoned", job.run_id)
                return
            except StepFailed as failed:
                _LOG.warning("a step of run %s failed: %s", job.run_id, type(failed.cause).__name__)
                n = self._tries.get((job.org_id, job.run_id, failed.task_id), 1)
                result = PassResult(None, self.store.now() + self.settings.retry_after * n)
            self._settle(job, result)
        except Exception as exc:  # the lease runs out and the job is claimed again
            _LOG.warning("a pass over run %s failed: %s", job.run_id, type(exc).__name__)

    def _settle(self, job: LightJobRow, result: PassResult) -> None:
        with self.store.transaction() as uow:
            uow.settle_light_job(
                job.org_id,
                job.run_id,
                self.owner,
                job.epoch,
                result.state or job.state,
                result.due_at,
            )

    # -- start requests ---------------------------------------------------------------------------

    def _intake(self) -> bool:
        if self.builder is None:
            return False
        with self.store.transaction() as uow:
            orgs = uow.claimable_start_orgs(ENGINE)
        # the halt check comes before the claim: a halted organization's request stays pending
        halted = frozenset(o for o in orgs if org_halted(self.store, o))
        skip = halted | self._announce(o for o in orgs if o not in halted)
        with self.store.transaction() as uow:
            row = uow.claim_start_request(self.owner, ENGINE, self.settings.lease, skip_orgs=skip)
            if row is not None and self.content is not None:
                extend_claimed_plan_text(self.content, uow, row, self.store.now())
        if row is None:
            return False
        if not self._entry_ready(row.org_id):
            # R6: the org's first request appeared after the list read above and its entry cannot
            # be written: give the claim back, begin nothing (the next tick tries again)
            with self.store.transaction() as uow:
                uow.release_start_request(row.org_id, row.run_id, self.owner)
            return False
        beat = threading.Event()
        pulse = threading.Thread(
            target=self._pulse, args=(row, beat), name="nuramesh-light-claim", daemon=True
        )
        pulse.start()
        try:
            begin_from_request(
                self.store,
                self.builder,
                row,
                self.owner,
                start_ttl=self.settings.start_ttl,
                recheck=self.settings.recheck,
                on_started=lambda uow, r: uow.put_light_job(r.org_id, r.run_id),
                keep=self._keep_claim,
                content=self.content,
                admission=self.admission,
            )
        except ConcurrencyError:  # another runner began the run first (E30): back off
            with self.store.transaction() as uow:
                uow.release_start_request(row.org_id, row.run_id, self.owner)
        except ClaimLost:
            pass
        except Exception as exc:  # transient: the claim runs out and the request is taken again
            _LOG.warning("start request for run %s failed: %s", row.run_id, type(exc).__name__)
        finally:
            beat.set()
            pulse.join(timeout=5)
        return True

    def _entry_ready(self, org_id: str) -> bool:
        """True when `org_id` has its R6 entry in this term (or needs none)."""
        if not self.admission.real_capable:
            return True
        if self._announced[0] == self._epoch and org_id in self._announced[1]:
            return True
        if org_id in self._announce([org_id]):
            return False
        return True

    def _announce(self, orgs: Iterable[str]) -> frozenset[str]:
        """R6: before the first claim of an organization's request in this
        leadership term, a real-capable runner writes `runner.real_models_on` to that
        organization's stream. Returns the organizations of which nothing may be claimed now: the
        entry could not be written, or the organization's state could not be read. An erased or
        key-lost organization gets no entry (its stream is closed) and is not held back: its
        request is claimed and ends in the existing refusal, which writes no entry either."""
        if not self.admission.real_capable:
            return frozenset()  # a `--fake` runner writes no such entry
        if self._announced[0] != self._epoch:
            self._announced = (self._epoch, set())
        done = self._announced[1]
        held: set[str] = set()
        for org_id in orgs:
            if org_id in done:
                continue
            denial = deny_checks(self.store, org_id, content=self.content)
            if denial is not None and denial.kind in ("erased", "key_lost"):
                continue
            if denial is not None:  # unsure, or halted since the look above: wait, write nothing
                held.add(org_id)
                continue
            try:
                with self.store.transaction() as uow:
                    uow.append(
                        EventDraft(
                            stream=org_stream(org_id),
                            type=EventType.RUNNER_REAL_MODELS_ON,
                            actor="system",
                            payload={"engine": ENGINE, "sandbox": self.admission.sandbox},
                        )
                    )
            except Exception as exc:  # no entry, no claim (L02); the next tick tries again
                _LOG.warning("runner.real_models_on could not be written: %s", type(exc).__name__)
                held.add(org_id)
                continue
            done.add(org_id)
        return frozenset(held)

    def _keep_claim(self, row: StartRequestRow) -> None:
        """Renew the claim on the request being begun; `ClaimLost` if someone else holds it now."""
        with self.store.transaction() as uow:
            if not uow.hold_start_request(row.org_id, row.run_id, self.owner, self.settings.lease):
                raise ClaimLost

    def _pulse(self, row: StartRequestRow, done: threading.Event) -> None:
        """While one intake builds and begins (it can take over a minute), renew its claim every
        third of the lease, so a standby never takes the request while this runner is on it."""
        every = max(0.05, self.settings.lease.total_seconds() / 3)
        while not done.wait(every):
            try:
                with self.store.transaction() as uow:
                    if not uow.hold_start_request(
                        row.org_id, row.run_id, self.owner, self.settings.lease
                    ):
                        return
            except Exception as exc:  # the next beat tries again
                _LOG.warning("the claim renewal failed: %s", type(exc).__name__)

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self) -> None:
        """Run the scheduler in a background thread until `shutdown`."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="nuramesh-light-scheduler", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick(wait_for_passes=False)
            except Exception as exc:
                _LOG.warning("a scheduler tick failed: %s", type(exc).__name__)
            self._stopping.wait(self.settings.poll)

    def shutdown(self, grace: float | None = None) -> bool:
        """Stop taking work, wait up to `grace` seconds for steps in flight, release the leases of
        the rest and give up the lead. True if everything drained."""
        self._stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._lock:
            pending = list(self._inflight.values())
        limit = self.settings.grace if grace is None else grace
        _, not_done = wait([f for _, f in pending], timeout=limit)
        try:
            for job, fut in pending:
                if not fut.done():  # re-run on the next start; the step is idempotent
                    self._settle(job, PassResult(None, interrupted=True))
            with self.store.transaction() as uow:
                uow.release_leader(self.settings.leader, self.owner)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
        return not not_done

    def serve(self) -> int:
        """Run until SIGTERM or SIGINT, then shut down within the grace period; the exit code."""
        stop = threading.Event()
        previous = {
            sig: signal.signal(sig, lambda *_: stop.set())
            for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            self.start()
            while not stop.wait(0.2):  # a timed wait, so the signal handler gets to run
                pass
            self.shutdown()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0


__all__ = [
    "EngineFactory",
    "LightConfigError",
    "LightRunner",
    "LightSettings",
    "PreparedRun",
    "RunBuilder",
    "StartRefused",
    "TickReport",
]
