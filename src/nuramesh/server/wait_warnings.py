"""The expiry warning to the approver: the server-side delivery pass (R1.20 PR D, spec 7).

The engines (the Temporal activity, the light runner) only write the ledger entry
`decision.recorded kind=human_wait_warning`; they never send mail and never import `MailService`.
This pass reads those entries for both engines and sends one fixed, content-free mail to each
person who may answer the gate: the approvers the gate names, else every member holding `decide`;
named approvers are resolved inside the run's organization only, and a disabled or erased user or
an address that is not verified is skipped. An agent never receives one.

Delivery is at-least-once with an idempotent key `(run, task, attempt, lead, recipient_hash)`:

1. an intent marker (`decision.recorded kind=human_wait_warning_mail_attempted`, try 1) is appended
   BEFORE the send, in a transaction that re-reads the organization stream and appends only if no
   result and no still-fresh marker exists, so two server replicas cannot both mark;
2. the send writes the existing `email.sent` or `email.failed` (carrying the key ids);
3. a marker without a result (a crash between the send and the result) is retried ONCE, after five
   minutes (try 2, marked the same way); after the second unresolved attempt the pass stops for that
   key and writes nothing more. The recipient may then get the same mail twice, never more.

A failed mail never blocks the warning entry or the wait: the in-app marker is the guaranteed
channel. A halted, erased or key-lost organization gets no mail (the clock goes on, spec 7 item 7);
`needs_human` tasks are never warned (the engines do not write the entry for them, R1.20d).
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nuramesh.core.auth import ROLE_PERMISSIONS, Permission, User, identifier_hash
from nuramesh.core.canonical import thaw
from nuramesh.core.gate_config import HumanGateConfig
from nuramesh.core.gates import GateKind
from nuramesh.core.identity import MailSendError
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.tasks import TaskStatus
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.org_halt import PAGE, org_stream_id
from nuramesh.engine.run import open_human_gates
from nuramesh.engine.steps import HUMAN_WAIT_WARNING
from nuramesh.server.mail import MailService
from nuramesh.server.mail_templates import wait_warning_mail
from nuramesh.server.secevents import SecurityRecorder
from nuramesh.stores.base import BadTimeError, EventDraft, Store, UnitOfWork

_LOG = logging.getLogger(__name__)

MARKER = "human_wait_warning_mail_attempted"
RETRY_AFTER = timedelta(minutes=5)
MAX_TRIES = 2
PASS_LIMIT = 100
ACTOR = "system"

WarnKey = tuple[str, str, int, int]  # run, task, attempt, lead (days)
MailKey = tuple[str, str, int, int, str]  # + recipient_hash


@dataclass
class _Scan:
    cursor: int = 0
    done: bool = False


@dataclass
class WarningState:
    """The pass's memory between ticks: a cursor per run stream, the warnings found, and an
    incremental fold of the organization stream (markers and results by key)."""

    runs: dict[str, _Scan] = field(default_factory=dict)
    warnings: dict[WarnKey, datetime] = field(default_factory=dict)
    settled: set[WarnKey] = field(default_factory=set)
    cursor: int = 0
    markers: dict[MailKey, list[LedgerEvent]] = field(default_factory=dict)
    results: set[MailKey] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def apply(self, e: LedgerEvent) -> None:
        """Fold one organization-stream entry (idempotent by seq)."""
        with self.lock:
            if e.seq <= self.cursor:
                return
            self.cursor = e.seq
            p = e.payload
            key = _mail_key(p)
            if key is None:
                return
            if e.type == EventType.DECISION_RECORDED and p.get("kind") == MARKER:
                self.markers.setdefault(key, []).append(e)
            elif e.type in (EventType.EMAIL_SENT, EventType.EMAIL_FAILED):
                self.results.add(key)

    def refresh(self, store: Store, org_id: str, uow: UnitOfWork | None = None) -> None:
        stream = org_stream_id(org_id)
        while True:
            after = self.cursor
            batch = (
                uow.events_after(stream, after, PAGE)
                if uow is not None
                else list(store.read_stream(stream, after_seq=after, limit=PAGE))
            )
            for e in batch:
                self.apply(e)
            if len(batch) < PAGE:
                return


def _mail_key(p: Any) -> MailKey | None:
    try:
        run, task, attempt, lead, rh = (
            p["run"],
            p["task"],
            p["attempt"],
            p["lead"],
            p["recipient_hash"],
        )
    except (KeyError, TypeError):
        return None
    if not (isinstance(run, str) and isinstance(task, str) and isinstance(rh, str)):
        return None
    if not (isinstance(attempt, int) and isinstance(lead, int)):
        return None
    return (run, task, attempt, lead, rh)


@dataclass
class WarningReport:
    reason: str = ""
    sent: int = 0
    failed: int = 0


def _scan_runs(store: Store, org_id: str, state: WarningState) -> None:
    for stream in store.streams():
        if not stream.startswith("run:"):
            continue
        scan = state.runs.get(stream)
        if scan is None:
            if store.run_owner(stream) != org_id:
                continue  # another organization's run is never read
            scan = state.runs[stream] = _Scan()
        if scan.done:
            continue
        try:
            head = store.head(stream)
            if head is None or head.seq <= scan.cursor:
                continue
            cursor = scan.cursor
            done = False
            found: dict[WarnKey, datetime] = {}
            while True:
                batch = list(store.read_stream(stream, after_seq=cursor, limit=PAGE))
                for e in batch:
                    if e.type in (EventType.RUN_ENDED, EventType.RUN_STOP_REQUESTED):
                        done = True
                    elif (
                        e.type == EventType.DECISION_RECORDED
                        and e.payload.get("kind") == HUMAN_WAIT_WARNING
                        and e.run_id is not None
                    ):
                        p = e.payload
                        task, attempt, lead = p.get("task_id"), p.get("attempt"), p.get("days_left")
                        if (
                            isinstance(task, str)
                            and isinstance(attempt, int)
                            and isinstance(lead, int)
                        ):
                            found[(e.run_id, task, attempt, lead)] = e.at
                if batch:
                    cursor = batch[-1].seq
                if len(batch) < PAGE:
                    break
            state.warnings.update(found)
            scan.cursor, scan.done = cursor, scan.done or done
        except BadTimeError:
            _LOG.warning("run stream %s holds a time that is not real; skipped", stream)


def _may_decide(user: User) -> bool:
    return (
        user.erased_at is None
        and user.disabled_at is None
        and user.email is not None
        and user.email_verified_at is not None
        and Permission.DECIDE in ROLE_PERMISSIONS.get(user.role, frozenset())
    )


def recipients(store: Store, org_id: str, run_id: str, task_id: str, attempt: int) -> list[User]:
    """Who is told about a waiting task, or `[]` when it no longer waits for a person."""
    stored = store.get_task(task_id)
    if stored is None or stored.task.run_id != run_id or stored.task.attempt != attempt:
        return []
    task = stored.task
    if task.status is not TaskStatus.VERIFYING:
        return []
    open_ids = open_human_gates(task, store.task_events(run_id, task_id))
    if not open_ids:
        return []
    named: set[str] = set()
    anyone = False
    for spec in task.definition_of_done.required_gates:
        if spec.kind is not GateKind.HUMAN or spec.id not in open_ids:
            continue
        try:
            config = HumanGateConfig.model_validate(thaw(spec.config))
        except ValueError:
            continue
        if config.approvers:
            named.update(config.approvers)
        else:
            anyone = True
    users = [u for u in store.list_users(org_id) if u.org_id == org_id and _may_decide(u)]
    chosen = [u for u in users if anyone or u.id in named]
    return sorted(chosen, key=lambda u: u.id)


def _claim(
    store: Store, state: WarningState, org_id: str, key: MailKey, *, clock_label: str = ""
) -> int:
    """One key, one transaction. Returns the try number to send (the marker is appended), or 0."""
    with store.transaction() as uow:
        if uow.key_tombstone(org_id) is not None:
            return 0
        state.refresh(store, org_id, uow)
        if key in state.results:
            return 0
        marks = state.markers.get(key, [])
        if len(marks) >= MAX_TRIES:
            return 0  # the second attempt is unresolved: stop, write nothing more
        if marks and marks[-1].at + RETRY_AFTER > uow.now():
            return 0
        run, task, attempt, lead, rh = key
        n = len(marks) + 1
        event = uow.append(
            EventDraft(
                stream=org_stream_id(org_id),
                type=EventType.DECISION_RECORDED,
                actor=ACTOR,
                payload={
                    "kind": MARKER,
                    "run": run,
                    "task": task,
                    "attempt": attempt,
                    "lead": lead,
                    "recipient_hash": rh,
                    "try": n,
                },
            )
        )
        state.apply(event)
        return n


def deliver_warnings(
    store: Store,
    org_id: str,
    mail: MailService,
    state: WarningState,
    *,
    content: Any | None = None,
) -> WarningReport:
    """One delivery pass. Never raises: a failure is logged and the next tick tries again."""
    report = WarningReport()
    try:
        _pass(store, org_id, mail, state, content, report)
    except Exception:
        _LOG.warning("the wait warning delivery pass failed", exc_info=True)
        report.reason = report.reason or "error"
    return report


def _pass(
    store: Store,
    org_id: str,
    mail: MailService,
    state: WarningState,
    content: Any | None,
    report: WarningReport,
) -> None:
    if not mail.enabled:
        report.reason = "mail_off"  # N07: in-app only, no send, no failure entry
        return
    denial = deny_checks(store, org_id, content=content)
    if denial is not None:
        report.reason = "halted" if denial.is_halt else "denied"
        return
    _scan_runs(store, org_id, state)
    state.refresh(store, org_id)
    rec = SecurityRecorder(org_id, store.now)
    handled = 0
    for wkey in sorted(state.warnings, key=lambda k: state.warnings[k]):
        if wkey in state.settled:
            continue
        run, task, attempt, lead = wkey
        people = recipients(store, org_id, run, task, attempt)
        if not people:
            state.settled.add(wkey)
            continue
        for user in people:
            if handled >= PASS_LIMIT:
                return
            assert user.email is not None
            with store.transaction() as uow:
                rh = identifier_hash(rec.key(uow, user.id), org_id, user.email)
            key: MailKey = (run, task, attempt, lead, rh)
            if _claim(store, state, org_id, key) == 0:
                continue
            handled += 1
            base = mail.public_url
            letter = wait_warning_mail(
                user.email,
                days_left=lead,
                at=store.now(),
                org_name=mail.org_name,
                link=None if base is None else f"{base.rstrip('/')}/approvals",
            )
            try:
                mail.deliver(
                    letter,
                    ACTOR,
                    extra={"run": run, "task": task, "attempt": attempt, "lead": lead},
                )
                report.sent += 1
            except MailSendError:
                report.failed += 1  # email.failed is written by deliver; the wait goes on
            state.refresh(store, org_id)
