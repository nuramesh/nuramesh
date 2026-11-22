"""Held human decisions: the marker, the delivery pass and the reconcile sweep (deny-checks spec).

A human decision on a gate of a durable run is recorded by `record_decision`; waking the workflow
is a separate, at-most-once step. **Every newly recorded decision gets its marker (L1) BEFORE any
wake is sent**, so "decision committed and no marker" always means no wake went out (the sweep
repairs it). L1 to L4 are `decision.recorded` entries on the organization stream `org:<id>`:

- L1 `human_decision_held`: a wake is owed (`org_halted`, `halt_unreadable`, `wake_owed`,
  `reconciled`);
- L2 `human_decision_wake_attempted`: the delivery record, appended BEFORE the send (try 1 to 3);
- L4 `human_decision_wake_failed`: the waker raised an error other than a timeout (retry owed);
- L3 `human_decision_wake_closed`: the item is closed once (`sent`, `sent_unconfirmed`, `run_over`,
  `parked`).

Payloads hold ids, a hash and words only. Every append happens in a transaction that re-reads the
org stream and the store tombstone and writes nothing for an erased organization (ND121).
Nothing here imports Temporal: the wake is an injected `(run_id, task_id) -> None`.
"""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from nuramesh.core.auth import SecurityLogEntry, coalesced_row_id
from nuramesh.core.breakers import HaltState, counts_for_halt, halt_state
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.engine.deny_checks import deny_checks, erased_denial
from nuramesh.engine.org_halt import PAGE, org_stream_id
from nuramesh.engine.steps import HUMAN_DECISION
from nuramesh.stores.base import BadTimeError, EventDraft, Store, UnitOfWork, run_stream

_LOG = logging.getLogger(__name__)

HELD = "human_decision_held"
ATTEMPTED = "human_decision_wake_attempted"
FAILED = "human_decision_wake_failed"
CLOSED = "human_decision_wake_closed"
ACTOR = "system"

LEASE_SECONDS = 60
MAX_ATTEMPTS = 3
PASS_LIMIT = 100
RECONCILE_GRACE_SECONDS = 30
PARKED_WARNING_SECONDS = 7 * 86400
PARKED_WARNING = "decision_wake_parked"
WAKE_TIMEOUT_SECONDS = 3.0

Key = tuple[str, str, str, int, int]
Trigger = Literal["tick", "resume", "sweep"]
Wake = Callable[[str, str], None]


def key_of(payload: Any) -> Key | None:
    """The item key `(run, task, gate, attempt, submission)` of a marker payload, or None."""
    try:
        run, task, gate = payload["run"], payload["task"], payload["gate"]
        attempt, submission = payload["attempt"], payload["submission"]
    except (KeyError, TypeError):
        return None
    if not (isinstance(run, str) and isinstance(task, str) and isinstance(gate, str)):
        return None
    if not (isinstance(attempt, int) and isinstance(submission, int)):
        return None
    return (run, task, gate, attempt, submission)


def _payload(kind: str, key: Key, **more: Any) -> dict[str, Any]:
    run, task, gate, attempt, submission = key
    return {
        "kind": kind,
        "run": run,
        "task": task,
        "gate": gate,
        "attempt": attempt,
        "submission": submission,
        **more,
    }


@dataclass
class Item:
    """One held decision, folded from its L1 to L4 entries."""

    key: Key
    reason: str
    order: int  # the L1's seq: oldest first
    tries: int = 0
    last: LedgerEvent | None = None  # the latest L2 or L4
    closed: str | None = None  # the L3 result

    @property
    def pending(self) -> bool:
        return self.closed is None


class WakeFold:
    """Thread-safe, incremental fold of one organization's stream into halt state and held items
    (a cursor like `HaltWatch`: one head query when nothing changed)."""

    def __init__(self, store: Store, org_id: str) -> None:
        self._store, self._org, self._stream = store, org_id, org_stream_id(org_id)
        self._cursor = 0
        self._halt: LedgerEvent | None = None
        self.items: dict[Key, Item] = {}
        self.parked_at: list[datetime] = []
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _apply(self, e: LedgerEvent) -> None:
        if counts_for_halt(e):
            self._halt = e
        if e.type != EventType.DECISION_RECORDED.value:
            return
        kind = e.payload.get("kind")
        if kind not in (HELD, ATTEMPTED, FAILED, CLOSED):
            return
        key = key_of(e.payload)
        if key is None:
            return
        item = self.items.get(key)
        if kind == HELD:
            if item is None:
                self.items[key] = Item(key, str(e.payload.get("reason")), e.seq)
            return
        if item is None:
            return
        if kind == ATTEMPTED:
            item.tries += 1
            item.last = e
        elif kind == FAILED:
            item.last = e
        elif item.closed is None:
            item.closed = str(e.payload.get("result"))
            if item.closed == "parked":
                self.parked_at.append(e.at)

    def refresh(self, uow: UnitOfWork | None = None) -> None:
        """Read the entries appended since the last look (inside `uow` when given).

        Lock order is always store first, fold second: a read without `uow` takes the store's
        locks while the fold lock is NOT held, and only applies what it read under the fold lock
        (events at or before the cursor are skipped, so two readers never apply one twice)."""
        while True:
            with self._lock:
                cursor = self._cursor
            if uow is None:
                if cursor and (head := self._store.head(self._stream)) is not None:
                    if head.seq <= cursor:
                        return
                batch = list(self._store.read_stream(self._stream, after_seq=cursor, limit=PAGE))
            else:
                batch = uow.events_after(self._stream, cursor, PAGE)
            with self._lock:
                for e in batch:
                    if e.seq > self._cursor:
                        self._apply(e)
                        self._cursor = e.seq
            if len(batch) < PAGE:
                return

    def halt(self) -> HaltState:
        with self._lock:
            return halt_state([self._halt] if self._halt is not None else [])

    def pending(self) -> list[Item]:
        with self._lock:
            return sorted((i for i in self.items.values() if i.pending), key=lambda i: i.order)

    def item(self, key: Key) -> Item | None:
        with self._lock:
            return self.items.get(key)

    def parked_recently(self, now: datetime) -> bool:
        with self._lock:
            horizon = now - timedelta(seconds=PARKED_WARNING_SECONDS)
            return any(t > horizon for t in self.parked_at)


def parked_warning(fold: WakeFold, now: datetime) -> list[str]:
    """The health warning list this feature adds: `decision_wake_parked` while an item was parked
    within the last 7 days. Never raises."""
    try:
        fold.refresh()
        return [PARKED_WARNING] if fold.parked_recently(now) else []
    except Exception:
        _LOG.warning("could not read the held decisions", exc_info=True)
        return []


# -- the marker ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    """What `place_marker` did: `held` (written or found, the halt is in force), `owed` (written or
    found, usable), `erased` (nothing written), `unreadable` (nothing written; logged)."""

    status: Literal["held", "owed", "erased", "unreadable"]
    reason: str = ""


def place_marker(
    store: Store,
    fold: WakeFold,
    org_id: str,
    key: Key,
    decision_hash: str,
    *,
    usable_reason: str,
    halt_reason: str = "org_halted",
) -> Marker:
    """Write L1 for `key` (once: an existing L1 is reused), in one transaction that re-reads the
    org stream and the store tombstone. Tombstone found: nothing written (erased wins, even over a
    lifted halt). Halt in force: L1 with `halt_reason`. Else L1 with `usable_reason`."""
    try:
        with store.transaction() as uow:
            fold.refresh(uow)
            if uow.key_tombstone(org_id) is not None:
                return Marker("erased")
            halted = fold.halt().halted
            existing = fold.item(key)
            if existing is not None:
                return Marker("held" if halted else "owed", existing.reason)
            reason = halt_reason if halted else usable_reason
            uow.append(
                EventDraft(
                    stream=org_stream_id(org_id),
                    type=EventType.DECISION_RECORDED,
                    actor=ACTOR,
                    payload=_payload(HELD, key, decision_hash=decision_hash, reason=reason),
                )
            )
            return Marker("held" if halted else "owed", reason)
    except Exception:
        _LOG.warning("could not write the marker of a held decision", exc_info=True)
        return Marker("unreadable")


# -- one delivery pass ---------------------------------------------------------------------------


@dataclass
class DeliveryReport:
    """What a pass did. `reason` is set when it ended early (`halted`, `erased`, `deferred`,
    `error`). `outcomes` maps each item key it touched to a word."""

    reason: str = ""
    sent: list[Key] = field(default_factory=list)
    outcomes: dict[Key, str] = field(default_factory=dict)


def _run_over(uow: UnitOfWork, run_id: str) -> bool:
    cursor = 0
    while batch := uow.events_after(run_stream(run_id), cursor, PAGE):
        if any(
            e.type in (EventType.RUN_ENDED.value, EventType.RUN_STOP_REQUESTED.value) for e in batch
        ):
            return True
        cursor = batch[-1].seq
    return False


def _append(uow: UnitOfWork, org_id: str, kind: str, key: Key, **more: Any) -> LedgerEvent:
    return uow.append(
        EventDraft(
            stream=org_stream_id(org_id),
            type=EventType.DECISION_RECORDED,
            actor=ACTOR,
            payload=_payload(kind, key, **more),
        )
    )


def _claim(store: Store, fold: WakeFold, org_id: str, key: Key) -> tuple[str, int]:
    """One item, one transaction. Returns `(action, n)`: `skip`, `live`, `not_due`, `end` (halt in
    force), `erased`, `closed` (n unused; result in the action as `closed:<result>`), or `send` (n =
    the try number, L2 already appended)."""
    with store.transaction() as uow:
        fold.refresh(uow)
        item = fold.item(key)
        if item is None or not item.pending:
            return "skip", 0
        if fold.halt().halted:
            return "end", 0
        if uow.key_tombstone(org_id) is not None:
            return "erased", 0
        if _run_over(uow, key[0]):
            _append(uow, org_id, CLOSED, key, result="run_over")
            return "closed:run_over", 0
        now = uow.now()
        last = item.last
        if last is not None and last.payload.get("kind") == ATTEMPTED:
            if last.at + timedelta(seconds=LEASE_SECONDS) > now:
                return "live", 0
            _append(uow, org_id, CLOSED, key, result="sent_unconfirmed")
            return "closed:sent_unconfirmed", 0
        if last is not None:  # failed: the retry is due 60 s, then 120 s after the failure
            wait = 60 * 2 ** (int(last.payload.get("try", 1)) - 1)
            if now < last.at + timedelta(seconds=wait):
                return "not_due", 0
        if item.tries >= MAX_ATTEMPTS:  # unreachable: the third failure parks
            _append(uow, org_id, CLOSED, key, result="parked")
            return "closed:parked", 0
        _append(uow, org_id, ATTEMPTED, key, **{"try": item.tries + 1})
        return "send", item.tries + 1


def _finish(store: Store, fold: WakeFold, org_id: str, key: Key, kind: str, **more: Any) -> bool:
    """Append L3 or L4 unless the item is closed or the organization erased. True if written."""
    try:
        with store.transaction() as uow:
            fold.refresh(uow)
            item = fold.item(key)
            if item is None or not item.pending or uow.key_tombstone(org_id) is not None:
                return False
            _append(uow, org_id, kind, key, **more)
            return True
    except Exception:
        _LOG.warning("could not record the outcome of a wake", exc_info=True)
        return False


def close_erased(store: Store, fold: WakeFold, org_id: str) -> bool:
    """The held items of an erased organization are closed by ONE coalesced count on
    `content.capture_refused` (`tombstone`), only if an item is pending and no such row exists yet
    (any surviving day). Writes no org-scoped entry. True if a count was added."""
    try:
        fold.refresh()
        if not fold.pending():
            return False
        if any(
            r.type == "content.capture_refused" and r.details.get("reason") == "tombstone"
            for r in store.list_security_log(org_id)
        ):
            return False
        now = store.now()
        entry = SecurityLogEntry.new(
            org_id,
            now,
            "content.capture_refused",
            actor="system",
            details={"reason": "tombstone", "count": 1},
        ).model_copy(
            update={"id": coalesced_row_id("content.capture_refused", org_id, now, "tombstone")}
        )
        store.upsert_security_log_count(entry)
        return True
    except Exception:
        _LOG.warning("could not count the held decisions of an erased organization", exc_info=True)
        return False


def deliver_held(
    store: Store,
    org_id: str,
    wake: Wake,
    *,
    content: Any | None = None,
    trigger: Trigger = "tick",
    only: Key | None = None,
    fold: WakeFold | None = None,
) -> DeliveryReport:
    """One delivery pass (spec 8.3). Never raises: a store error is logged and ends the pass."""
    report = DeliveryReport()
    fold = fold if fold is not None else WakeFold(store, org_id)
    try:
        _pass(store, org_id, wake, content, trigger, only, fold, report)
    except Exception:
        _LOG.warning("a decision delivery pass failed", exc_info=True)
        report.reason = report.reason or "error"
    return report


def _pass(
    store: Store,
    org_id: str,
    wake: Wake,
    content: Any | None,
    trigger: Trigger,
    only: Key | None,
    fold: WakeFold,
    report: DeliveryReport,
) -> None:
    if trigger != "tick":
        erased = erased_denial(store, org_id, content=content)
        if erased is not None and erased.kind == "erased":
            if only is None:
                close_erased(store, fold, org_id)
            report.reason = "erased"
            return
    denial = deny_checks(store, org_id, content=content)
    if denial is not None:
        report.reason = (
            "halted" if denial.is_halt else "erased" if denial.kind == "erased" else "deferred"
        )
        if denial.kind == "erased" and trigger != "tick" and only is None:
            close_erased(store, fold, org_id)
        return
    fold.refresh()
    items = [fold.item(only)] if only is not None else fold.pending()
    for item in [i for i in items if i is not None and i.pending][:PASS_LIMIT]:
        key = item.key
        action, n = _claim(store, fold, org_id, key)
        if action == "end":
            report.reason = "halted"
            return
        if action == "erased":
            close_erased(store, fold, org_id)
            report.reason = "erased"
            report.outcomes[key] = "erased"
            return
        if action.startswith("closed:"):
            report.outcomes[key] = action.split(":", 1)[1]
            continue
        if action != "send":
            report.outcomes[key] = "pending"
            continue
        try:  # re-read the store tombstone only: a shred after the L2 sends nothing
            if store.key_tombstone(org_id) is not None:
                close_erased(store, fold, org_id)
                report.reason = "erased"
                report.outcomes[key] = "erased"
                return
        except Exception:
            report.reason = "deferred"
            return
        _send(store, fold, org_id, wake, key, n, report)


def _send(
    store: Store, fold: WakeFold, org_id: str, wake: Wake, key: Key, n: int, report: DeliveryReport
) -> None:
    try:
        wake(key[0], key[1])
    except TimeoutError:  # the signal may have gone out: never resent
        _LOG.warning("a wake timed out; it will not be resent (org %s run %s)", org_id, key[0])
        _finish(store, fold, org_id, key, CLOSED, result="sent_unconfirmed")
        report.outcomes[key] = "sent_unconfirmed"
        return
    except Exception:
        if n >= MAX_ATTEMPTS:
            _finish(store, fold, org_id, key, CLOSED, result="parked")
            _LOG.error(
                "a human decision wake was parked after %d tries (org %s run %s task %s gate %s)",
                n,
                org_id,
                key[0],
                key[1],
                key[2],
            )
            report.outcomes[key] = "parked"
        else:
            _finish(store, fold, org_id, key, FAILED, **{"try": n})
            report.outcomes[key] = "retry"
        return
    _finish(store, fold, org_id, key, CLOSED, result="sent")
    report.sent.append(key)
    report.outcomes[key] = "sent"


# -- the reconcile sweep -------------------------------------------------------------------------


@dataclass
class _RunScan:
    cursor: int = 0
    org: str | None = None
    done: bool = False
    damaged: bool = False  # a stored time in this run is not real (ND238/ND244)
    decisions: list[tuple[Key, str, datetime]] = field(default_factory=list)


@dataclass
class SweepState:
    """The sweep's memory between runs: a cursor per run stream (the first sweep reads each open
    run once), and the decisions already settled."""

    runs: dict[str, _RunScan] = field(default_factory=dict)
    settled: set[Key] = field(default_factory=set)


@dataclass
class SweepReport:
    reason: str = ""
    marked: list[Key] = field(default_factory=list)
    sent: list[Key] = field(default_factory=list)


def _scan_runs(store: Store, org_id: str, state: SweepState) -> None:
    for stream in store.streams():
        if not stream.startswith("run:"):
            continue
        scan = state.runs.get(stream)
        if scan is None:
            # the owner comes first, from the raw row, so another organization's run (damaged
            # or not) is never read, and a damaged run is contained to its own organization
            if store.run_owner(stream) != org_id:
                continue
            scan = state.runs[stream] = _RunScan(org=org_id)
        if scan.done or scan.org != org_id:
            continue
        try:
            _scan_one(store, stream, scan)
            scan.damaged = False
        except BadTimeError:
            _LOG.warning("run stream %s holds a time that is not real; skipped", stream)
            scan.damaged = True


def _scan_one(store: Store, stream: str, scan: _RunScan) -> None:
    """Read the new entries of one own run. Nothing is kept until the whole read succeeds, so a
    damaged entry leaves the scan as it was (and a later sweep reads the same entries again)."""
    head = store.head(stream)
    if head is None or head.seq <= scan.cursor:
        return
    cursor = scan.cursor
    done = False
    found: list[tuple[Key, str, datetime]] = []
    while True:
        batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
        for e in batch:
            if e.type in (EventType.RUN_ENDED.value, EventType.RUN_STOP_REQUESTED.value):
                done = True
            elif (
                e.type == EventType.DECISION_RECORDED.value
                and e.payload.get("kind") == HUMAN_DECISION
                and e.run_id is not None
                and e.task_id is not None
            ):
                p = e.payload
                gate, att, sub = p.get("gate"), p.get("attempt"), p.get("submission")
                if isinstance(gate, str) and isinstance(att, int) and isinstance(sub, int):
                    found.append(
                        ((e.run_id, e.task_id, gate, att, sub), str(p.get("decision_hash")), e.at)
                    )
        if batch:
            cursor = batch[-1].seq
        if len(batch) < PAGE:
            break
    scan.decisions.extend(found)
    scan.done = scan.done or done
    scan.cursor = cursor


def reconcile_decisions(
    store: Store,
    org_id: str,
    wake: Wake,
    *,
    content: Any | None = None,
    fold: WakeFold | None = None,
    state: SweepState | None = None,
) -> SweepReport:
    """Find decisions committed (L0) without a marker (L1), write the marker after the deny
    checks, then deliver through the one claim path (spec 8.6). Never raises."""
    report = SweepReport()
    fold = fold if fold is not None else WakeFold(store, org_id)
    state = state if state is not None else SweepState()
    try:
        _sweep(store, org_id, wake, content, fold, state, report)
    except Exception:
        _LOG.warning("the decision reconcile sweep failed", exc_info=True)
        report.reason = report.reason or "error"
    return report


def _sweep(
    store: Store,
    org_id: str,
    wake: Wake,
    content: Any | None,
    fold: WakeFold,
    state: SweepState,
    report: SweepReport,
) -> None:
    erased = erased_denial(store, org_id, content=content)
    if erased is not None and erased.kind == "erased":
        close_erased(store, fold, org_id)
        report.reason = "erased"
        return
    _scan_runs(store, org_id, state)
    now = store.now()
    young = now - timedelta(seconds=RECONCILE_GRACE_SECONDS)
    fold.refresh()
    candidates: list[tuple[datetime, Key, str]] = []
    for scan in state.runs.values():
        if scan.org != org_id or scan.done or scan.damaged:
            continue
        for key, dhash, at in scan.decisions:
            if key in state.settled or fold.item(key) is not None:
                continue
            if at > young:
                continue  # a live request owns it
            candidates.append((at, key, dhash))
    candidates.sort(key=lambda c: c[0])  # stable: equal times keep ledger order
    for _at, key, dhash in candidates[:PASS_LIMIT]:
        denial = deny_checks(store, org_id, content=content)
        if denial is not None and not denial.is_halt:
            report.reason = "erased" if denial.kind == "erased" else "deferred"
            return
        if denial is not None and denial.kind == "halt_unreadable":
            report.reason = "halted"
            return
        if _run_ended_or_stopping(store, key[0]):
            state.settled.add(key)
            continue
        marker = place_marker(
            store,
            fold,
            org_id,
            key,
            dhash,
            usable_reason="reconciled",
            halt_reason="org_halted",
        )
        if marker.status == "erased":
            report.reason = "erased"
            return
        if marker.status == "unreadable":
            report.reason = "error"
            return
        state.settled.add(key)
        report.marked.append(key)
        if marker.status == "owed":
            sent = deliver_held(
                store, org_id, wake, content=content, trigger="sweep", only=key, fold=fold
            )
            report.sent.extend(sent.sent)


def _run_ended_or_stopping(store: Store, run_id: str) -> bool:
    cursor = 0
    while batch := list(store.read_stream(run_stream(run_id), after_seq=cursor, limit=PAGE)):
        if any(
            e.type in (EventType.RUN_ENDED.value, EventType.RUN_STOP_REQUESTED.value) for e in batch
        ):
            return True
        cursor = batch[-1].seq
    return False


def keys_of(items: Iterable[Item]) -> list[Key]:
    """The keys of items, in order (for tests and reports)."""
    return [i.key for i in items]


# -- the wake gate (the API route and the Temporal client's `decide()` share it) ---------------


def hold_reason(kind: str) -> str:
    """The `held_reason` word of a halt denial."""
    return "halt_unreadable" if kind == "halt_unreadable" else "org_halted"


def _claim_for(
    store: Store,
    org_id: str,
    content: Any | None,
    wake: Wake,
    key: Key,
    fold: WakeFold,
    deliver: Callable[..., DeliveryReport],
) -> tuple[bool, bool, str]:
    report = deliver(store, org_id, wake, content=content, trigger="tick", only=key, fold=fold)
    outcome = report.outcomes.get(key, "pending" if not report.reason else report.reason)
    if outcome == "sent":
        return True, False, ""
    if outcome in ("sent_unconfirmed", "run_over", "erased"):
        return False, False, ""
    if outcome == "parked":
        return False, True, "delivery_parked"
    return False, True, "delivery_pending"


def wake_gate(
    store: Store,
    org_id: str,
    content: Any | None,
    fold: WakeFold,
    wake: Wake,
    run_id: str,
    made: Any,
    *,
    deny: Callable[..., Any] = deny_checks,
    marker: Callable[..., Marker] | None = None,
    deliver: Callable[..., DeliveryReport] | None = None,
) -> tuple[bool | None, bool, str]:
    """The wake gate (deny-checks spec 4, step 6): `(woken, held, held_reason)` for a decision
    just recorded (`made`: a `RecordedDecision`). A marker (L1) always precedes a send; a halted
    or unusable organization is never signalled (the decision stays recorded and held)."""
    place = marker if marker is not None else place_marker
    send = deliver if deliver is not None else deliver_held
    key = (run_id, made.task_id, made.gate, made.attempt, made.submission)
    denial = deny(store, org_id, content=content)
    try:
        if denial is not None and not denial.is_halt:
            if content is not None and denial.kind in ("erased", "key_lost"):
                reason = "tombstone" if denial.kind == "erased" else "orphaned_content"
                content.record_security_count(org_id, "content.capture_refused", reason)
            return False, False, ""
        fold.refresh()
        existing = fold.item(key)
        if made.already_recorded:  # a replay never sends a wake of its own
            if existing is None or not existing.pending:
                held_parked = existing is not None and existing.closed == "parked"
                return False, held_parked, "delivery_parked" if held_parked else ""
            if denial is not None:
                return False, True, hold_reason(denial.kind)
            return _claim_for(store, org_id, content, wake, key, fold, send)
        placed = place(
            store,
            fold,
            org_id,
            key,
            made.decision_hash,
            usable_reason="wake_owed",
            halt_reason=hold_reason(denial.kind) if denial is not None else "org_halted",
        )
        if placed.status == "erased":
            if content is not None:
                content.record_security_count(org_id, "content.capture_refused", "tombstone")
            return False, False, ""
        if placed.status == "unreadable":
            return False, denial is not None, hold_reason(denial.kind) if denial else ""
        if placed.status == "held":
            return False, True, placed.reason
        return _claim_for(store, org_id, content, wake, key, fold, send)
    except Exception:
        _LOG.warning("the wake gate failed; nothing was signalled", exc_info=True)
        if denial is not None and denial.is_halt:
            return False, True, hold_reason(denial.kind)
        return False, False, ""
