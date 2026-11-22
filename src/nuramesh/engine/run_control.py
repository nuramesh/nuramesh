"""Stopping a run: a cooperative, ledger-based request (v0.1 Stage A).

A human asks a run to stop by appending **`run.stop_requested`** to the run's ledger stream
(`request_stop`: who, when, and optionally the keyed hash of a reason, T47). Nothing is signalled
to a process: the run engine reads the stream between steps (`StopWatch`) and ends the run
cleanly when it finds the request. That makes Stop work from any process that shares the store
(the API server, `nuramesh stop`, another terminal) and puts the request itself in the audit trail.

Rules:

- **Idempotent.** A second request while the run is still going changes nothing and returns the
  first request. The check and the append share one transaction that also checks nothing was
  appended since the stream was read, so two racing requests write one event and a request can
  never be appended after `run.ended`.
- **Finished runs refuse** (`RunFinished`); runs without a stream are `NotFoundError`.
- **No content.** The ledger holds the reason's keyed hash only; the text goes to the content store
  in the same transaction.
"""

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.engine.content import (
    KEY_DESTROYED,
    ContentError,
    ContentService,
    is_gate_refusal,
)
from nuramesh.engine.org_halt import HaltWatch
from nuramesh.stores.base import (
    EventDraft,
    NotFoundError,
    Store,
    StoredTask,
    StoreError,
    UnitOfWork,
    run_stream,
)

PAGE = 500
STOP_TYPE = EventType.RUN_STOP_REQUESTED.value
TASK_STOP_TYPE = EventType.TASK_STOP_REQUESTED.value
HALT_VIAS = ("org_halt", "org_halt_unreadable")
"""`via` of a stop request that comes from an organization halt, not from a person (R27)."""
ENDED_TYPE = EventType.RUN_ENDED.value


class RunFinished(StoreError):  # noqa: N818 (a state, said in words)
    """The run has already ended; a stop request would change nothing."""


def withheld_text(code: str | None) -> str | None:
    """The fixed words shown for a stop whose reason was withheld (API, CLI and app)."""
    if code is None:
        return None
    if code == KEY_DESTROYED:
        return "reason withheld: org key destroyed"
    return "reason withheld: org key unavailable"


@dataclass(frozen=True)
class StopRequest:
    """A recorded stop request."""

    seq: int
    by: str
    at: datetime
    via: str
    reason_hash: str | None = None
    reason_withheld: str | None = None
    """The gate code (`org_key_destroyed`, `org_key_missing`, `key_tombstone_unavailable`) when the
    reason could not be kept: Stop always works, and nothing of the reason is stored (R1.37 K6)."""

    @property
    def withheld_text(self) -> str | None:
        """What a person is shown instead of the reason, or None if it was not withheld."""
        return withheld_text(self.reason_withheld)

    @staticmethod
    def of(event: LedgerEvent) -> "StopRequest":
        """The request a `run.stop_requested` event records."""
        reason = event.payload.get("reason_hash")
        withheld = event.payload.get("reason_withheld")
        return StopRequest(
            seq=event.seq,
            by=event.actor,
            at=event.at,
            via=str(event.payload.get("via", "")),
            reason_hash=reason if isinstance(reason, str) else None,
            reason_withheld=withheld if isinstance(withheld, str) else None,
        )


@dataclass(frozen=True)
class StopState:
    """Where a run's stream stands with respect to Stop."""

    exists: bool
    head_seq: int
    request: StopRequest | None
    ended: bool
    ended_status: str | None  # the `status` of `run.ended` ("stopped"), if it says one
    user_request: StopRequest | None = None
    """The first stop request that is not an organization halt's (a person's, Ctrl-C, ...): the
    halt's request is first in the stream and does not cut calls (R27), so a later person's stop
    is recorded as its own entry and found here (review m2 of #1068)."""


def stop_state(store: Store, run_id: str, since: StopState | None = None) -> StopState:
    """Read the stream and say whether a stop was requested and whether it ended.

    With `since` (an earlier result for the same run) only the entries after it are read.
    `head_seq` is the last entry actually read (not a later head), so a writer can check that
    nothing was appended since."""
    stream = run_stream(run_id)
    request = since.request if since else None
    user = since.user_request if since else None
    ended = since.ended if since else False
    status = since.ended_status if since else None
    cursor = since.head_seq if since else 0
    while True:
        batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
        for event in batch:
            if event.type == STOP_TYPE:
                found = StopRequest.of(event)
                request = request or found
                if user is None and found.via not in HALT_VIAS:
                    user = found
            elif event.type == ENDED_TYPE:
                ended = True
                value = event.payload.get("status")
                status = value if isinstance(value, str) else None
        if batch:
            cursor = batch[-1].seq
        if len(batch) < PAGE:
            return StopState(cursor > 0, cursor, request, ended, status, user)


@dataclass(frozen=True)
class StopOutcome:
    """What `request_stop` did: the request in force, and whether it was already there."""

    request: StopRequest
    already_requested: bool


def takes_part(tasks: Iterable[StoredTask], agent_id: str) -> bool:
    """Whether an agent takes part in a run: it is the current assignee of at least one of the
    run's tasks, whatever the task's status (E2 spec 6.2.2). A pure verifier is not an assignee."""
    return any(t.task.assignee == agent_id for t in tasks)


def request_stop(
    store: Store,
    run_id: str,
    *,
    actor: str,
    via: str,
    reason: str | None = None,
    content: ContentService | None = None,
    org_id: str | None = None,
    recheck: Callable[[UnitOfWork], None] | None = None,
) -> StopOutcome:
    """Record that `actor` asked the run to stop (module docstring).

    Raises `NotFoundError` (no such run), `RunFinished`, or `ValueError` for a reason without a
    content store. Whether `actor` is allowed to is the caller's job (the API requires a human).

    `recheck(uow)`, if given, runs inside the write transaction after the writer lock is held and
    the stream has been read, as the last read before the write; whatever it raises aborts the
    request with nothing written (E2 spec 8.1: an agent's credential, suspension and
    participation are checked again here).
    """
    text = (reason or "").strip()
    if text and (content is None or org_id is None):
        raise ValueError("a reason is stored in the content store, which isn't configured")
    # read most of the stream outside the write lock; the last step happens inside the write
    # transaction (which holds the lock), where only what was appended since is read, so a busy
    # writer can't keep the request from being written
    state = stop_state(store, run_id)
    with store.transaction() as uow:
        outcome = request_stop_in(
            uow,
            run_id,
            state,
            actor=actor,
            via=via,
            reason=reason,
            content=content,
            org_id=org_id,
            recheck=recheck,
        )
    if content is not None:
        content.flush_alerts()  # the capture_failed row, now that the transaction has ended
    return outcome


def request_stop_in(
    uow: UnitOfWork,
    run_id: str,
    state: StopState,
    *,
    actor: str,
    via: str,
    reason: str | None = None,
    content: ContentService | None = None,
    org_id: str | None = None,
    recheck: Callable[[UnitOfWork], None] | None = None,
) -> StopOutcome:
    """`request_stop` inside the caller's unit of work (R1.35): opens no transaction. `state` is
    the cheap `stop_state` pre-read taken before the transaction. `recheck(uow)` runs after the
    stream read and before any write (E2 spec 8.1). The caller calls
    `content.flush_alerts()` once its transaction has ended."""
    text = (reason or "").strip()
    if text and (content is None or org_id is None):
        raise ValueError("a reason is stored in the content store, which isn't configured")
    stream = run_stream(run_id)
    request, ended, cursor = state.request, state.ended, state.head_seq
    user = state.user_request
    while True:
        batch = uow.events_after(stream, cursor, PAGE)
        for event in batch:
            if event.type == STOP_TYPE:
                found = StopRequest.of(event)
                request = request or found
                if user is None and found.via not in HALT_VIAS:
                    user = found
            elif event.type == ENDED_TYPE:
                ended = True
        if batch:
            cursor = batch[-1].seq
        if len(batch) < PAGE:
            break
    if recheck is not None:
        recheck(uow)
    if cursor == 0:
        raise NotFoundError(f"no run '{run_id}'")
    if ended:
        raise RunFinished(f"run '{run_id}' has already ended")
    # the halt's request is first in the stream and does not cut calls (R27); a person's stop that
    # comes after it is its own entry (once), so a watcher that saw the halt can still see it
    if request is not None and (user is not None or via in HALT_VIAS):
        return StopOutcome(user or request, already_requested=True)
    payload: dict[str, Any] = {"via": via}
    if text:
        assert content is not None and org_id is not None
        try:
            payload["reason_hash"] = content.capture_reason(uow, org_id, text)
        except ContentError as exc:
            if not is_gate_refusal(exc):
                raise  # too big and the like: refused as before
            # Stop always works (K6): the stop is recorded without the reason; the text is
            # discarded, never stored, hashed or logged
            payload["reason_withheld"] = exc.code
    event = uow.append(
        EventDraft(
            stream=stream,
            type=EventType.RUN_STOP_REQUESTED,
            actor=actor,
            run_id=run_id,
            payload=payload,
        )
    )
    return StopOutcome(StopRequest.of(event), already_requested=False)


class StopWatch:
    """A cheap, thread-safe 'has this run been asked to stop?' for the engine's hot path.

    One head query when nothing was appended since the last look; otherwise it reads only the new
    events (a page at a time). Once a request is seen it stays seen.
    """

    def __init__(
        self,
        store: Store,
        run_id: str,
        org_id: str | None = None,
        *,
        halt: HaltWatch | None = None,
    ) -> None:
        self._store, self._stream = store, run_stream(run_id)
        # E19: with an organization, a halted org counts as a stop request (fail closed). The halt
        # and the role pauses are read by one `HaltWatch` (pass the engine's to share its cursor).
        self._halt = halt or (HaltWatch(store, org_id) if org_id is not None else None)
        self._task_stops: set[str] = set()  # tasks asked to stop by a role pause (E19)
        self._cursor = 0
        self._request: StopRequest | None = None
        self._user_request: StopRequest | None = None  # the first one that is not a halt's
        self._lock = threading.Lock()

    @property
    def request(self) -> StopRequest | None:
        """The request seen so far, if any."""
        return self._request

    @property
    def user_request(self) -> StopRequest | None:
        """The first request seen that is not an organization halt's, if any."""
        return self._user_request

    def requested(self) -> bool:
        """Whether a stop request is in the stream (reads new events if there are any), or the
        organization is halted (`via: org_halt`; an unreadable halt state counts as halted)."""
        if self._requested_in_run():
            return True
        if self._halt is None:
            return False
        try:
            state = self._halt.state()
            if not state.halted:
                return False
            request = StopRequest(
                seq=state.seq or 0,
                by=state.by or "system",
                at=state.at or self._store.now(),
                via="org_halt",
            )
        except Exception:
            request = StopRequest(
                seq=0, by="system", at=self._store.now(), via="org_halt_unreadable"
            )
        with self._lock:
            if self._request is None:
                self._request = request
        return True

    def user_stop(self) -> StopRequest | None:
        """A stop request of the run that is not an organization halt's, if one was seen (reads
        new events). A halt seen first does not hide it (R27: a person's stop cuts calls)."""
        with self._lock:
            if self._user_request is None:
                self._read_run()
            return self._user_request

    def _requested_in_run(self) -> bool:
        if self._request is not None:
            return True
        with self._lock:
            return self._read_run() is not None

    def _read_run(self) -> StopRequest | None:
        """Read what was appended to the run stream since the last look (caller holds the lock):
        the first `run.stop_requested`, and every `task.stop_requested`."""
        head = self._store.head(self._stream)
        while head is not None and head.seq > self._cursor and self._user_request is None:
            batch = list(self._store.read_stream(self._stream, after_seq=self._cursor, limit=PAGE))
            if not batch:
                break
            for event in batch:
                if event.type == STOP_TYPE:
                    found = StopRequest.of(event)
                    self._request = self._request or found
                    if self._user_request is None and found.via not in HALT_VIAS:
                        self._user_request = found
                elif event.type == TASK_STOP_TYPE:
                    task = event.payload.get("task")
                    if isinstance(task, str):
                        self._task_stops.add(task)
            self._cursor = batch[-1].seq
        return self._request

    def role_stopped(self, task_id: str, role: str) -> bool:
        """Whether this task must stop because its role is paused or it was asked to by a role
        pause (E19, `task.stop_requested`). Reads the same org stream as the halt (one code path,
        fail closed: an unreadable stream raises, and the caller treats that as stopped)."""
        with self._lock:
            if self._request is None:
                self._read_run()
            if task_id in self._task_stops:
                return True
        return self._halt is not None and role != "" and self._halt.role_paused(role)

    def record_interrupt(self, actor: str) -> StopRequest:
        """Ctrl-C: record the same kind of request (`via: ctrl_c`), unless one is already there.

        If the ledger can't take it (store down, run finished) the engine still stops: the request
        is held in memory so the run ends rather than carrying on."""
        return self.record_stop(actor, "ctrl_c")

    def record_stop(self, actor: str, via: str) -> StopRequest:
        """Record a stop request by `actor` (`via` says how) and hold it in memory too; if the
        ledger can't take it the engine still stops."""
        run_id = self._stream.removeprefix("run:")
        try:
            request = request_stop(self._store, run_id, actor=actor, via=via).request
        except (StoreError, ValueError):
            request = StopRequest(seq=0, by=actor, at=self._store.now(), via=via)
        with self._lock:
            self._request = request
            if via not in HALT_VIAS:
                self._user_request = request
        return request
