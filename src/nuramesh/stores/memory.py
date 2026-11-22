"""In-memory store: for tests, the CLI's dry runs and library use without a database.

One lock serialises transactions. A unit of work writes to private buffers and applies them all at
once on commit, so a failed block leaves nothing behind and readers never see half of one.
"""

import json
import secrets
import threading
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nuramesh.core.auth import (
    AGENT_CREDENTIAL_ENDED_RETENTION,
    ERASED_ACTOR,
    ERASED_DETAIL_KEYS,
    HALT_PENDING_REASON,
    MAX_COUNT,
    AgentCredential,
    ApiToken,
    AuthFailure,
    AuthSession,
    AuthToken,
    Credential,
    Role,
    SecurityLogEntry,
    SetupToken,
    User,
    erased_copy,
    new_identifier_key,
    next_erased_token_name,
    normalize_email,
)
from nuramesh.core.budgets import BudgetBalance, Reservation, ReservationStatus, org_key
from nuramesh.core.canonical import thaw
from nuramesh.core.content import ContentKind, ContentRecord, KeyTombstone
from nuramesh.core.email_change import (
    EMAIL_CHANGE_RETENTION,
    EmailChange,
    email_change_ended_at,
)
from nuramesh.core.idempotency import Scope
from nuramesh.core.identity import ERASED_CREATOR, INVITE_RETENTION, Invite, invite_ended_at
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.mfa import (
    MFA_PENDING_LIFETIME,
    MfaChallenge,
    MfaFactor,
    OrgAuthSettings,
)
from nuramesh.core.password_reset import (
    PasswordLoginBlock,
    ResetToken,
    RevokeLink,
    reset_retention_end,
    revoke_link_retention_end,
)
from nuramesh.core.policy import Approval, ApprovalMode, ApprovalStatus
from nuramesh.core.projection import ALL_KINDS, RUN_STARTED, projection_key
from nuramesh.core.tasks import Task
from nuramesh.stores.base import (
    CONTENT_SUBJECT_COLUMNS,
    CONTENT_SUBJECT_INDEXES,
    AlreadyExistsError,
    Clock,
    ConcurrencyError,
    ContentSubjectRow,
    ExtensionRecord,
    NestedTransactionError,
    NotFoundError,
    ProjectionStreams,
    Store,
    StoredApproval,
    StoredBalance,
    StoredIdempotency,
    StoredReservation,
    StoredTask,
    StoreError,
    StreamHead,
    UnitOfWork,
    check_limit,
    clamped_expiry,
    definition_order,
    is_budget_definition,
    keyed_ids_of,
    merged_expiry,
    projection_streams,
    run_stream,
    utc_now,
    verify_projected,
)
from nuramesh.stores.run_queue import KEYS as RQ_KEYS
from nuramesh.stores.tombstones import bumped, shred_entries


@dataclass
class _Projection:
    """The autonomy projection (T136): indexed entries by `(stream, kind)`, a count per pair kept
    apart from the entries (so a disagreement is detectable), and each organization's runs."""

    entries: dict[tuple[str, str], list[LedgerEvent]] = field(default_factory=dict)
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    runs: dict[str, list[str]] = field(default_factory=dict)

    def add(self, event: LedgerEvent) -> None:
        key = projection_key(event)
        if key is None:
            return
        pair = (event.stream, key.kind)
        self.entries.setdefault(pair, []).append(event)
        self.counts[pair] = self.counts.get(pair, 0) + 1
        if key.kind == RUN_STARTED:
            self.runs.setdefault(key.org or "", []).append(event.stream)

    def merge(self, other: "_Projection") -> None:
        for pair, events in other.entries.items():
            self.entries.setdefault(pair, []).extend(events)
        for pair, n in other.counts.items():
            self.counts[pair] = self.counts.get(pair, 0) + n
        for org, streams in other.runs.items():
            self.runs.setdefault(org, []).extend(streams)


def _read_projection(
    parts: tuple[_Projection, ...],
    position_of: Callable[[LedgerEvent], int],
    org_id: str,
    kinds: Collection[str] | None,
    scope: ProjectionStreams = "all",
) -> list[tuple[int, LedgerEvent]]:
    wanted = ALL_KINDS if kinds is None else frozenset(kinds) & ALL_KINDS
    streams = [f"org:{org_id}"]
    if scope == "all":
        for part in parts:
            streams.extend(part.runs.get(org_id, ()))
    rows: list[tuple[str, str, int, LedgerEvent]] = []
    counts: dict[tuple[str, str], int] = {}
    for stream in dict.fromkeys(streams):
        for kind in wanted:
            for part in parts:
                for event in part.entries.get((stream, kind), ()):
                    rows.append((stream, kind, position_of(event), event))
                if (stream, kind) in part.counts:
                    counts[(stream, kind)] = (
                        counts.get((stream, kind), 0) + part.counts[(stream, kind)]
                    )
    return verify_projected(rows, counts)


@dataclass
class _State:
    projection: _Projection = field(default_factory=_Projection)
    events: dict[str, list[LedgerEvent]] = field(default_factory=dict)
    positions: dict[tuple[str, int], int] = field(default_factory=dict)  # commit positions
    position: int = 0  # the latest commit position (E13, T135)
    budget_definitions: list[LedgerEvent] = field(default_factory=list)  # the index (R8)
    tasks: dict[str, StoredTask] = field(default_factory=dict)
    balances: dict[tuple[str, str], StoredBalance] = field(default_factory=dict)
    reservations: dict[str, StoredReservation] = field(default_factory=dict)
    approvals: dict[str, StoredApproval] = field(default_factory=dict)
    content: dict[tuple[str, str], ContentRecord] = field(default_factory=dict)
    subjects: dict[tuple[str, str, str], tuple[str | None, bool]] = field(default_factory=dict)
    runq: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = field(default_factory=dict)
    holds: set[str] = field(default_factory=set)
    users: dict[tuple[str, str], User] = field(default_factory=dict)
    credentials: dict[tuple[str, str], Credential] = field(default_factory=dict)
    setup_tokens: dict[tuple[str, str], SetupToken] = field(default_factory=dict)
    auth_sessions: dict[tuple[str, str], AuthSession] = field(default_factory=dict)
    auth_tokens: dict[tuple[str, str], AuthToken] = field(default_factory=dict)
    auth_failures: dict[str, AuthFailure] = field(default_factory=dict)
    mfa_factors: dict[tuple[str, str], MfaFactor] = field(default_factory=dict)
    # (org, user, code hash) -> (created, used)
    mfa_codes: dict[tuple[str, str, str], tuple[datetime, datetime | None]] = field(
        default_factory=dict
    )
    mfa_challenges: dict[tuple[str, str], MfaChallenge] = field(default_factory=dict)
    org_auth: dict[str, OrgAuthSettings] = field(default_factory=dict)
    security_log: dict[tuple[str, str], SecurityLogEntry] = field(default_factory=dict)
    user_keys: dict[tuple[str, str], bytes] = field(default_factory=dict)
    org_keys: dict[tuple[str, int], bytes | None] = field(default_factory=dict)  # None: destroyed
    api_tokens: dict[tuple[str, str], ApiToken] = field(default_factory=dict)
    retired_names: dict[tuple[str, int, str], datetime] = field(default_factory=dict)
    extension: dict[tuple[str, str, str, str], ExtensionRecord] = field(default_factory=dict)
    agent_credentials: dict[tuple[str, str], AgentCredential] = field(default_factory=dict)
    invites: dict[tuple[str, str], Invite] = field(default_factory=dict)
    email_changes: dict[tuple[str, str], EmailChange] = field(default_factory=dict)
    reset_tokens: dict[tuple[str, str], ResetToken] = field(default_factory=dict)
    revoke_links: dict[tuple[str, str], RevokeLink] = field(default_factory=dict)
    login_blocks: dict[tuple[str, str], PasswordLoginBlock] = field(default_factory=dict)
    tombstones: dict[str, KeyTombstone] = field(default_factory=dict)
    idem: dict[tuple[str, str, str, str, str], StoredIdempotency] = field(default_factory=dict)
    store_id: str = field(default_factory=lambda: secrets.token_hex(16))


def _next_version(kind: str, key: str, current: int | None, expected: int | None) -> int:
    """Optimistic concurrency shared by every versioned record."""
    if expected is None:
        if current is not None:
            raise ConcurrencyError(f"{kind} '{key}' already exists")
        return 1
    if current != expected:
        raise ConcurrencyError(f"{kind} '{key}' is at version {current}, not {expected}")
    return expected + 1


class _MemoryUnitOfWork(UnitOfWork):
    def __init__(self, state: _State, clock: Clock) -> None:
        self._state = state
        self._clock = clock
        self._at = clock()  # read once, inside the write lock (ND234)
        self.new_events: dict[str, list[LedgerEvent]] = {}
        self.new_positions: dict[tuple[str, int], int] = {}
        self.new_projection = _Projection()
        self.position = state.position
        self.new_tasks: dict[str, StoredTask] = {}
        self.new_balances: dict[tuple[str, str], StoredBalance] = {}
        self.new_reservations: dict[str, StoredReservation] = {}
        self.new_approvals: dict[str, StoredApproval] = {}
        self.deleted_approvals: set[str] = set()
        self.new_content: dict[tuple[str, str], ContentRecord | None] = {}  # None: deleted
        # (org, content id, subject) -> (run id, erase pending); None: deleted
        self.new_subjects: dict[tuple[str, str, str], tuple[str | None, bool] | None] = {}
        self.new_runq: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        self.new_holds: dict[str, bool] = {}
        self.new_tombstones: dict[str, KeyTombstone] = {}
        self.new_users: dict[tuple[str, str], User] = {}
        self.new_credentials: dict[tuple[str, str], Credential | None] = {}  # None: removed
        self.new_tokens: dict[tuple[str, str], SetupToken] = {}
        self.auth: _State | None = None  # a private copy of the auth tables, made on first use

    def last_event(self, stream: str) -> LedgerEvent | None:
        pending = self.new_events.get(stream)
        if pending:
            return pending[-1]
        committed = self._state.events.get(stream)
        return committed[-1] if committed else None

    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        merged = [*self._state.events.get(stream, ()), *self.new_events.get(stream, ())]
        return [e for e in merged if e.seq > after_seq][:limit]

    def events_after_positioned(
        self, stream: str, after_seq: int, limit: int
    ) -> list[tuple[int, LedgerEvent]]:
        return [(self._position_of(e), e) for e in self.events_after(stream, after_seq, limit)]

    def _position_of(self, event: LedgerEvent) -> int:
        key = (event.stream, event.seq)
        return self.new_positions.get(key) or self._state.positions.get(key, 0)

    def commit_position(self) -> int:
        return self.position

    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        check_limit(limit)
        found = [
            (self._position_of(e), e)
            for events in (self._state.events, self.new_events)
            for stream_events in events.values()
            for e in stream_events
        ]
        found = sorted((item for item in found if item[0] > pos), key=lambda item: item[0])
        return found if limit is None else found[:limit]

    def has_event(self, stream: str, type: EventType) -> bool:
        wanted = type.value
        return any(e.type == wanted for e in self._state.events.get(stream, ())) or any(
            e.type == wanted for e in self.new_events.get(stream, ())
        )

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        stream = run_stream(run_id)
        merged = [*self._state.events.get(stream, ()), *self.new_events.get(stream, ())]
        return [e for e in merged if e.task_id == task_id]

    def head(self, stream: str) -> StreamHead | None:
        event = self.last_event(stream)
        if event is None:
            return None
        return StreamHead(stream=stream, seq=event.seq, hash=event.hash, at=event.at)

    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        return _read_projection(
            (self._state.projection, self.new_projection),
            self._position_of,
            org_id,
            kinds,
            projection_streams(streams),
        )

    def _insert_event(self, event: LedgerEvent) -> None:
        self.new_events.setdefault(event.stream, []).append(event)
        self.new_projection.add(event)
        self.position += 1
        self.new_positions[(event.stream, event.seq)] = self.position

    def get_task(self, task_id: str) -> StoredTask | None:
        return self.new_tasks.get(task_id) or self._state.tasks.get(task_id)

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        merged = {**self._state.tasks, **self.new_tasks}
        return sorted(
            (t for t in merged.values() if t.task.run_id == run_id), key=lambda t: t.task.id
        )

    def put_task(self, task: Task, *, expected_version: int | None) -> int:
        current = self.get_task(task.id)
        version = _next_version(
            "task", task.id, None if current is None else current.version, expected_version
        )
        self.new_tasks[task.id] = StoredTask(task=task, version=version)
        return version

    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        key = (org_id, budget_id)
        return self.new_balances.get(key) or self._state.balances.get(key)

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        merged = {**self._state.balances, **self.new_balances}
        return [merged[k] for k in sorted(merged) if org_id is None or k[0] == org_id]

    def put_balance(self, balance: BudgetBalance, *, expected_version: int | None) -> int:
        key = (org_key(balance.budget.org_id), balance.budget.id)
        current = self.get_balance(*key)
        version = _next_version(
            "budget", key[1], None if current is None else current.version, expected_version
        )
        self.new_balances[key] = StoredBalance(balance=balance, version=version)
        return version

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        if approval_id in self.deleted_approvals:
            return None
        return self.new_approvals.get(approval_id) or self._state.approvals.get(approval_id)

    def delete_approval_if_due(
        self, approval_id: str, now: datetime, *, expected_version: int | None = None
    ) -> bool:
        current = self.get_approval(approval_id)
        if current is None or not current.approval.retention_over(now):
            return False
        if expected_version is not None and current.version != expected_version:
            return False
        self.new_approvals.pop(approval_id, None)
        self.deleted_approvals.add(approval_id)
        return True

    def put_approval(self, approval: Approval, *, expected_version: int | None) -> int:
        current = self.get_approval(approval.id)
        version = _next_version(
            "approval", approval.id, None if current is None else current.version, expected_version
        )
        self.deleted_approvals.discard(approval.id)  # put after delete in one unit: it stays
        self.new_approvals[approval.id] = StoredApproval(approval=approval, version=version)
        return version

    def solo_approvals_after(self, decided_by: str, after: datetime) -> list[Approval]:
        merged = {**self._state.approvals, **self.new_approvals}
        found = [
            s.approval
            for s in merged.values()
            if s.approval.approval_mode is ApprovalMode.SOLO
            and s.approval.decided_by == decided_by
            and s.approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED)
            and s.approval.decided_at is not None
            and s.approval.decided_at > after
        ]
        return sorted(found, key=lambda a: (a.decided_at, a.id))

    def approvals_matching(
        self, run_id: str, agent_id: str, status: ApprovalStatus
    ) -> list[StoredApproval]:
        merged = {**self._state.approvals, **self.new_approvals}
        found = [
            a
            for a in merged.values()
            if a.approval.id not in self.deleted_approvals
            and a.approval.run_id == run_id
            and a.approval.agent_id == agent_id
            and a.approval.status is status
        ]
        return sorted(found, key=lambda a: a.approval.id)

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        return self.new_reservations.get(reservation_id) or self._state.reservations.get(
            reservation_id
        )

    def _content(self, key: tuple[str, str]) -> ContentRecord | None:
        if key in self.new_content:
            return self.new_content[key]
        return self._state.content.get(key)

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        return self._content((org_id, content_id))

    def put_content(self, record: ContentRecord) -> None:
        key = (record.org_id, record.id)
        current = self._content(key)
        if current is not None:
            expiry = merged_expiry(current.expires_at, record.expires_at)
            record = current.model_copy(update={"expires_at": expiry})
        self.new_content[key] = record

    def set_run_content_expiry(
        self, org_id: str, content_id: str, expires_at: datetime | None
    ) -> bool:
        key = (org_id, content_id)
        current = self._content(key)
        if current is None or current.kind not in (
            ContentKind.TASK_OUTPUT,
            ContentKind.GATE_EVIDENCE,
            ContentKind.PLAN_TEXT,
        ):
            return False
        self.new_content[key] = current.model_copy(update={"expires_at": expires_at})
        return True

    def shorten_content_expiry(
        self,
        kind: ContentKind,
        max_days: int,
        *,
        org_id: str | None = None,
        content_id: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, int]:
        changed: dict[str, int] = {}
        for key in sorted(set(self._state.content) | set(self.new_content)):
            current = self._content(key)
            if current is None or current.kind is not kind:
                continue
            if (org_id is not None and key[0] != org_id) or (
                content_id is not None and key[1] != content_id
            ):
                continue
            new = clamped_expiry(since or current.created_at, current.expires_at, max_days)
            if new is not None:
                self.new_content[key] = current.model_copy(update={"expires_at": new})
                changed[key[0]] = changed.get(key[0], 0) + 1
        return changed

    def delete_content(self, org_id: str, content_id: str) -> bool:
        key = (org_id, content_id)
        existed = self._content(key) is not None
        self.new_content[key] = None
        self._drop_subjects(org_id, content_id)
        return existed

    def _subject_view(self) -> dict[tuple[str, str, str], tuple[str | None, bool]]:
        merged = dict(self._state.subjects)
        for key, tagged in self.new_subjects.items():
            if tagged is None:
                merged.pop(key, None)
            else:
                merged[key] = tagged
        return merged

    def _drop_subjects(self, org_id: str, content_id: str) -> None:
        for key in self._subject_view():
            if key[0] == org_id and key[1] == content_id:
                self.new_subjects[key] = None

    def _put_content_subject(
        self, org_id: str, content_id: str, subject: str, run_id: str | None
    ) -> bool:
        key = (org_id, content_id, subject)
        if key in self._subject_view():
            return False
        self.new_subjects[key] = (run_id, False)
        return True

    # -- the run queue (R1.20 PR A): primitives of RunQueueOps ----------------------------------

    def _rq_get(self, table: str, key: tuple[str, ...]) -> dict[str, Any] | None:
        row = self.new_runq.get((table, key)) or self._state.runq.get((table, key))
        return None if row is None else dict(row)

    def _rq_insert(self, table: str, row: dict[str, Any]) -> bool:
        key = (table, tuple(row[c] for c in RQ_KEYS[table]))
        if key in self.new_runq or key in self._state.runq:
            return False
        self.new_runq[key] = dict(row)
        return True

    def _rq_update(self, table: str, row: dict[str, Any]) -> None:
        self.new_runq[(table, tuple(row[c] for c in RQ_KEYS[table]))] = dict(row)

    def _rq_scan(self, table: str, state_in: tuple[str, ...] | None) -> list[dict[str, Any]]:
        merged = {k: v for k, v in self._state.runq.items() if k[0] == table}
        merged.update({k: v for k, v in self.new_runq.items() if k[0] == table})
        return [dict(v) for v in merged.values() if state_in is None or v.get("state") in state_in]

    def _rq_count(self, table: str, org_id: str, state_in: tuple[str, ...] | None) -> int:
        return sum(1 for r in self._rq_scan(table, state_in) if r["org_id"] == org_id)

    def content_subject_rows(self, org_id: str) -> list[ContentSubjectRow]:
        return [
            ContentSubjectRow(key[1], key[2], run_id, pending)
            for key, (run_id, pending) in sorted(self._subject_view().items())
            if key[0] == org_id
        ]

    def erase_content_subject(self, org_id: str, subject: str) -> tuple[int, list[str], int]:
        view = self._subject_view()
        mine = sorted({k[1] for k in view if k[0] == org_id and k[2] == subject})
        if self.legal_hold(org_id):
            for cid in mine:
                run_id, _ = view[(org_id, cid, subject)]
                self.new_subjects[(org_id, cid, subject)] = (run_id, True)
            return 0, [], len(mine)
        kinds: set[str] = set()
        count = 0
        for cid in mine:
            record = self._content((org_id, cid))
            if record is not None:
                kinds.add(record.kind.value)
                count += 1
            self.new_content[(org_id, cid)] = None
            self._drop_subjects(org_id, cid)
        return count, sorted(kinds), 0

    def pending_erasure_orgs(self) -> list[str]:
        return sorted({k[0] for k, (_, pending) in self._subject_view().items() if pending})

    def erase_pending_content(self, org_id: str) -> tuple[int, list[str]]:
        view = self._subject_view()
        due = sorted({k[1] for k, (_, pending) in view.items() if k[0] == org_id and pending})
        kinds: set[str] = set()
        count = 0
        for cid in due:
            record = self._content((org_id, cid))
            if record is not None:
                kinds.add(record.kind.value)
                count += 1
            self.new_content[(org_id, cid)] = None
            self._drop_subjects(org_id, cid)
        return count, sorted(kinds)

    def legal_hold(self, org_id: str) -> bool:
        return self.new_holds.get(org_id, org_id in self._state.holds)

    def delete_if_expired(self, org_id: str, content_id: str, now: datetime) -> bool:
        record = self._content((org_id, content_id))
        held = self.new_holds.get(org_id, org_id in self._state.holds)
        if record is None or held or record.expires_at is None or record.expires_at > now:
            return False
        self.new_content[(org_id, content_id)] = None
        self._drop_subjects(org_id, content_id)
        return True

    def set_legal_hold(self, org_id: str, on: bool) -> None:
        self.new_holds[org_id] = on

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        return self.new_tombstones.get(org_id) or self._state.tombstones.get(org_id)

    def put_key_tombstone(self, record: KeyTombstone) -> bool:
        if self.key_tombstone(record.org_id) is not None:
            return False
        self.new_tombstones[record.org_id] = record
        return True

    def org_has_content(self, org_id: str) -> bool:
        merged = {**self._state.content, **self.new_content}
        return any(k[0] == org_id and v is not None for k, v in merged.items())

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        merged = {**self._state.content, **self.new_content}
        return frozenset(v.key_id for k, v in merged.items() if k[0] == org_id and v is not None)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        events = [e for items in self._state.events.values() for e in items]
        events += [e for items in self.new_events.values() for e in items]
        return keyed_ids_of((e.payload for e in events), org_id)

    def upsert_security_log_count(self, entry: SecurityLogEntry) -> int:
        log = self._auth().security_log
        key = (entry.org_id, entry.id)
        stored = log.get(key)
        if stored is None:
            log[key] = entry
            return int(entry.details.get("count", 1) or 1)
        count = bumped(int(stored.details.get("count", 1) or 1))
        log[key] = stored.model_copy(update={"details": {**stored.details, "count": count}})
        return count

    def put_reservation(self, reservation: Reservation, *, expected_version: int | None) -> int:
        key = reservation.id
        current = self.get_reservation(key)
        version = _next_version(
            "reservation", key, None if current is None else current.version, expected_version
        )
        self.new_reservations[key] = StoredReservation(reservation=reservation, version=version)
        return version

    def _users(self, org_id: str) -> list[User]:
        merged = {k: u for k, u in self._state.users.items() if k[0] == org_id}
        merged.update({k: u for k, u in self.new_users.items() if k[0] == org_id})
        return list(merged.values())

    def create_user(self, user: User) -> None:
        if self.get_user(user.org_id, user.id) is not None:
            raise AlreadyExistsError(f"user '{user.id}' already exists")
        if user.email is not None and self.get_user_by_email(user.org_id, user.email) is not None:
            raise AlreadyExistsError("a user with this email already exists")
        self.new_users[(user.org_id, user.id)] = user
        self._auth().user_keys[(user.org_id, user.id)] = new_identifier_key()

    def get_user(self, org_id: str, user_id: str) -> User | None:
        key = (org_id, user_id)
        return self.new_users.get(key) or self._state.users.get(key)

    def list_users(self, org_id: str) -> list[User]:
        merged = {**self._state.users, **self.new_users}
        return sorted((u for k, u in merged.items() if k[0] == org_id), key=lambda u: u.id)

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        return next((u for u in self._users(org_id) if u.email == wanted), None)

    def erase_user(self, org_id: str, user_id: str, at: datetime) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self.new_users[(org_id, user_id)] = erased_copy(user, at)
        self.new_credentials[(org_id, user_id)] = None
        self._auth().user_keys.pop((org_id, user_id), None)  # crypto-shredding (ND60)
        self.purge_idempotency_actor(org_id, user_id)  # a user id is the actor id (T42)
        return True

    def set_user_role(self, org_id: str, user_id: str, role: Role) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self.new_users[(org_id, user_id)] = user.model_copy(update={"role": role})
        return True

    def set_user_email_verified(self, org_id: str, user_id: str, at: datetime, via: str) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self.new_users[(org_id, user_id)] = user.model_copy(
            update={"email_verified_at": at, "email_verified_via": via}
        )
        return True

    def set_user_email(self, org_id: str, user_id: str, email: str, at: datetime) -> None:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            raise NotFoundError(f"no active user '{user_id}'")
        holder = self.get_user_by_email(org_id, email)
        if holder is not None and holder.id != user_id:
            raise AlreadyExistsError("a user with this email already exists")
        self.new_users[(org_id, user_id)] = user.model_copy(
            update={"email": email, "email_verified_at": at, "email_verified_via": "link"}
        )

    def put_email_change(self, change: EmailChange) -> None:
        found = self._auth().email_changes
        if (change.org_id, change.token_hash) in found:
            raise AlreadyExistsError("this email change token already exists")
        found[(change.org_id, change.token_hash)] = change

    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        return self._auth().email_changes.get((org_id, token_hash))

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        found = [
            c
            for c in self._auth().email_changes.values()
            if c.org_id == org_id and c.user_id == user_id
        ]
        return sorted(found, key=lambda c: (c.created_at, c.token_hash))

    def cancel_email_changes(
        self, org_id: str, user_id: str, at: datetime, keep_hash: str | None = None
    ) -> int:
        found = self._auth().email_changes
        changed = 0
        for key, c in list(found.items()):
            if (
                c.org_id == org_id
                and c.user_id == user_id
                and c.token_hash != keep_hash
                and c.consumed_at is None
                and c.cancelled_at is None
            ):
                found[key] = c.model_copy(update={"cancelled_at": at})
                changed += 1
        return changed

    def consume_email_change(self, org_id: str, token_hash: str, at: datetime) -> bool:
        found = self._auth().email_changes
        c = found.get((org_id, token_hash))
        if c is None or c.consumed_at is not None or c.cancelled_at is not None:
            return False
        found[(org_id, token_hash)] = c.model_copy(update={"consumed_at": at})
        return True

    def purge_email_changes(self, now: datetime) -> int:
        found = self._auth().email_changes
        due = [
            k for k, c in found.items() if email_change_ended_at(c) + EMAIL_CHANGE_RETENTION <= now
        ]
        for key in due:
            del found[key]
        return len(due)

    def delete_email_changes_for_user(self, org_id: str, user_id: str) -> int:
        found = self._auth().email_changes
        due = [k for k, c in found.items() if c.org_id == org_id and c.user_id == user_id]
        for key in due:
            del found[key]
        return len(due)

    def put_invite(self, invite: Invite) -> None:
        found = self._auth().invites
        if (invite.org_id, invite.id) in found or any(
            i.org_id == invite.org_id and i.token_hash == invite.token_hash for i in found.values()
        ):
            raise AlreadyExistsError("an invite with this id or token already exists")
        found[(invite.org_id, invite.id)] = invite

    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        return self._auth().invites.get((org_id, invite_id))

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        return next(
            (
                i
                for i in self._auth().invites.values()
                if i.org_id == org_id and i.token_hash == token_hash
            ),
            None,
        )

    def list_invites(self, org_id: str) -> list[Invite]:
        found = [i for i in self._auth().invites.values() if i.org_id == org_id]
        return sorted(found, key=lambda i: (i.created_at, i.id), reverse=True)

    def consume_invite(
        self, org_id: str, invite_id: str, token_hash: str, at: datetime, user_id: str
    ) -> bool:
        found = self._auth().invites
        current = found.get((org_id, invite_id))
        if (
            current is None
            or current.token_hash != token_hash
            or current.accepted_at is not None
            or current.revoked_at is not None
        ):
            return False
        found[(org_id, invite_id)] = current.model_copy(
            update={"accepted_at": at, "accepted_user_id": user_id, "email": None}
        )
        return True

    def revoke_invite(self, org_id: str, invite_id: str, at: datetime, reason: str) -> bool:
        found = self._auth().invites
        current = found.get((org_id, invite_id))
        if current is None or current.accepted_at is not None or current.revoked_at is not None:
            return False
        found[(org_id, invite_id)] = current.model_copy(
            update={"revoked_at": at, "revoke_reason": reason}
        )
        return True

    def resend_invite(
        self, org_id: str, invite_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        found = self._auth().invites
        current = found.get((org_id, invite_id))
        if current is None or current.accepted_at is not None or current.revoked_at is not None:
            return False
        found[(org_id, invite_id)] = current.model_copy(
            update={
                "token_hash": token_hash,
                "expires_at": expires_at,
                "resent_count": current.resent_count + 1,
            }
        )
        return True

    def purge_invites(self, now: datetime) -> int:
        found = self._auth().invites
        due = [k for k, i in found.items() if invite_ended_at(i) + INVITE_RETENTION <= now]
        for key in due:
            del found[key]
        return len(due)

    def detach_user_from_invites(self, org_id: str, user_id: str) -> int:
        found = self._auth().invites
        changed = 0
        for key, inv in list(found.items()):
            if inv.org_id != org_id:
                continue
            update: dict[str, object] = {}
            if inv.accepted_user_id == user_id:
                update["accepted_user_id"] = None
            if inv.created_by == user_id:
                update["created_by"] = ERASED_CREATOR
            if update:
                found[key] = inv.model_copy(update=update)
                changed += 1
        return changed

    # -- password reset (R1.41) ----------------------------------------------------------

    def put_reset_token(self, token: ResetToken) -> None:
        found = self._auth().reset_tokens
        if (token.org_id, token.token_hash) in found:
            raise AlreadyExistsError("a reset token with this hash already exists")
        found[(token.org_id, token.token_hash)] = token

    def consume_reset_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        found = self._auth().reset_tokens
        row = found.get((org_id, token_hash))
        if (
            row is None
            or row.consumed_at is not None
            or row.revoked_at is not None
            or row.expires_at <= at
        ):
            return False
        found[(org_id, token_hash)] = row.model_copy(update={"consumed_at": at})
        return True

    def revoke_reset_tokens(
        self, org_id: str, user_id: str, at: datetime, *, keep: str | None = None
    ) -> int:
        found = self._auth().reset_tokens
        count = 0
        for key, row in list(found.items()):
            if (
                row.org_id == org_id
                and row.user_id == user_id
                and row.token_hash != keep
                and row.consumed_at is None
                and row.revoked_at is None
            ):
                found[key] = row.model_copy(update={"revoked_at": at})
                count += 1
        return count

    def put_revoke_link(self, link: RevokeLink) -> None:
        found = self._auth().revoke_links
        if (link.org_id, link.token_hash) in found:
            raise AlreadyExistsError("a revoke link with this hash already exists")
        found[(link.org_id, link.token_hash)] = link

    def consume_revoke_link(self, org_id: str, token_hash: str, at: datetime) -> bool:
        found = self._auth().revoke_links
        row = found.get((org_id, token_hash))
        if (
            row is None
            or row.consumed_at is not None
            or row.revoked_at is not None
            or row.expires_at <= at
        ):
            return False
        found[(org_id, token_hash)] = row.model_copy(update={"consumed_at": at})
        return True

    def revoke_revoke_links(self, org_id: str, user_id: str, at: datetime) -> int:
        found = self._auth().revoke_links
        count = 0
        for key, row in list(found.items()):
            if (
                row.org_id == org_id
                and row.user_id == user_id
                and row.consumed_at is None
                and row.revoked_at is None
            ):
                found[key] = row.model_copy(update={"revoked_at": at})
                count += 1
        return count

    def set_password_login_block(self, org_id: str, user_id: str, at: datetime, via: str) -> None:
        self._auth().login_blocks[(org_id, user_id)] = PasswordLoginBlock(
            org_id=org_id,
            user_id=user_id,
            blocked_at=at,
            via=via,  # type: ignore[arg-type]
        )

    def clear_password_login_block(self, org_id: str, user_id: str) -> bool:
        return self._auth().login_blocks.pop((org_id, user_id), None) is not None

    def purge_password_reset(self, now: datetime) -> int:
        state = self._auth()
        tokens = [k for k, r in state.reset_tokens.items() if reset_retention_end(r) <= now]
        links = [k for k, r in state.revoke_links.items() if revoke_link_retention_end(r) <= now]
        for key in tokens:
            del state.reset_tokens[key]
        for lkey in links:
            del state.revoke_links[lkey]
        return len(tokens) + len(links)

    def erase_password_reset(self, org_id: str, user_id: str) -> int:
        state = self._auth()
        changed = 0
        for tkey, token in list(state.reset_tokens.items()):
            if token.org_id != org_id:
                continue
            if token.user_id == user_id:
                del state.reset_tokens[tkey]
                changed += 1
            elif token.created_by == user_id:
                state.reset_tokens[tkey] = token.model_copy(update={"created_by": ERASED_CREATOR})
                changed += 1
        for lkey, link in list(state.revoke_links.items()):
            if link.org_id == org_id and link.user_id == user_id:
                del state.revoke_links[lkey]
                changed += 1
        if state.login_blocks.pop((org_id, user_id), None) is not None:
            changed += 1
        return changed

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        return self._auth().reset_tokens.get((org_id, token_hash))

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        found = [r for r in self._auth().reset_tokens.values() if r.org_id == org_id]
        return sorted(found, key=lambda r: (r.created_at, r.token_hash))

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        return self._auth().revoke_links.get((org_id, token_hash))

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        found = [r for r in self._auth().revoke_links.values() if r.org_id == org_id]
        return sorted(found, key=lambda r: (r.created_at, r.token_hash))

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        return self._auth().login_blocks.get((org_id, user_id))

    def adopt_security_log_rows(self, org_id: str, email: str, user_id: str) -> int:
        log = self._auth().security_log
        changed = 0
        for key, entry in list(log.items()):
            if (
                entry.org_id == org_id
                and entry.subject is None
                and entry.details.get("email") == email
            ):
                log[key] = entry.model_copy(update={"subject": user_id})
                changed += 1
        return changed

    def set_user_disabled(self, org_id: str, user_id: str, at: datetime | None) -> bool:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return False
        self.new_users[(org_id, user_id)] = user.model_copy(update={"disabled_at": at})
        return True

    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        key = (org_id, user_id)
        if key in self.new_credentials:
            return self.new_credentials[key]
        return self._state.credentials.get(key)

    def put_credential(self, credential: Credential) -> None:
        user = self.get_user(credential.org_id, credential.user_id)
        if user is None or user.erased:
            raise NotFoundError(f"no active user '{credential.user_id}'")
        self.new_credentials[(credential.org_id, credential.user_id)] = credential

    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        key = (org_id, token_hash)
        return self.new_tokens.get(key) or self._state.setup_tokens.get(key)

    def put_setup_token(self, token: SetupToken) -> None:
        if self.get_setup_token(token.org_id, token.token_hash) is not None:
            raise AlreadyExistsError("this setup token already exists")
        self.new_tokens[(token.org_id, token.token_hash)] = token

    def consume_setup_token(self, org_id: str, token_hash: str, now: datetime) -> bool:
        token = self.get_setup_token(org_id, token_hash)
        if token is None or token.consumed_at is not None or now >= token.expires_at:
            return False
        self.new_tokens[(org_id, token_hash)] = token.model_copy(update={"consumed_at": now})
        return True

    def _auth(self) -> _State:
        if self.auth is None:
            self.auth = _State(
                auth_sessions=dict(self._state.auth_sessions),
                auth_tokens=dict(self._state.auth_tokens),
                auth_failures=dict(self._state.auth_failures),
                mfa_factors=dict(self._state.mfa_factors),
                mfa_codes=dict(self._state.mfa_codes),
                mfa_challenges=dict(self._state.mfa_challenges),
                org_auth=dict(self._state.org_auth),
                security_log=dict(self._state.security_log),
                user_keys=dict(self._state.user_keys),
                org_keys=dict(self._state.org_keys),
                api_tokens=dict(self._state.api_tokens),
                retired_names=dict(self._state.retired_names),
                idem=dict(self._state.idem),
                extension=dict(self._state.extension),
                agent_credentials=dict(self._state.agent_credentials),
                invites=dict(self._state.invites),
                email_changes=dict(self._state.email_changes),
                reset_tokens=dict(self._state.reset_tokens),
                revoke_links=dict(self._state.revoke_links),
                login_blocks=dict(self._state.login_blocks),
            )
        return self.auth

    def put_auth_session(self, session: AuthSession) -> None:
        session.check_login_via()
        sessions = self._auth().auth_sessions
        if (session.org_id, session.id) in sessions:
            raise AlreadyExistsError("this session already exists")
        sessions[(session.org_id, session.id)] = session

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        return self._auth().auth_sessions.get((org_id, session_id))

    def touch_auth_session(self, org_id: str, session_id: str, at: datetime) -> None:
        sessions = self._auth().auth_sessions
        current = sessions.get((org_id, session_id))
        if current is not None:
            sessions[(org_id, session_id)] = current.model_copy(update={"last_active_at": at})

    def set_auth_session_step_up(self, org_id: str, session_id: str, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("step-up time must carry a timezone")
        sessions = self._auth().auth_sessions
        current = sessions.get((org_id, session_id))
        if current is None or current.revoked_at is not None:
            return False
        sessions[(org_id, session_id)] = current.model_copy(update={"step_up_at": at})
        return True

    def revoke_auth_session(self, org_id: str, session_id: str, at: datetime, reason: str) -> bool:
        sessions = self._auth().auth_sessions
        current = sessions.get((org_id, session_id))
        if current is None or current.revoked_at is not None:
            return False
        sessions[(org_id, session_id)] = AuthSession.from_stored(
            {**current.model_dump(), "revoked_at": at, "revoke_reason": reason}
        )
        return True

    def put_auth_token(self, token: AuthToken) -> None:
        tokens = self._auth().auth_tokens
        if (token.org_id, token.token_hash) in tokens:
            raise AlreadyExistsError("this token already exists")
        tokens[(token.org_id, token.token_hash)] = token

    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        return self._auth().auth_tokens.get((org_id, token_hash))

    def use_auth_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        tokens = self._auth().auth_tokens
        current = tokens.get((org_id, token_hash))
        if current is None or current.used_at is not None:
            return False
        tokens[(org_id, token_hash)] = current.model_copy(update={"used_at": at})
        return True

    def put_auth_failure(self, failure: AuthFailure) -> None:
        self._auth().auth_failures[failure.key] = failure

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        return self._auth().auth_failures.get(key)

    def delete_auth_failure(self, key: str) -> None:
        self._auth().auth_failures.pop(key, None)

    def purge_auth_records(self, sessions_ended_before: datetime, failures_before: datetime) -> int:
        auth = self._auth()
        gone = {k for k, v in auth.auth_sessions.items() if v.ended_at <= sessions_ended_before}
        for key in gone:
            del auth.auth_sessions[key]
        stale = [k for k, t in auth.auth_tokens.items() if (t.org_id, t.session_id) in gone]
        for tkey in stale:
            del auth.auth_tokens[tkey]
        old = [
            k
            for k, f in auth.auth_failures.items()
            if max(f.last_failure_at, f.locked_until or f.last_failure_at) <= failures_before
        ]
        for fkey in old:
            del auth.auth_failures[fkey]
        return len(gone) + len(stale) + len(old)

    def set_auth_session_password_confirmed(
        self, org_id: str, session_id: str, at: datetime
    ) -> bool:
        sessions = self._auth().auth_sessions
        current = sessions.get((org_id, session_id))
        if current is None:
            return False
        sessions[(org_id, session_id)] = current.model_copy(update={"password_confirmed_at": at})
        return True

    def clear_session_proofs(self, org_id: str, user_id: str) -> int:
        sessions = self._auth().auth_sessions
        cleared = 0
        for key, current in list(sessions.items()):
            if key[0] == org_id and current.user_id == user_id:
                if current.password_confirmed_at is not None or current.step_up_at is not None:
                    sessions[key] = current.model_copy(
                        update={"password_confirmed_at": None, "step_up_at": None}
                    )
                    cleared += 1
        return cleared

    # -- MFA (R1.38) --

    def set_auth_session_mfa(self, org_id: str, session_id: str, at: datetime, method: str) -> bool:
        sessions = self._auth().auth_sessions
        current = sessions.get((org_id, session_id))
        if current is None:
            return False
        sessions[(org_id, session_id)] = AuthSession.from_stored(
            {**current.model_dump(), "mfa_verified_at": at, "mfa_method": method}
        )
        return True

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        return self._auth().mfa_factors.get((org_id, user_id))

    def put_mfa_factor(self, factor: MfaFactor) -> None:
        factors = self._auth().mfa_factors
        current = factors.get((factor.org_id, factor.user_id))
        if current is not None and current.active:
            raise AlreadyExistsError("this person already has an active factor")
        factors[(factor.org_id, factor.user_id)] = factor

    def activate_mfa_factor(
        self, org_id: str, user_id: str, factor_id: str, at: datetime, step: int | None
    ) -> bool:
        factors = self._auth().mfa_factors
        current = factors.get((org_id, user_id))
        if current is None or current.id != factor_id or current.confirmed_at is not None:
            return False
        factors[(org_id, user_id)] = current.model_copy(
            update={"confirmed_at": at, "last_used_step": step}
        )
        return True

    def advance_mfa_step(self, org_id: str, user_id: str, factor_id: str, step: int) -> bool:
        factors = self._auth().mfa_factors
        current = factors.get((org_id, user_id))
        if current is None or current.id != factor_id:
            return False
        if current.last_used_step is not None and current.last_used_step >= step:
            return False
        factors[(org_id, user_id)] = current.model_copy(update={"last_used_step": step})
        return True

    def delete_mfa_factor(self, org_id: str, user_id: str) -> bool:
        auth = self._auth()
        existed = auth.mfa_factors.pop((org_id, user_id), None) is not None
        for key in [k for k in auth.mfa_codes if k[:2] == (org_id, user_id)]:
            del auth.mfa_codes[key]
        return existed

    def reseal_mfa_factor(
        self,
        org_id: str,
        user_id: str,
        factor_id: str,
        from_key_id: str,
        secret_enc: bytes,
        key_id: str,
    ) -> bool:
        factors = self._auth().mfa_factors
        current = factors.get((org_id, user_id))
        if current is None or current.id != factor_id or current.key_id != from_key_id:
            return False
        factors[(org_id, user_id)] = current.model_copy(
            update={"secret_enc": secret_enc, "key_id": key_id}
        )
        return True

    def delete_mfa_challenges(self, org_id: str, user_id: str) -> int:
        auth = self._auth()
        gone = [
            k for k, c in auth.mfa_challenges.items() if k[0] == org_id and c.user_id == user_id
        ]
        for key in gone:
            del auth.mfa_challenges[key]
        return len(gone)

    def erase_mfa_user(self, org_id: str, user_id: str) -> bool:
        auth = self._auth()
        existed = self.delete_mfa_factor(org_id, user_id)
        for ckey in [
            k for k, c in auth.mfa_challenges.items() if k[0] == org_id and c.user_id == user_id
        ]:
            del auth.mfa_challenges[ckey]
        current = auth.org_auth.get(org_id)
        if current is not None and current.updated_by == user_id:
            auth.org_auth[org_id] = current.model_copy(update={"updated_by": "erased-user"})
        return existed

    def delete_all_mfa(self, org_id: str) -> int:
        auth = self._auth()
        removed = 0
        for fkey in [k for k in auth.mfa_factors if k[0] == org_id]:
            del auth.mfa_factors[fkey]
            removed += 1
        for ckey in [k for k in auth.mfa_codes if k[0] == org_id]:
            del auth.mfa_codes[ckey]
        for tkey in [k for k in auth.mfa_challenges if k[0] == org_id]:
            del auth.mfa_challenges[tkey]
        return removed

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        return sum(
            1
            for k, (_, used) in self._auth().mfa_codes.items()
            if k[:2] == (org_id, user_id) and used is None
        )

    def replace_mfa_recovery_codes(
        self, org_id: str, user_id: str, code_hashes: Sequence[str], at: datetime
    ) -> None:
        codes = self._auth().mfa_codes
        for key in [k for k in codes if k[:2] == (org_id, user_id)]:
            del codes[key]
        for code_hash in code_hashes:
            codes[(org_id, user_id, code_hash)] = (at, None)

    def use_mfa_recovery_code(
        self, org_id: str, user_id: str, code_hash: str, at: datetime
    ) -> bool:
        codes = self._auth().mfa_codes
        current = codes.get((org_id, user_id, code_hash))
        if current is None or current[1] is not None:
            return False
        codes[(org_id, user_id, code_hash)] = (current[0], at)
        return True

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        return self._auth().mfa_challenges.get((org_id, token_hash))

    def put_mfa_challenge(self, challenge: MfaChallenge) -> None:
        challenges = self._auth().mfa_challenges
        if (challenge.org_id, challenge.token_hash) in challenges:
            raise AlreadyExistsError("this challenge already exists")
        challenges[(challenge.org_id, challenge.token_hash)] = challenge

    def consume_mfa_challenge(self, org_id: str, token_hash: str, now: datetime) -> bool:
        challenges = self._auth().mfa_challenges
        current = challenges.get((org_id, token_hash))
        if current is None or current.consumed_at is not None or now >= current.expires_at:
            return False
        challenges[(org_id, token_hash)] = current.model_copy(update={"consumed_at": now})
        return True

    def add_mfa_challenge_attempt(self, org_id: str, token_hash: str) -> int | None:
        challenges = self._auth().mfa_challenges
        current = challenges.get((org_id, token_hash))
        if current is None:
            return None
        challenges[(org_id, token_hash)] = current.model_copy(
            update={"attempts": current.attempts + 1}
        )
        return current.attempts + 1

    def set_mfa_challenge_factor(self, org_id: str, token_hash: str, factor_id: str) -> bool:
        challenges = self._auth().mfa_challenges
        current = challenges.get((org_id, token_hash))
        if current is None or current.factor_id is not None:
            return False
        challenges[(org_id, token_hash)] = current.model_copy(update={"factor_id": factor_id})
        return True

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        return self._auth().org_auth.get(org_id) or OrgAuthSettings(org_id=org_id)

    def put_org_auth_settings(self, settings: OrgAuthSettings) -> None:
        self._auth().org_auth[settings.org_id] = settings

    def purge_mfa_records(self, now: datetime) -> int:
        auth = self._auth()
        old = [k for k, c in auth.mfa_challenges.items() if c.purge_at() <= now]
        for key in old:
            del auth.mfa_challenges[key]
        stale = [
            k
            for k, f in auth.mfa_factors.items()
            if f.confirmed_at is None and now >= f.created_at + MFA_PENDING_LIFETIME
        ]
        for fkey in stale:
            del auth.mfa_factors[fkey]
        return len(old) + len(stale)

    # -- idempotency keys (R1.35) --

    @staticmethod
    def _idem_key(scope: Scope, key_hash: str) -> tuple[str, str, str, str, str]:
        return (scope.org_id, scope.actor, scope.method, scope.route, key_hash)

    def idempotency_get(self, scope: Scope, key_hash: str) -> StoredIdempotency | None:
        return self._auth().idem.get(self._idem_key(scope, key_hash))

    def idempotency_put(self, row: StoredIdempotency) -> None:
        self._auth().idem[self._idem_key(row.scope, row.key_hash)] = row

    def idempotency_delete(self, scope: Scope, key_hash: str) -> bool:
        return self._auth().idem.pop(self._idem_key(scope, key_hash), None) is not None

    def purge_idempotency_org(self, org_id: str) -> int:
        rows = self._auth().idem
        gone = [k for k in rows if k[0] == org_id]
        for k in gone:
            del rows[k]
        return len(gone)

    def purge_idempotency_actor(self, org_id: str, person: str) -> int:
        rows = self._auth().idem
        gone = [
            k
            for k, r in rows.items()
            if k[0] == org_id and (person == k[1] or person in r.subjects)
        ]
        for k in gone:
            del rows[k]
        return len(gone)

    def purge_idempotency_expired(self, now: datetime, limit: int) -> int:
        rows = self._auth().idem
        due = sorted((r.expires_at, k) for k, r in rows.items() if r.expires_at <= now)[:limit]
        for _, k in due:
            del rows[k]
        return len(due)

    def idempotency_rows(self, org_id: str) -> list[StoredIdempotency]:
        rows = self._auth().idem
        return [rows[k] for k in sorted(rows) if k[0] == org_id]

    def append_security_log(self, entry: SecurityLogEntry) -> None:
        log = self._auth().security_log
        if (entry.org_id, entry.id) in log:
            raise AlreadyExistsError("this security log row already exists")
        log[(entry.org_id, entry.id)] = entry

    def get_security_log(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        return self._auth().security_log.get((org_id, row_id))

    def take_security_log_count(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        return self._auth().security_log.pop((org_id, row_id), None)

    def list_expired_security_log(self, now: datetime, reason: str) -> list[SecurityLogEntry]:
        return [
            e
            for e in self._auth().security_log.values()
            if e.expires_at <= now and e.details.get("reason") == reason
        ]

    def add_security_log_count(
        self, org_id: str, row_id: str, n: int, *, create: SecurityLogEntry | None = None
    ) -> int:
        if n < 1:
            raise StoreError("the count to add must be at least 1")
        log = self._auth().security_log
        stored = log.get((org_id, row_id))
        if stored is None:
            if create is None:
                raise StoreError("no such security log row to add to")
            count = min(n, MAX_COUNT)
            log[(org_id, row_id)] = create.model_copy(
                update={"details": {**create.details, "count": count}}
            )
            return count
        count = min(int(stored.details.get("count", 1) or 1) + n, MAX_COUNT)
        details = {**stored.details, "count": count}
        log[(org_id, row_id)] = stored.model_copy(update={"details": details})
        return count

    def purge_security_log(self, now: datetime) -> int:
        log = self._auth().security_log
        due = [
            k
            for k, e in log.items()
            if e.expires_at <= now and e.details.get("reason") != HALT_PENDING_REASON
        ]
        for key in due:
            del log[key]
        return len(due)

    def user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        user = self.get_user(org_id, user_id)
        if user is None or user.erased:
            return None
        keys = self._auth().user_keys
        return keys.setdefault((org_id, user_id), new_identifier_key())

    def org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        keys = self._auth().org_keys
        if (org_id, epoch) not in keys:
            if any(k[0] == org_id and k[1] > epoch for k in keys):
                raise StoreError("no identifier key is made for an epoch older than the newest")
            keys[(org_id, epoch)] = new_identifier_key()
        return keys[(org_id, epoch)]

    def destroy_org_identifier_keys(self, before_epoch: int) -> int:
        keys = self._auth().org_keys
        due = [k for k, v in keys.items() if k[1] < before_epoch and v is not None]
        for key in due:
            keys[key] = None
        return len(due)

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        return self._auth().org_keys.get((org_id, epoch))

    def erase_security_log(self, org_id: str, user_id: str, email: str | None) -> int:
        log = self._auth().security_log
        theirs = [
            k
            for k, e in log.items()
            if k[0] == org_id
            and (e.subject == user_id or (email is not None and e.details.get("email") == email))
        ]
        for key in theirs:
            del log[key]
        for key, entry in list(log.items()):
            if key[0] == org_id and entry.actor == user_id:  # only the actor: kept, stripped
                kept = {k: v for k, v in entry.details.items() if k not in ERASED_DETAIL_KEYS}
                log[key] = entry.model_copy(update={"actor": ERASED_ACTOR, "details": kept})
        return len(theirs)

    def put_api_token(self, token: ApiToken) -> None:
        tokens = self._auth().api_tokens
        if any(
            k[0] == token.org_id
            and (k[1] == token.id or t.name == token.name or t.token_hash == token.token_hash)
            for k, t in tokens.items()
        ):
            raise AlreadyExistsError("this token already exists")
        tokens[(token.org_id, token.id)] = token

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        return self._auth().api_tokens.get((org_id, token_id))

    def revoke_api_token(self, org_id: str, token_id: str, at: datetime, by: str) -> bool:
        tokens = self._auth().api_tokens
        current = tokens.get((org_id, token_id))
        if current is None or current.revoked_at is not None:
            return False
        tokens[(org_id, token_id)] = ApiToken.model_validate(
            {**current.model_dump(), "revoked_at": at, "revoked_by": by}
        )
        return True

    def rename_api_token(self, org_id: str, token_id: str, name: str) -> bool:
        tokens = self._auth().api_tokens
        current = tokens.get((org_id, token_id))
        if current is None:
            return False
        if any(k[0] == org_id and k[1] != token_id and t.name == name for k, t in tokens.items()):
            raise AlreadyExistsError("a token with this name exists")
        tokens[(org_id, token_id)] = ApiToken.model_validate({**current.model_dump(), "name": name})
        return True

    def delete_api_token(self, org_id: str, token_id: str, now: datetime) -> bool:
        auth = self._auth()
        current = auth.api_tokens.get((org_id, token_id))
        if current is None or (current.revoked_at is None and now <= current.expires_at):
            return False
        del auth.api_tokens[(org_id, token_id)]
        for key, link in list(auth.revoke_links.items()):
            if link.org_id == org_id and link.api_token_id == token_id:
                del auth.revoke_links[key]
        return True

    def _rewrite_issuer(self, org_id: str, token_id: str, *, created: bool, revoked: bool) -> int:
        tokens = self._auth().api_tokens
        current = tokens[(org_id, token_id)]
        changes: dict[str, Any] = {}
        if created:
            taken = {t.name for k, t in tokens.items() if k[0] == org_id and k[1] != token_id}
            changes["created_by"] = ERASED_ACTOR
            changes["name"] = next_erased_token_name(token_id, taken, current=current.name)
        if revoked:
            changes["revoked_by"] = ERASED_ACTOR
        tokens[(org_id, token_id)] = ApiToken.model_validate({**current.model_dump(), **changes})
        return 1

    def erase_api_token_issuer(self, org_id: str, user_id: str) -> int:
        tokens = self._auth().api_tokens
        changed = 0
        for key in sorted(k for k in tokens if k[0] == org_id):
            row = tokens[key]
            if user_id in (row.created_by, row.revoked_by):
                changed += self._rewrite_issuer(
                    org_id,
                    key[1],
                    created=row.created_by == user_id,
                    revoked=row.revoked_by == user_id,
                )
        return changed

    def _person_gone(self, org_id: str, who: str | None) -> bool:
        if who is None or not who.startswith("human:"):
            return False
        user = self.get_user(org_id, who)
        return user is None or user.erased

    def normalise_api_token_issuer(self, org_id: str, token_id: str) -> int:
        row = self._auth().api_tokens.get((org_id, token_id))
        if row is None:
            return 0
        created = self._person_gone(org_id, row.created_by)
        revoked = self._person_gone(org_id, row.revoked_by)
        if not (created or revoked):
            return 0
        return self._rewrite_issuer(org_id, token_id, created=created, revoked=revoked)

    def api_token_name_retired(self, org_id: str, keyed: Sequence[tuple[int, str]]) -> bool:
        retired = self._auth().retired_names
        return any((org_id, epoch, name_hash) in retired for epoch, name_hash in keyed)

    def retire_api_token_name(
        self, org_id: str, name_hash: str, epoch: int, retired_at: datetime
    ) -> None:
        self._auth().retired_names.setdefault((org_id, epoch, name_hash), retired_at)

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        return self._auth().extension.get((org_id, namespace, kind, record_id))

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        return _ext_unique(self._auth().extension, org_id, namespace, kind, unique_key)

    def ext_put(self, record: ExtensionRecord) -> None:
        stored = self._auth().extension
        key = (record.org_id, record.namespace, record.kind, record.record_id)
        clash = record.unique_key is not None and _ext_unique(
            stored, record.org_id, record.namespace, record.kind, record.unique_key
        )
        if key in stored or clash:
            raise AlreadyExistsError("this record already exists")
        stored[key] = record

    def ext_erase(self, org_id: str) -> int:
        stored = self._auth().extension
        gone = [k for k, r in stored.items() if k[0] == org_id and r.erasable]
        for k in gone:
            del stored[k]
        return len(gone)

    def put_agent_credential(self, credential: AgentCredential) -> None:
        found = self._auth().agent_credentials
        if any(
            k[0] == credential.org_id
            and (k[1] == credential.id or c.token_hash == credential.token_hash)
            for k, c in found.items()
        ):
            raise AlreadyExistsError("this credential already exists")
        found[(credential.org_id, credential.id)] = credential

    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        return self._auth().agent_credentials.get((org_id, credential_id))

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        found = [c for k, c in self._auth().agent_credentials.items() if k[0] == org_id]
        return sorted(found, key=lambda c: (c.created_at, c.id))

    def revoke_agent_credential(
        self, org_id: str, credential_id: str, at: datetime, by: str
    ) -> bool:
        found = self._auth().agent_credentials
        current = found.get((org_id, credential_id))
        if current is None or current.revoked_at is not None:
            return False
        found[(org_id, credential_id)] = AgentCredential.model_validate(
            {**current.model_dump(), "revoked_at": at, "revoked_by": by}
        )
        return True

    def shorten_agent_credential(
        self, org_id: str, credential_id: str, expires_at: datetime
    ) -> bool:
        found = self._auth().agent_credentials
        current = found.get((org_id, credential_id))
        if current is None or expires_at >= current.expires_at:
            return False
        found[(org_id, credential_id)] = AgentCredential.model_validate(
            {**current.model_dump(), "expires_at": expires_at}
        )
        return True

    def count_live_agent_credentials(self, org_id: str, agent_id: str, now: datetime) -> int:
        return sum(
            1
            for k, c in self._auth().agent_credentials.items()
            if k[0] == org_id and c.agent_id == agent_id and c.active(now)
        )

    def erase_agent_credential_user(
        self, org_id: str, user_id: str, now: datetime
    ) -> list[AgentCredential]:
        found = self._auth().agent_credentials
        live: list[AgentCredential] = []
        for key, c in sorted(found.items(), key=lambda kv: (kv[1].created_at, kv[1].id)):
            if key[0] != org_id or user_id not in (c.created_by, c.revoked_by):
                continue
            if c.created_by == user_id and c.active(now):
                live.append(c)
            changes: dict[str, Any] = {}
            if c.created_by == user_id:
                changes["created_by"] = ERASED_ACTOR
            if c.revoked_by == user_id:
                changes["revoked_by"] = ERASED_ACTOR
            found[key] = AgentCredential.model_validate({**c.model_dump(), **changes})
        return live

    def purge_agent_credentials(self, now: datetime) -> int:
        found = self._auth().agent_credentials
        due = [
            k
            for k, c in found.items()
            if (c.revoked_at is not None or c.expires_at <= now)
            and c.ended_at() + AGENT_CREDENTIAL_ENDED_RETENTION <= now
        ]
        for key in due:
            del found[key]
        return len(due)


class MemoryStore(Store):
    """State and ledger in process memory. Thread-safe; not shared between processes."""

    def __init__(self, *, clock: Clock = utc_now) -> None:
        super().__init__(clock=clock)
        self._state = _State()
        self._lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("units of work don't nest")
        with self._lock:
            self._local.active = True
            try:
                unit = _MemoryUnitOfWork(self._state, self._clock)
                yield unit  # an exception here skips the commit below
                for stream, events in unit.new_events.items():
                    self._state.events.setdefault(stream, []).extend(events)
                    self._state.positions.update(unit.new_positions)
                    self._state.position = unit.position
                    self._state.budget_definitions.extend(
                        e for e in events if is_budget_definition(e)
                    )
                self._state.projection.merge(unit.new_projection)
                self._state.tombstones.update(unit.new_tombstones)
                self._state.tasks.update(unit.new_tasks)
                self._state.balances.update(unit.new_balances)
                self._state.reservations.update(unit.new_reservations)
                self._state.approvals.update(unit.new_approvals)
                for approval_id in unit.deleted_approvals:
                    self._state.approvals.pop(approval_id, None)
                for key, record in unit.new_content.items():
                    if record is None:
                        self._state.content.pop(key, None)
                    else:
                        self._state.content[key] = record
                for skey, tagged in unit.new_subjects.items():
                    if tagged is None:
                        self._state.subjects.pop(skey, None)
                    else:
                        self._state.subjects[skey] = tagged
                self._state.runq.update(unit.new_runq)
                for org, on in unit.new_holds.items():
                    (self._state.holds.add if on else self._state.holds.discard)(org)
                self._state.users.update(unit.new_users)
                for ckey, credential in unit.new_credentials.items():
                    if credential is None:
                        self._state.credentials.pop(ckey, None)
                    else:
                        self._state.credentials[ckey] = credential
                self._state.setup_tokens.update(unit.new_tokens)
                if unit.auth is not None:
                    self._state.auth_sessions = unit.auth.auth_sessions
                    self._state.auth_tokens = unit.auth.auth_tokens
                    self._state.auth_failures = unit.auth.auth_failures
                    self._state.mfa_factors = unit.auth.mfa_factors
                    self._state.mfa_codes = unit.auth.mfa_codes
                    self._state.mfa_challenges = unit.auth.mfa_challenges
                    self._state.org_auth = unit.auth.org_auth
                    self._state.security_log = unit.auth.security_log
                    self._state.user_keys = unit.auth.user_keys
                    self._state.org_keys = unit.auth.org_keys
                    self._state.api_tokens = unit.auth.api_tokens
                    self._state.retired_names = unit.auth.retired_names
                    self._state.idem = unit.auth.idem
                    self._state.extension = unit.auth.extension
                    self._state.agent_credentials = unit.auth.agent_credentials
                    self._state.invites = unit.auth.invites
                    self._state.email_changes = unit.auth.email_changes
                    self._state.reset_tokens = unit.auth.reset_tokens
                    self._state.revoke_links = unit.auth.revoke_links
                    self._state.login_blocks = unit.auth.login_blocks
            finally:
                self._local.active = False

    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        with self._lock:
            return self._state.content.get((org_id, content_id))

    def has_content(self, org_id: str) -> bool:
        with self._lock:
            return any(k[0] == org_id and v is not None for k, v in self._state.content.items())

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(
                v.key_id for k, v in self._state.content.items() if k[0] == org_id and v is not None
            )

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        with self._lock:
            events = [e for items in self._state.events.values() for e in items]
        return keyed_ids_of((e.payload for e in events), org_id)

    def get_user(self, org_id: str, user_id: str) -> User | None:
        with self._lock:
            return self._state.users.get((org_id, user_id))

    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        try:
            wanted = normalize_email(email)
        except ValueError:
            return None
        with self._lock:
            users = [u for k, u in self._state.users.items() if k[0] == org_id]
        return next((u for u in users if u.email == wanted), None)

    def list_users(self, org_id: str) -> list[User]:
        with self._lock:
            users = [u for k, u in self._state.users.items() if k[0] == org_id]
        return sorted(users, key=lambda u: u.id)

    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        with self._lock:
            return self._state.credentials.get((org_id, user_id))

    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        with self._lock:
            return self._state.setup_tokens.get((org_id, token_hash))

    def list_setup_tokens(self, org_id: str) -> list[SetupToken]:
        with self._lock:
            found = [t for k, t in self._state.setup_tokens.items() if k[0] == org_id]
        return sorted(found, key=lambda t: (t.created_at, t.token_hash))

    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        with self._lock:
            return self._state.auth_sessions.get((org_id, session_id))

    def list_auth_sessions(self, org_id: str) -> list[AuthSession]:
        with self._lock:
            found = [v for k, v in self._state.auth_sessions.items() if k[0] == org_id]
        return sorted(found, key=lambda s: s.id)

    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        with self._lock:
            return self._state.auth_tokens.get((org_id, token_hash))

    def list_auth_tokens(self, org_id: str, session_id: str) -> list[AuthToken]:
        with self._lock:
            found = [
                t
                for k, t in self._state.auth_tokens.items()
                if k[0] == org_id and t.session_id == session_id
            ]
        return sorted(found, key=lambda t: (t.created_at, t.kind.value))

    def get_auth_failure(self, key: str) -> AuthFailure | None:
        with self._lock:
            return self._state.auth_failures.get(key)

    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        with self._lock:
            return self._state.mfa_factors.get((org_id, user_id))

    def list_mfa_factors(self, org_id: str) -> list[MfaFactor]:
        with self._lock:
            return sorted(
                (f for k, f in self._state.mfa_factors.items() if k[0] == org_id),
                key=lambda f: f.user_id,
            )

    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        with self._lock:
            return sum(
                1
                for k, (_, used) in self._state.mfa_codes.items()
                if k[:2] == (org_id, user_id) and used is None
            )

    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        with self._lock:
            return self._state.mfa_challenges.get((org_id, token_hash))

    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        with self._lock:
            return self._state.org_auth.get(org_id) or OrgAuthSettings(org_id=org_id)

    def get_user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        with self._lock:
            return self._state.user_keys.get((org_id, user_id))

    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        with self._lock:
            return self._state.org_keys.get((org_id, epoch))

    def list_security_log(self, org_id: str) -> list[SecurityLogEntry]:
        with self._lock:
            found = [e for k, e in self._state.security_log.items() if k[0] == org_id]
        return sorted(found, key=lambda e: (e.at, e.id))

    def get_api_token_by_hash(self, org_id: str, token_hash: str) -> ApiToken | None:
        with self._lock:
            for (org, _), token in self._state.api_tokens.items():
                if org == org_id and token.token_hash == token_hash:
                    return token
        return None

    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        with self._lock:
            return self._state.api_tokens.get((org_id, token_id))

    def list_api_tokens(self, org_id: str) -> list[ApiToken]:
        with self._lock:
            found = [t for k, t in self._state.api_tokens.items() if k[0] == org_id]
        return sorted(found, key=lambda t: (t.created_at, t.id))

    def api_token_retired_rows(self, org_id: str) -> list[tuple[int, str, datetime]]:
        with self._lock:
            found = [
                (epoch, name_hash, at)
                for (org, epoch, name_hash), at in self._state.retired_names.items()
                if org == org_id
            ]
        return sorted(found, key=lambda r: (r[2], r[0], r[1]))

    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        with self._lock:
            return self._state.extension.get((org_id, namespace, kind, record_id))

    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        with self._lock:
            return _ext_unique(self._state.extension, org_id, namespace, kind, unique_key)

    def ext_list(
        self, org_id: str, namespace: str, kind: str, group_key: str
    ) -> list[ExtensionRecord]:
        with self._lock:
            found = [
                r
                for (org, ns, knd, _), r in self._state.extension.items()
                if (org, ns, knd, r.group_key) == (org_id, namespace, kind, group_key)
            ]
        found.sort(key=lambda r: r.record_id)
        found.sort(key=lambda r: r.sort_key, reverse=True)
        return found

    def count_security_log(
        self, org_id: str, type: str, actor: str | None, start: datetime, end: datetime
    ) -> int:
        with self._lock:
            return sum(
                1
                for (org, _), e in self._state.security_log.items()
                if org == org_id and e.type == type and e.actor == actor and start <= e.at < end
            )

    def get_agent_credential_by_hash(
        self, org_id: str, team: str, token_hash: str
    ) -> AgentCredential | None:
        with self._lock:
            for (org, _), c in self._state.agent_credentials.items():
                if org == org_id and c.team == team and c.token_hash == token_hash:
                    return c
        return None

    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        with self._lock:
            return self._state.agent_credentials.get((org_id, credential_id))

    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        with self._lock:
            return self._state.email_changes.get((org_id, token_hash))

    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        with self._lock:
            found = [
                c
                for c in self._state.email_changes.values()
                if c.org_id == org_id and c.user_id == user_id
            ]
        return sorted(found, key=lambda c: (c.created_at, c.token_hash))

    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        with self._lock:
            return self._state.invites.get((org_id, invite_id))

    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        with self._lock:
            return next(
                (
                    i
                    for i in self._state.invites.values()
                    if i.org_id == org_id and i.token_hash == token_hash
                ),
                None,
            )

    def list_invites(self, org_id: str) -> list[Invite]:
        with self._lock:
            found = [i for i in self._state.invites.values() if i.org_id == org_id]
        return sorted(found, key=lambda i: (i.created_at, i.id), reverse=True)

    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        with self._lock:
            return self._state.reset_tokens.get((org_id, token_hash))

    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        with self._lock:
            found = [r for r in self._state.reset_tokens.values() if r.org_id == org_id]
        return sorted(found, key=lambda r: (r.created_at, r.token_hash))

    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        with self._lock:
            return self._state.revoke_links.get((org_id, token_hash))

    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        with self._lock:
            found = [r for r in self._state.revoke_links.values() if r.org_id == org_id]
        return sorted(found, key=lambda r: (r.created_at, r.token_hash))

    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        with self._lock:
            return self._state.login_blocks.get((org_id, user_id))

    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        with self._lock:
            found = [c for k, c in self._state.agent_credentials.items() if k[0] == org_id]
        return sorted(found, key=lambda c: (c.created_at, c.id))

    def content_subject_schema(self) -> dict[str, list[Any]]:
        return {
            "columns": list(CONTENT_SUBJECT_COLUMNS),
            "indexes": [list(i) for i in CONTENT_SUBJECT_INDEXES],
        }

    def expired_content(self, now: datetime, *, limit: int = 500) -> list[ContentRecord]:
        check_limit(limit)
        with self._lock:
            due = [
                r
                for r in self._state.content.values()
                if r.expires_at is not None
                and r.expires_at <= now
                and r.org_id not in self._state.holds
            ]
        due.sort(key=lambda r: (r.expires_at or now, r.org_id, r.id))
        return due[:limit]

    def expiring_content(
        self, kind: ContentKind, after: datetime, until: datetime
    ) -> list[ContentRecord]:
        with self._lock:
            found = [
                r
                for r in self._state.content.values()
                if r.kind == kind
                and r.expires_at is not None
                and after < r.expires_at <= until
                and r.org_id not in self._state.holds
            ]
        return sorted(found, key=lambda r: (r.expires_at or after, r.org_id, r.id))

    def legal_hold(self, org_id: str) -> bool:
        with self._lock:
            return org_id in self._state.holds

    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        with self._lock:
            return self._state.tombstones.get(org_id)

    def key_tombstones(self) -> list[KeyTombstone]:
        with self._lock:
            return [self._state.tombstones[k] for k in sorted(self._state.tombstones)]

    def shredded_orgs(self) -> list[tuple[str, datetime, str]]:
        with self._lock:
            rows = [
                (stream, e.at, e.actor, json.dumps(thaw(e.payload)))
                for stream, events in self._state.events.items()
                for e in events
                if e.type == "decision.recorded"
            ]
        return shred_entries(rows)

    def store_id(self) -> str:
        return self._state.store_id

    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[LedgerEvent]:
        check_limit(limit)
        with self._lock:
            events = [e for e in self._state.events.get(stream, ()) if e.seq > after_seq]
        return iter(events if limit is None else events[:limit])

    def read_stream_positioned(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[tuple[int, LedgerEvent]]:
        check_limit(limit)
        with self._lock:
            found = [
                (self._state.positions.get((stream, e.seq), 0), e)
                for e in self._state.events.get(stream, ())
                if e.seq > after_seq
            ]
        return iter(found if limit is None else found[:limit])

    def commit_position(self) -> int:
        with self._lock:
            return self._state.position

    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        check_limit(limit)
        with self._lock:
            found = [
                (self._state.positions.get((e.stream, e.seq), 0), e)
                for events in self._state.events.values()
                for e in events
            ]
        found = sorted((item for item in found if item[0] > pos), key=lambda item: item[0])
        return found if limit is None else found[:limit]

    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        with self._lock:
            return _read_projection(
                (self._state.projection,),
                lambda e: self._state.positions.get((e.stream, e.seq), 0),
                org_id,
                kinds,
                projection_streams(streams),
            )

    def _expected_projection(self) -> _Projection:
        expected = _Projection()
        for events in self._state.events.values():
            for event in events:
                expected.add(event)
        return expected

    def projection_problems(self) -> list[str]:
        with self._lock:
            expected, held = self._expected_projection(), self._state.projection
            problems: list[str] = []
            for pair in sorted(expected.entries.keys() | held.entries.keys()):
                want = [e.seq for e in expected.entries.get(pair, ())]
                have = [e.seq for e in held.entries.get(pair, ())]
                if want != have:
                    problems.append(
                        f"{pair[0]} '{pair[1]}': ledger implies {want}, index has {have}"
                    )
            for pair in sorted(expected.counts.keys() | held.counts.keys()):
                if expected.counts.get(pair, 0) != held.counts.get(pair, 0):
                    problems.append(f"{pair[0]} '{pair[1]}': maintained count differs")
            return problems

    def rebuild_projection(self) -> int:
        with self._lock:
            self._state.projection = self._expected_projection()
            return sum(self._state.projection.counts.values())

    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        with self._lock:
            stream = self._state.events.get(run_stream(run_id), ())
            return [e for e in stream if e.task_id == task_id]

    def snapshot(self, stream: str) -> tuple[list[LedgerEvent], StreamHead | None]:
        if getattr(self._local, "active", False):
            raise NestedTransactionError("verify or snapshot outside a unit of work")
        with self._lock:
            return list(self._state.events.get(stream, ())), self.head(stream)

    def head(self, stream: str) -> StreamHead | None:
        with self._lock:
            events = self._state.events.get(stream)
            if not events:
                return None
            last = events[-1]
        return StreamHead(stream=stream, seq=last.seq, hash=last.hash, at=last.at)

    def streams(self) -> list[str]:
        with self._lock:
            return sorted(s for s, events in self._state.events.items() if events)

    def budget_definitions(self) -> list[LedgerEvent]:
        with self._lock:
            found = list(self._state.budget_definitions)
        return sorted(found, key=definition_order)

    def heads(self, only: Collection[str] | None = None) -> dict[str, StreamHead]:
        with self._lock:
            return {
                s: StreamHead(stream=s, seq=e[-1].seq, hash=e[-1].hash, at=e[-1].at)
                for s, e in sorted(self._state.events.items())
                if e and (only is None or s in only)
            }

    def get_task(self, task_id: str) -> StoredTask | None:
        with self._lock:
            return self._state.tasks.get(task_id)

    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        with self._lock:
            found = [t for t in self._state.tasks.values() if t.task.run_id == run_id]
        return sorted(found, key=lambda t: t.task.id)

    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        with self._lock:
            return self._state.balances.get((org_id, budget_id))

    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        with self._lock:
            return self._state.reservations.get(reservation_id)

    def get_approval(self, approval_id: str) -> StoredApproval | None:
        with self._lock:
            return self._state.approvals.get(approval_id)

    def due_approvals(self, now: datetime, *, after_id: str, limit: int) -> list[StoredApproval]:
        with self._lock:
            found = [
                a
                for a in self._state.approvals.values()
                if a.approval.id > after_id and a.approval.retention_over(now)
            ]
        return sorted(found, key=lambda a: a.approval.id)[:limit]

    def approvals(
        self, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[StoredApproval]:
        with self._lock:
            found = [
                a
                for a in self._state.approvals.values()
                if (run_id is None or a.approval.run_id == run_id)
                and (status is None or a.approval.status is status)
            ]
        return sorted(found, key=lambda a: a.approval.id)

    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        with self._lock:
            return [
                self._state.balances[k]
                for k in sorted(self._state.balances)
                if org_id is None or k[0] == org_id
            ]

    def reservations_for_run(
        self, run_id: str, status: ReservationStatus | None = None
    ) -> list[StoredReservation]:
        with self._lock:
            found = [
                r
                for r in self._state.reservations.values()
                if r.reservation.context.run_id == run_id
                and (status is None or r.reservation.status is status)
            ]
        return sorted(found, key=lambda r: r.reservation.id)


def _ext_unique(
    stored: dict[tuple[str, str, str, str], ExtensionRecord],
    org_id: str,
    namespace: str,
    kind: str,
    unique_key: str,
) -> ExtensionRecord | None:
    for (org, ns, knd, _), row in stored.items():
        if (org, ns, knd, row.unique_key) == (org_id, namespace, kind, unique_key):
            return row
    return None
