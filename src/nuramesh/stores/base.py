"""Store interface: state and ledger written together, in one transaction (decision A1).

A `Store` hands out units of work. Everything done through one unit of work (ledger appends and
state changes) commits together when the `with` block ends normally, and none of it is kept if the
block raises. Readers never see half a transaction.

The store, not the caller, assigns each event's sequence number, previous hash and timestamp:
appends read the stream head and write the event in the same transaction, so concurrent writers
can't both claim the same sequence number. Timestamps never go backwards within a stream (if the
clock does, the previous event's time is reused).

Tasks are stored with a version number; `put_task` with a stale `expected_version` raises
`ConcurrencyError` instead of overwriting someone else's change.

Units of work don't nest: opening one inside another on the same thread raises
`NestedTransactionError` (an inner commit can't be undone by an outer failure). Database errors
reach callers as `StoreError` subclasses; `StoreBusyError` means "locked, try again".
"""

import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from nuramesh.core.auth import (
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
)
from nuramesh.core.budgets import BudgetBalance, Reservation, ReservationStatus
from nuramesh.core.content import ContentKind, ContentRecord, KeyTombstone
from nuramesh.core.email_change import EmailChange
from nuramesh.core.idempotency import (
    IdempotencyFenced,
    InProgress,
    Mismatch,
    Outcome,
    Replay,
    Row,
    Scope,
    Sealed,
    Started,
    State,
    decide,
    expiry,
)
from nuramesh.core.identity import Invite
from nuramesh.core.ledger import (
    ChainProblem,
    ChainReport,
    CostBasis,
    EventType,
    LedgerEvent,
    next_event,
    verify_chain,
)
from nuramesh.core.mfa import MfaChallenge, MfaFactor, OrgAuthSettings
from nuramesh.core.money import Money
from nuramesh.core.password_reset import PasswordLoginBlock, ResetToken, RevokeLink
from nuramesh.core.policy import APPROVAL_RETENTION, Approval, ApprovalStatus
from nuramesh.core.projection import projection_key
from nuramesh.core.tasks import Task
from nuramesh.stores.run_queue import (
    RunQueueOps,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The default clock."""
    return datetime.now(UTC)


def due_anchor_day(now: datetime) -> str:
    """The latest anchor date prefix (`YYYY-MM-DD` of the stored text) a due approval can have: the
    UTC date of `now - APPROVAL_RETENTION` plus one day, because the stored text may carry any UTC
    offset (at most a day from the UTC date). A superset filter for the index; the exact rule
    (`Approval.retention_over`) still decides."""
    cutoff = now.astimezone(UTC).replace(microsecond=0) - APPROVAL_RETENTION
    return (cutoff + timedelta(days=1)).date().isoformat()


def run_stream(run_id: str) -> str:
    """The ledger stream of a run."""
    return f"run:{run_id}"


def is_budget_definition(event: LedgerEvent) -> bool:
    """A `budget.defined` entry of an organization stream (`Store.budget_definitions`)."""
    return event.stream.startswith("org:") and event.type == EventType.BUDGET_DEFINED.value


def definition_order(event: LedgerEvent) -> tuple[datetime, str, int]:
    """Earliest first: time, then stream, then sequence (`Store.budget_definitions`)."""
    return (event.at, event.stream, event.seq)


def merged_expiry(old: datetime | None, new: datetime | None) -> datetime | None:
    """The later of two expiries; no expiry (kept) is the latest of all."""
    if old is None or new is None:
        return None
    return max(old, new)


def clamped_expiry(
    created_at: datetime, expires_at: datetime | None, max_days: int
) -> datetime | None:
    """The expiry a record must have so it is kept at most `max_days` from when it was saved, or
    None when it already is (expiries are only ever shortened, ND202)."""
    cap = created_at + timedelta(days=max_days)
    if expires_at is None or expires_at > cap:
        return cap
    return None


def check_limit(limit: int | None) -> None:
    """`limit` must be None or zero or more."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be zero or more")


def hash_org_pattern(org_id: str) -> str:
    """A `LIKE ... ESCAPE '\\'` pattern that narrows ledger payloads to those naming `org_id` as
    `hash_org` (canonical JSON has no spaces); the exact check is `keyed_ids_of`."""
    escaped = org_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f'%"hash_org":"{escaped}"%'


def keyed_ids_of(payloads: Iterable[Mapping[str, Any]], org_id: str) -> frozenset[str]:
    """The `hash_key_id` values of the payloads that carry `hash_scheme` and `hash_org == org_id`
    (PH-KEY R5b); a missing `hash_key_id` counts as `""`. Shared by the three stores."""
    found: set[str] = set()
    for payload in payloads:
        if "hash_scheme" in payload and payload.get("hash_org") == org_id:
            value = payload.get("hash_key_id")
            found.add(value if isinstance(value, str) else "")
    return frozenset(found)


ProjectionStreams = Literal["all", "org"]
"""Which streams an autonomy projection read covers: the organization stream and the streams of
its runs ("all", the default), or the organization stream alone ("org")."""


def projection_streams(streams: str) -> ProjectionStreams:
    """Validate a `streams` argument of `autonomy_events`."""
    if streams == "all":
        return "all"
    if streams == "org":
        return "org"
    raise ValueError(f"streams must be 'all' or 'org', not {streams!r}")


class StoreError(Exception):
    """Base class for store errors."""


class SchemaOutOfDate(StoreError):
    """The database schema is not the one this code expects (R1.29): behind it (run `nuramesh
    db migrate`) or newer than it. Raised by the PostgreSQL check-only open. Defined here, not in
    the PostgreSQL module, so callers can test for it without importing psycopg."""


class FenceLost(BaseException):  # noqa: N818 (a lost lease, not an error to handle)
    """The fence of an acknowledging write refused it: the holder's lease on its light job ran
    out or was taken over (ND347 5.3a). A `BaseException`, so the engine's broad `except
    Exception` handlers cannot turn it into a task result; the runner catches it and abandons the
    job. Nothing the refused transaction wrote is committed."""


class ConcurrencyError(StoreError):
    """Someone else changed the record first; reload and retry."""


class StreamTaken(StoreError):  # noqa: N818 (a state, said in words)
    """`append_first` found the stream already holding an entry (R3.02 F1)."""


class AlreadyExistsError(StoreError):
    """A record with the same id (or the same unique email) exists. Not retryable: it is not a
    `ConcurrencyError` (a retry of the same request can never succeed)."""


class NotFoundError(StoreError):
    """The record doesn't exist."""


class NestedTransactionError(StoreError):
    """A unit of work was opened inside another one on the same thread."""


class BadTimeError(StoreError):
    """A stored `at` is not a real time (a row written outside the product, ND233). Readers that
    list many streams skip such a stream and report it; one stream's reads and verification
    name it. `stream` and `seq` (None for a head) say where; the text never holds the value."""

    def __init__(self, stream: str, seq: int | None) -> None:
        where = f"{stream}#{seq}" if seq is not None else f"the head of {stream}"
        super().__init__(f"stored time of {where} is not a real time")
        self.stream = stream
        self.seq = seq


class ProjectionError(StoreError):
    """The autonomy projection disagrees with the ledger (or cannot be read). A caller must fail
    closed (an `ask` stays `ask`); `rebuild_projection` repairs it from the ledger."""


class StoreBusyError(StoreError):
    """The database stayed locked for longer than the busy timeout; retrying may succeed."""


class StreamHead(BaseModel):
    """The latest event of a stream: what an append builds on and what verification ends at."""

    model_config = ConfigDict(frozen=True)

    stream: str
    seq: int
    hash: str
    at: datetime


_LOG = logging.getLogger("nuramesh.stores")
REPORT_LIMIT = 20  # unlinked ids in the warning line; `Store.subject_backfill` keeps them all


@dataclass(frozen=True)
class SubjectBackfill:
    """What migration 8 did to the security-log rows that existed before the `subject` column
    (ND60): `linked` maps (organization, row id) to the user it was linked to; `unlinked` lists
    (organization, row id, event type) of every row that could not be linked and was left without
    a subject. Ids and types only, no personal data."""

    linked: dict[tuple[str, str], str] = field(default_factory=dict)
    unlinked: list[tuple[str, str, str]] = field(default_factory=list)

    def summary(self) -> str:
        """One line for the migration's log output: the counts and the unlinked event types."""
        types: dict[str, int] = {}
        for _, _, kind in self.unlinked:
            types[kind] = types.get(kind, 0) + 1
        detail = ", ".join(f"{k}={n}" for k, n in sorted(types.items())) or "none"
        return (
            f"security_log subject backfill: {len(self.linked)} linked, "
            f"{len(self.unlinked)} unlinked ({detail})"
        )

    def report(self) -> None:
        """Print the result: the summary and, for every unlinked row, its id and event type."""
        _LOG.info(self.summary())
        if self.unlinked:
            shown = self.unlinked[:REPORT_LIMIT]
            _LOG.warning(
                "security_log rows without a subject (only the 90-day expiry removes them): "
                "%d in all, the first %d: %s",
                len(self.unlinked),
                len(shown),
                ", ".join(f"{org}/{row_id} {kind}" for org, row_id, kind in shown),
            )


def derive_subjects(
    rows: Iterable[tuple[str, str, str, str | None, str | None]],
    users: Iterable[tuple[str, str, str | None]],
) -> SubjectBackfill:
    """Link old security-log rows to users, only where the row itself identifies one (never a
    guess). `rows` are (organization, row id, event type, actor, `details.email`); `users` are
    (organization, id, email or None when erased). A row is linked when its actor is a user's id
    (that user, erased or not) or its email is a not-erased user's normalised email. A row has no
    session or token id, so a system-actor row about a session cannot be linked: it stays
    without a subject and is listed, and only the 90-day expiry removes it."""
    by_id = {(org, uid) for org, uid, _ in users}
    by_email = {(org, email): uid for org, uid, email in users if email}
    result = SubjectBackfill()
    for org, row_id, kind, actor, email in rows:
        if actor is not None and (org, actor) in by_id:
            result.linked[(org, row_id)] = actor
        elif email is not None and (org, email) in by_email:
            result.linked[(org, row_id)] = by_email[(org, email)]
        else:
            result.unlinked.append((org, row_id, kind))
    return result


@dataclass(frozen=True)
class EventDraft:
    """What a caller provides; the store fills in seq, prev_hash, hash and at."""

    stream: str
    type: EventType | str
    actor: str
    run_id: str | None = None
    task_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    cost: Money | None = None
    cost_basis: CostBasis | None = None


@dataclass(frozen=True)
class ContentSubjectRow:
    """One person a stored content row names (R1.32 S3): the row is erased with the person."""

    content_id: str
    subject: str
    run_id: str | None
    erase_pending: bool


CONTENT_SUBJECT_COLUMNS = ["org_id", "content_id", "subject", "run_id", "erase_pending"]
CONTENT_SUBJECT_INDEXES = [["org_id", "subject"], ["org_id", "run_id"]]


@dataclass(frozen=True)
class StoredTask:
    """A task and the version it was stored at (for optimistic concurrency)."""

    task: Task
    version: int


@dataclass(frozen=True)
class StoredBalance:
    """A budget balance and the version it was stored at."""

    balance: BudgetBalance
    version: int


@dataclass(frozen=True)
class ExtensionRecord:
    """A record an installed plugin keeps in the store (ND72). The store never reads `body`: it
    is the plugin's own text, kept exactly as given. A record is unique per organization on
    `(namespace, kind, record_id)` and, when `unique_key` is set, on `(namespace, kind,
    unique_key)`. Records are never edited or deleted, with one exception: a record with
    `erasable` set is destroyed together with the organization's content key (the same
    transaction as its key tombstone), so a plugin can keep a secret that must not outlive it."""

    org_id: str
    namespace: str
    kind: str
    record_id: str
    body: str = field(repr=False)
    unique_key: str | None = None
    group_key: str | None = None
    sort_key: str = ""
    erasable: bool = False


@dataclass(frozen=True)
class StoredApproval:
    """An approval request and the version it was stored at."""

    approval: Approval
    version: int


@dataclass(frozen=True)
class StoredReservation:
    """A reservation and the version it was stored at."""

    reservation: Reservation
    version: int


@dataclass(frozen=True)
class StoredIdempotency:
    """One row of `idempotency_keys` (R1.35): never the key text, never a plaintext response.
    `subjects` is every person (`human:` id) the stored answer names, the actor included (ND148);
    erasing any one of them purges the row."""

    scope: Scope
    key_hash: str
    fingerprint: str
    state: State
    attempt: int
    fence: str | None
    subjects: frozenset[str]
    created_at: datetime
    lease_until: datetime
    expires_at: datetime
    status: int | None = None
    sealed: Sealed | None = None

    def as_row(self) -> Row:
        """The facts `core.idempotency.decide` needs."""
        return Row(
            self.state,
            self.fingerprint,
            self.attempt,
            self.lease_until,
            self.expires_at,
            self.status,
            self.sealed,
        )


_FENCE: ContextVar[Callable[["UnitOfWork"], None] | None] = ContextVar(
    "nuramesh_fence", default=None
)
_ACKNOWLEDGING = (EventType.TASK_TRANSITIONED, EventType.GATE_COMPLETED)


@contextmanager
def fenced(check: Callable[["UnitOfWork"], None]) -> Iterator[None]:
    """Install a pre-commit fence for this thread (ND347 5.3a): while it is active, a unit of work
    that appends an *acknowledging* entry (a task transition or a gate result) first calls
    `check(uow)`, inside the same transaction; `check` raises `FenceLost` to refuse. Calls that
    were already made and paid for (`model.called`, `budget.settled`, reservations) are never
    fenced: they always commit."""
    token = _FENCE.set(check)
    try:
        yield
    finally:
        _FENCE.reset(token)


class UnitOfWork(RunQueueOps, ABC):
    """Reads and writes inside one transaction. Obtain one from `Store.transaction()`."""

    @abstractmethod
    def head(self, stream: str) -> StreamHead | None:
        """The stream's latest event, including appends made in this unit of work."""

    @abstractmethod
    def last_event(self, stream: str) -> LedgerEvent | None:
        """The stream's latest event, including appends made in this unit of work."""

    @abstractmethod
    def events_after(self, stream: str, after_seq: int, limit: int) -> list[LedgerEvent]:
        """A stream's events after `after_seq` (at most `limit`), as seen inside this unit of
        work (under its write lock), including its own appends."""

    @abstractmethod
    def events_after_positioned(
        self, stream: str, after_seq: int, limit: int
    ) -> list[tuple[int, LedgerEvent]]:
        """Like `events_after`, each event with its commit position (E13, T135)."""

    @abstractmethod
    def commit_position(self) -> int:
        """The latest commit position, including appends made in this unit of work (0: none)."""

    @abstractmethod
    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        """Every stream's events committed after `pos`, in commit order, as seen inside this
        unit of work (under its write lock), including its own appends."""

    @abstractmethod
    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        """The projected entries of an organization (its stream and its runs' streams) of the
        given kinds (None: every projected kind, `nuramesh.core.projection.ALL_KINDS`), in commit
        order, as seen inside this unit of work. Reads the maintained projection, never the whole
        stream. With `streams="org"` only the organization stream's own entries are read (a primary
        key range of the projection; the runs' rows are neither read nor decoded). Raises
        `ProjectionError` if the projection disagrees with the ledger."""

    @abstractmethod
    def has_event(self, stream: str, type: EventType) -> bool:
        """Whether the stream has an event of this type, as seen inside this unit of work
        (including its own appends). An existence query: no event is decoded or returned; the
        database still filters the stream's rows (Stop and run-ended checks)."""

    @abstractmethod
    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        """One task's events in order, including appends made in this unit of work."""

    @abstractmethod
    def _insert_event(self, event: LedgerEvent) -> None:
        """Write one built event and move the head (implementations)."""

    _at: datetime
    """The store's clock, read ONCE when the unit of work starts, inside the global write lock
    (set by each store's constructor; ND234)."""

    @property
    def at(self) -> datetime:
        """This unit of work's one time: every append in it uses it (never earlier than the
        stream's last event, see `append`), and so does every time-dependent decision (budget
        windows, E13 evidence). Read-only: no caller can set it (ND230, ND234)."""
        return self._at

    def now(self) -> datetime:
        """Same as `at`. Never use `datetime.now()` for logic that depends on the time: tests and
        replays set the store's clock."""
        return self._at

    def append(self, draft: EventDraft) -> LedgerEvent:
        """Add an event to its stream; the store assigns seq, prev_hash, hash and time. The time
        is this unit of work's one time (`at`), never earlier than the stream's last event (if
        the store clock went backwards, the previous event's time is reused); no caller can
        supply it (ND230, ND234). The returned event carries the assigned `at`. A stream with an
        entry whose stored time is not real takes no more entries (`BadTimeError`, ND238): a
        damaged run stops writing."""
        if draft.type in _ACKNOWLEDGING:
            check = _FENCE.get()
            if check is not None:
                check(self)  # the light engine's fence (ND347 5.3a): raises FenceLost
        self._refuse_damaged(draft.stream)
        previous = self.last_event(draft.stream)
        now = self._at
        at = now if previous is None or now >= previous.at else previous.at
        event = next_event(
            previous,
            stream=draft.stream,
            type=draft.type,
            actor=draft.actor,
            at=at,
            run_id=draft.run_id,
            task_id=draft.task_id,
            payload=draft.payload,
            cost=draft.cost,
            cost_basis=draft.cost_basis,
        )
        self._insert_event(event)
        return event

    def append_first(self, draft: EventDraft) -> LedgerEvent:
        """`append`, but only to a stream with no entry: the atomic claim of a stream (R3.02 F1,
        a run id is claimed by its first `run.started`). `StreamTaken` otherwise, nothing written.
        Atomic because every store serializes its units of work (a global write lock or a
        transaction-level advisory lock) and the check reads inside that lock; the unique
        `(stream, seq)` is the database-level backstop."""
        if self.last_event(draft.stream) is not None:
            raise StreamTaken(draft.stream)
        return self.append(draft)

    def _refuse_damaged(self, stream: str) -> None:
        """Raise `BadTimeError` if `stream` holds a time that is not real. A store that keeps
        times as datetimes has none; the SQL stores check a stream once per process, since every
        later write is guarded by the database (migration 19)."""
        return None

    @abstractmethod
    def get_task(self, task_id: str) -> StoredTask | None:
        """A task and its version, or None."""

    @abstractmethod
    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        """A run's tasks ordered by id, including this unit of work's changes (read under the
        writer lock: E2 rechecks who takes part in a run here, spec 8.1)."""

    @abstractmethod
    def put_task(self, task: Task, *, expected_version: int | None) -> int:
        """Insert (`expected_version=None`) or update a task; returns the new version.

        Raises `ConcurrencyError` if the task exists when inserting, or its version isn't
        `expected_version` when updating.
        """

    @abstractmethod
    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        """A budget balance and its version, or None. A budget's key is its organization and its
        id (ND90); `org_key(None)` (the empty string) is the key of an unattributed budget."""

    @abstractmethod
    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        """One organization's budget balances, including changes in this unit of work, sorted by
        id; `None` lists every organization's, sorted by organization then id (reports and the
        local inspector only)."""

    @abstractmethod
    def put_balance(self, balance: BudgetBalance, *, expected_version: int | None) -> int:
        """Insert (`expected_version=None`) or update a balance; `ConcurrencyError` if stale."""

    @abstractmethod
    def get_approval(self, approval_id: str) -> StoredApproval | None:
        """An approval request and its version, or None."""

    @abstractmethod
    def put_approval(self, approval: Approval, *, expected_version: int | None) -> int:
        """Insert (`expected_version=None`) or update an approval; `ConcurrencyError` if stale."""

    @abstractmethod
    def solo_approvals_after(self, decided_by: str, after: datetime) -> list[Approval]:
        """The approved (or consumed) approvals this person decided with mode `solo` strictly
        after `after`, oldest decision first, including this unit of work's changes: the rows the
        solo rate limit counts (SA-9). Not scoped by organization (an
        approval row has none): the caller keeps the rows of its own organization's runs."""

    @abstractmethod
    def delete_approval_if_due(
        self, approval_id: str, now: datetime, *, expected_version: int | None = None
    ) -> bool:
        """Remove one approval only if, **inside this transaction**, it is decided or consumed and
        its retention is over at `now` (`Approval.retention_over`, ND195 a) and, when
        `expected_version` is given, it is still at that version. True if removed; a pending,
        missing, changed or not-yet-due approval stays. Never touches the ledger."""

    @abstractmethod
    def approvals_matching(
        self, run_id: str, agent_id: str, status: ApprovalStatus
    ) -> list[StoredApproval]:
        """A run's approvals for one agent in one status, including this unit of work's changes."""

    @abstractmethod
    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        """A reservation and its version, or None."""

    @abstractmethod
    def put_reservation(self, reservation: Reservation, *, expected_version: int | None) -> int:
        """Insert or update a reservation; `ConcurrencyError` if stale."""

    # -- content (ND25, T46): written in the same transaction as the ledger event that names it ---

    @abstractmethod
    def put_content(self, record: ContentRecord) -> None:
        """Store a record. The same (org, id) again keeps the first ciphertext and extends the
        expiry to the later of the two (no expiry counts as latest)."""

    @abstractmethod
    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        """One content record as seen **inside this transaction** (its own writes included), or
        None."""

    @abstractmethod
    def set_run_content_expiry(
        self, org_id: str, content_id: str, expires_at: datetime | None
    ) -> bool:
        """Set the expiry of a **durable run's content** exactly, so it may be shortened
        (`put_content` only ever extends): the retention clock of a durable run (R1.20, ND55) starts
        when the task is final. Only a record of kind `task_output`, `gate_evidence` (the gate
        feedback kept for the next attempt, ND57) or `plan_text` (R1.20 PR B) is touched; a
        reason keeps its own retention. True if one was changed."""

    @abstractmethod
    def shorten_content_expiry(
        self,
        kind: ContentKind,
        max_days: int,
        *,
        org_id: str | None = None,
        content_id: str | None = None,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Cap the expiry of every record of `kind` (of one organization, or one record) at
        `created_at + max_days`; a record with no expiry gets one. Expiries are only shortened
        (ND202). With `since`, the cap counts from that time instead of `created_at` (a record
        that is renewed in place, such as the current configuration, ND218). Returns the number
        changed per organization."""

    @abstractmethod
    def delete_content(self, org_id: str, content_id: str) -> bool:
        """Remove one record (and its subject rows, in this transaction). True if it existed."""

    # -- subjects of stored rows (R1.32 section 5): who a row names, so erasing a person finds it --

    def add_content_subjects(
        self, org_id: str, content_id: str, run_id: str | None, subjects: Collection[str]
    ) -> int:
        """Tag a row with the people it names (insert-if-absent, one row per subject). A person
        already erased (`users.erased_at`, read inside this transaction under the write lock) is
        never added, so a capture after an erasure cannot bring them back (S6). A row that does
        not exist, or none of whose subjects remain, adds nothing. Returns the rows added."""
        if self.get_content(org_id, content_id) is None:
            return 0
        added = 0
        for subject in sorted(set(subjects)):
            user = self.get_user(org_id, subject)
            if user is not None and user.erased:
                continue
            if self._put_content_subject(org_id, content_id, subject, run_id):
                added += 1
        return added

    @abstractmethod
    def _put_content_subject(
        self, org_id: str, content_id: str, subject: str, run_id: str | None
    ) -> bool:
        """Insert one subject row if absent. True if it was inserted."""

    @abstractmethod
    def content_subject_rows(self, org_id: str) -> list[ContentSubjectRow]:
        """Every subject row of the organization as seen in this transaction, ordered by
        content id and subject."""

    @abstractmethod
    def erase_content_subject(self, org_id: str, subject: str) -> tuple[int, list[str], int]:
        """Erase a person's rows: `(count, kinds, skipped)`. With the organization on **legal
        hold** nothing is deleted: the person's subject rows get `erase_pending` and `skipped` is
        the number of content rows concerned. Otherwise every content row with a subject row for
        the person is deleted, whoever else it names, with all its subject rows. `kinds` is the
        sorted distinct kinds deleted. No key is needed to delete ciphertext."""

    @abstractmethod
    def pending_erasure_orgs(self) -> list[str]:
        """Organizations with at least one subject row marked `erase_pending`."""

    @abstractmethod
    def erase_pending_content(self, org_id: str) -> tuple[int, list[str]]:
        """Delete every content row of the organization that has a subject row marked
        `erase_pending` (the caller checked there is no legal hold). `(count, kinds)`."""

    @abstractmethod
    def delete_if_expired(self, org_id: str, content_id: str, now: datetime) -> bool:
        """Remove one record only if, **inside this transaction**, its expiry has passed and the
        organization has no legal hold. True if it was removed. Expiry uses this (not a plain
        delete) so a hold set, or the same text captured again, after the list was read still
        protects the record."""

    @abstractmethod
    def legal_hold(self, org_id: str) -> bool:
        """Whether the hold is on, as seen **inside this transaction** (under the write lock, so a
        concurrent change waits for this transaction to finish)."""

    @abstractmethod
    def set_legal_hold(self, org_id: str, on: bool) -> None:
        """Turn an organization's legal hold on or off: while on, nothing of its content expires."""

    # -- key tombstones (R1.37): permanent, append-only; nothing here updates or deletes one --

    @abstractmethod
    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        """The organization's tombstone as seen inside this transaction, or None."""

    @abstractmethod
    def put_key_tombstone(self, record: KeyTombstone) -> bool:
        """Insert-if-absent. True if this call inserted it; False if one already exists (the
        existing row is never changed)."""

    @abstractmethod
    def org_has_content(self, org_id: str) -> bool:
        """Whether the organization has at least one content row (any kind, expired or not)."""

    @abstractmethod
    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        """The `key_id` stored on each of the organization's content records (PH-KEY R5b)."""

    @abstractmethod
    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        """The `hash_key_id` of every ledger event, in any stream, whose payload has
        `hash_scheme` and `hash_org == org_id` (PH-KEY R5b; exact by `hash_org`, never by stream;
        a row with `hash_scheme` and no `hash_org` counts for nobody; one with no `hash_key_id`
        counts as the id `""`)."""

    def org_key_material_in_use(self, org_id: str) -> bool:
        """Content rows or keyed ledger rows exist for the organization (PH-KEY R5b)."""
        return self.org_has_content(org_id) or bool(self.org_keyed_ledger_key_ids(org_id))

    # -- users, credentials, setup token (R1.22, T64). Synthetic data only until W.03 (ND52) --

    @abstractmethod
    def create_user(self, user: User) -> None:
        """Insert a user; `AlreadyExistsError` if the id exists in the organization or a user who
        is not erased already has the (normalised) email."""

    @abstractmethod
    def get_user(self, org_id: str, user_id: str) -> User | None:
        """One user, erased or not."""

    @abstractmethod
    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        """The user with this email, matched case-insensitively (on the normalised form)."""

    @abstractmethod
    def list_users(self, org_id: str) -> list[User]:
        """Every user of the organization, erased or not, ordered by id, including this unit of
        work's changes (a decision reads who else could decide inside its own transaction)."""

    @abstractmethod
    def erase_user(self, org_id: str, user_id: str, at: datetime) -> bool:
        """Clear the user's email and name, set `erased_at` and remove their credential. The row
        and its opaque id stay (the ledger names the id); nothing in the ledger is touched. True
        if a user was erased now; False if missing or erased before."""

    @abstractmethod
    def set_user_role(self, org_id: str, user_id: str, role: Role) -> bool:
        """Set the role of a user who is not erased (operator ownership transfer, T128). True if
        it was set; False if the user is missing, erased or in another organization."""

    @abstractmethod
    def set_user_disabled(self, org_id: str, user_id: str, at: datetime | None) -> bool:
        """Disable a user who is not erased (`at` is the time) or enable them (`at` is None).
        True if it was set; False if the user is missing, erased or in
        another organization. Nothing else about the user changes."""

    @abstractmethod
    def set_user_email_verified(self, org_id: str, user_id: str, at: datetime, via: str) -> bool:
        """Record that the user's address is verified (`via`: setup, invite, link or legacy).
        True if set; False for a missing, erased or other-organization user (R1.37)."""

    @abstractmethod
    def set_user_email(self, org_id: str, user_id: str, email: str, at: datetime) -> None:
        """R1.40: change a not-erased user's address to the (normalised) `email` and mark it
        verified `link` at `at`. `AlreadyExistsError` if another user holds it; `NotFoundError`
        for a missing or erased user."""

    @abstractmethod
    def put_email_change(self, change: EmailChange) -> None:
        """Insert an email change request (R1.40). `AlreadyExistsError` on a token hash clash."""

    @abstractmethod
    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        """The request with this token hash, or None."""

    @abstractmethod
    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        """Every request of the user, oldest first (`created_at`, then `token_hash`)."""

    @abstractmethod
    def cancel_email_changes(
        self, org_id: str, user_id: str, at: datetime, keep_hash: str | None = None
    ) -> int:
        """Set `cancelled_at` on the user's unconsumed, uncancelled requests except `keep_hash`;
        rows changed."""

    @abstractmethod
    def consume_email_change(self, org_id: str, token_hash: str, at: datetime) -> bool:
        """Set `consumed_at` only if the row is neither consumed nor cancelled. True for exactly
        one of any number of concurrent callers. Expiry is the caller's check."""

    @abstractmethod
    def purge_email_changes(self, now: datetime) -> int:
        """Delete requests whose end (`max(expires_at, consumed_at, cancelled_at)`) is 7 days or
        more before `now`; the number deleted."""

    @abstractmethod
    def delete_email_changes_for_user(self, org_id: str, user_id: str) -> int:
        """Erasure step 3b: delete every request of the user; rows deleted."""

    @abstractmethod
    def put_invite(self, invite: Invite) -> None:
        """Insert an invite (R1.37). `AlreadyExistsError` if the id or the token hash exists in
        the organization."""

    @abstractmethod
    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        """The invite, or None (also for another organization's)."""

    @abstractmethod
    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        """The invite with this token hash, or None."""

    @abstractmethod
    def list_invites(self, org_id: str) -> list[Invite]:
        """Every invite of the organization, newest first (`created_at`, then `id`, descending)."""

    @abstractmethod
    def consume_invite(
        self, org_id: str, invite_id: str, token_hash: str, at: datetime, user_id: str
    ) -> bool:
        """Accept: set `accepted_at` and `accepted_user_id`, and set `email` NULL, only if the row
        still has this token hash and is neither accepted nor revoked. True for exactly one of
        any number of concurrent callers. Expiry is the caller's check."""

    @abstractmethod
    def revoke_invite(self, org_id: str, invite_id: str, at: datetime, reason: str) -> bool:
        """Revoke an invite that is neither accepted nor revoked (an expired one may be
        revoked). True if it was revoked now."""

    @abstractmethod
    def resend_invite(
        self, org_id: str, invite_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        """New token hash and expiry, `resent_count` + 1, for an invite that is neither accepted
        nor revoked. True if done."""

    @abstractmethod
    def purge_invites(self, now: datetime) -> int:
        """Delete invites whose end (`max(accepted_at, revoked_at, expires_at)`) is 90 days or
        more before `now`; the number deleted."""

    @abstractmethod
    def detach_user_from_invites(self, org_id: str, user_id: str) -> int:
        """Erasure step 3b: `accepted_user_id` of the user's accepted invites becomes NULL, and
        `created_by` naming the user becomes `erased-user`; rows changed."""

    @abstractmethod
    def put_reset_token(self, token: ResetToken) -> None:
        """Insert a reset token or decoy (R1.41). `AlreadyExistsError` if the hash exists."""

    @abstractmethod
    def consume_reset_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        """Set `consumed_at` only if the row is neither consumed nor revoked. True for exactly
        one of any number of concurrent callers. Expiry is the caller's check."""

    @abstractmethod
    def revoke_reset_tokens(
        self, org_id: str, user_id: str, at: datetime, *, keep: str | None = None
    ) -> int:
        """Set `revoked_at` on the user's unconsumed, unrevoked tokens except the one with hash
        `keep`; the number revoked (one live link per user)."""

    @abstractmethod
    def put_revoke_link(self, link: RevokeLink) -> None:
        """Insert an API-token revoke link (R1.41 section 4a). `AlreadyExistsError` on a hash."""

    @abstractmethod
    def consume_revoke_link(self, org_id: str, token_hash: str, at: datetime) -> bool:
        """Set `consumed_at` only if the link is neither consumed nor revoked; single winner."""

    @abstractmethod
    def revoke_revoke_links(self, org_id: str, user_id: str, at: datetime) -> int:
        """Set `revoked_at` on the user's unconsumed, unrevoked links; the number revoked."""

    @abstractmethod
    def set_password_login_block(self, org_id: str, user_id: str, at: datetime, via: str) -> None:
        """Q15: upsert the user's one block row (`blocked_at`, `via` replaced)."""

    @abstractmethod
    def clear_password_login_block(self, org_id: str, user_id: str) -> bool:
        """Delete the user's block row; True if there was one."""

    @abstractmethod
    def purge_password_reset(self, now: datetime) -> int:
        """Delete reset tokens (decoys too) at `max(expires_at, consumed_at, revoked_at) + 7 d`
        and revoke links at `max(expires_at, consumed_at) + 7 d`; rows deleted."""

    @abstractmethod
    def erase_password_reset(self, org_id: str, user_id: str) -> int:
        """Erasure step 3b: delete the user's reset tokens, revoke links and block row, and set
        `created_by` naming the user to `erased-user`; rows changed or deleted."""

    @abstractmethod
    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        """The reset token with this hash, or None (R1.41)."""

    @abstractmethod
    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        """Every reset token of the organization, oldest first (`created_at`, then hash)."""

    @abstractmethod
    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        """The revoke link with this hash, or None."""

    @abstractmethod
    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        """Every revoke link of the organization, oldest first (`created_at`, then hash)."""

    @abstractmethod
    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        """The user's password-login block, or None."""

    @abstractmethod
    def adopt_security_log_rows(self, org_id: str, email: str, user_id: str) -> int:
        """Set `subject` to `user_id` on every security-log row of the organization whose
        `subject` is NULL and whose `details["email"]` is `email` (address history, README
        section 2); rows changed. A one-way NULL to user update of retention data."""

    @abstractmethod
    def put_credential(self, credential: Credential) -> None:
        """Set (or replace) a user's password hash. `NotFoundError` for a missing or erased user."""

    @abstractmethod
    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        """The user's password hash, if any."""

    @abstractmethod
    def put_setup_token(self, token: SetupToken) -> None:
        """Store a freshly issued setup token (its hash). `AlreadyExistsError` for the same hash."""

    @abstractmethod
    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        """A stored setup token by the hash of its secret."""

    @abstractmethod
    def consume_setup_token(self, org_id: str, token_hash: str, now: datetime) -> bool:
        """Mark the token used, **only if** in this transaction it exists, is unused and has not
        expired. True for exactly one caller among any number of concurrent ones."""

    # -- sessions, refresh families, lockout counters (R1.22 PR 2, T66) --

    @abstractmethod
    def put_auth_session(self, session: AuthSession) -> None:
        """Insert a session; `AlreadyExistsError` if its id exists."""

    @abstractmethod
    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        """A session by id, ended or not."""

    @abstractmethod
    def touch_auth_session(self, org_id: str, session_id: str, at: datetime) -> None:
        """Set `last_active_at` (a refresh happened)."""

    @abstractmethod
    def set_auth_session_step_up(self, org_id: str, session_id: str, at: datetime) -> bool:
        """Record the last strong authentication on this session, by any method, at `at`
        (section 6; the writers are enumerated in SU-2 of #1484). True if
        the session exists in the organization and is not revoked; otherwise nothing changes."""

    @abstractmethod
    def revoke_auth_session(self, org_id: str, session_id: str, at: datetime, reason: str) -> bool:
        """End a session now. True if it was active (not revoked before); the first reason stays."""

    @abstractmethod
    def put_auth_token(self, token: AuthToken) -> None:
        """Insert a token hash; `AlreadyExistsError` for the same hash."""

    @abstractmethod
    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        """A stored token by the hash of its secret."""

    @abstractmethod
    def use_auth_token(self, org_id: str, token_hash: str, at: datetime) -> bool:
        """Mark a token used, **only if** it exists and was not used before. True for exactly one
        caller among any number of concurrent ones."""

    @abstractmethod
    def put_auth_failure(self, failure: AuthFailure) -> None:
        """Set (replace) the counter for `failure.key`."""

    @abstractmethod
    def get_auth_failure(self, key: str) -> AuthFailure | None:
        """The counter for a key, or None."""

    @abstractmethod
    def delete_auth_failure(self, key: str) -> None:
        """Forget a counter (a successful login)."""

    @abstractmethod
    def purge_auth_records(self, sessions_ended_before: datetime, failures_before: datetime) -> int:
        """Delete sessions (with their tokens) that ended at or before `sessions_ended_before`
        (revoked, or past their absolute end), and failure counters whose last failure and lock
        are at or before `failures_before`. Returns the number of rows removed (ND53 D)."""

    @abstractmethod
    def set_auth_session_password_confirmed(
        self, org_id: str, session_id: str, at: datetime
    ) -> bool:
        """Record that this session re-confirmed the password at `at` (SA-8). Only this session;
        nothing else on the row changes. False if there is no such session."""

    @abstractmethod
    def clear_session_proofs(self, org_id: str, user_id: str) -> int:
        """W3 (SU-2, SU-7): forget `password_confirmed_at` and
        `step_up_at` on every session of the person (a password change or reset that leaves a
        session alive, SA-8); `mfa_verified_at` stays. It never sets a time. Returns the number of
        sessions cleared."""

    # -- MFA (R1.38) --

    @abstractmethod
    def set_auth_session_mfa(self, org_id: str, session_id: str, at: datetime, method: str) -> bool:
        """Record that a session passed a second factor (`totp` or `recovery`). False if there is
        no such session."""

    @abstractmethod
    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        """The person's factor, pending or active (sealed secret as stored)."""

    @abstractmethod
    def put_mfa_factor(self, factor: MfaFactor) -> None:
        """Store a new pending factor, replacing a pending one. `AlreadyExistsError` if the person
        has an active factor."""

    @abstractmethod
    def activate_mfa_factor(
        self, org_id: str, user_id: str, factor_id: str, at: datetime, step: int | None
    ) -> bool:
        """Confirm the factor **only if** it is this pending one (`confirmed_at IS NULL`) and set
        its `last_used_step`. True for exactly one caller among concurrent ones."""

    @abstractmethod
    def advance_mfa_step(self, org_id: str, user_id: str, factor_id: str, step: int) -> bool:
        """Set `last_used_step` **only if** it is unset or lower than `step` (a spent code can't
        be spent again). True if it moved."""

    @abstractmethod
    def delete_mfa_factor(self, org_id: str, user_id: str) -> bool:
        """Delete the person's factor and MFA recovery codes. False if there was no factor."""

    @abstractmethod
    def reseal_mfa_factor(
        self,
        org_id: str,
        user_id: str,
        factor_id: str,
        from_key_id: str,
        secret_enc: bytes,
        key_id: str,
    ) -> bool:
        """Replace the sealed secret of this factor **only if** it is still sealed under
        `from_key_id` (key rotation, spec section 2a). True if it moved."""

    @abstractmethod
    def delete_mfa_challenges(self, org_id: str, user_id: str) -> int:
        """Delete every login challenge of the person (live or not): they die with the factor
        that was reset or turned off (MJ1). Rows removed."""

    @abstractmethod
    def erase_mfa_user(self, org_id: str, user_id: str) -> bool:
        """Erasure step 3b (T32, T52): delete the person's factor, recovery codes and challenges,
        and where `org_auth_settings.updated_by` names them set it to `erased-user` (the policy
        value stays). True if a factor was removed."""

    @abstractmethod
    def delete_all_mfa(self, org_id: str) -> int:
        """`auth factor-key destroy`: delete every factor, recovery code and challenge of the
        organization. Returns the factors removed."""

    @abstractmethod
    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        """How many MFA recovery codes are unused."""

    @abstractmethod
    def replace_mfa_recovery_codes(
        self, org_id: str, user_id: str, code_hashes: Sequence[str], at: datetime
    ) -> None:
        """Replace the person's whole batch with these hashes (the old batch stops working)."""

    @abstractmethod
    def use_mfa_recovery_code(
        self, org_id: str, user_id: str, code_hash: str, at: datetime
    ) -> bool:
        """Spend a code **only if** it exists and is unused. True for exactly one caller."""

    @abstractmethod
    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        """A challenge by the hash of its token, used or not."""

    @abstractmethod
    def put_mfa_challenge(self, challenge: MfaChallenge) -> None:
        """Store a challenge; `AlreadyExistsError` for the same hash."""

    @abstractmethod
    def consume_mfa_challenge(self, org_id: str, token_hash: str, now: datetime) -> bool:
        """Use the challenge **only if** it exists, is unused and has not expired."""

    @abstractmethod
    def add_mfa_challenge_attempt(self, org_id: str, token_hash: str) -> int | None:
        """Count one wrong code; the new count, or None if there is no such challenge."""

    @abstractmethod
    def set_mfa_challenge_factor(self, org_id: str, token_hash: str, factor_id: str) -> bool:
        """Bind an enrol challenge to its factor, **only if** none is bound yet."""

    @abstractmethod
    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        """The organization's sign-in policy; with no stored row, MFA is not required."""

    @abstractmethod
    def put_org_auth_settings(self, settings: OrgAuthSettings) -> None:
        """Set (replace) the organization's sign-in policy."""

    @abstractmethod
    def purge_mfa_records(self, now: datetime) -> int:
        """Delete challenges one hour past their expiry and pending factors older than ten
        minutes (Q9). Active factors and recovery codes are never purged. Returns rows removed."""

    # -- idempotency keys (R1.35): a mutable, expiring table; never part of the ledger --

    @abstractmethod
    def idempotency_get(self, scope: Scope, key_hash: str) -> StoredIdempotency | None:
        """The row as stored (locked for update where the database has row locks), or None."""

    @abstractmethod
    def idempotency_put(self, row: StoredIdempotency) -> None:
        """Insert the row, or replace the one with the same scope and key hash."""

    @abstractmethod
    def idempotency_delete(self, scope: Scope, key_hash: str) -> bool:
        """Delete one row; whether there was one."""

    @abstractmethod
    def purge_idempotency_org(self, org_id: str) -> int:
        """Delete every row of the organization (shred and organization deletion). Never touches
        `key_tombstones` (R1.37)."""

    @abstractmethod
    def purge_idempotency_actor(self, org_id: str, person: str) -> int:
        """Delete the rows whose actor is `person` or whose subjects include `person` (erasure of a
        user; one indexed query)."""

    @abstractmethod
    def purge_idempotency_expired(self, now: datetime, limit: int) -> int:
        """Delete at most `limit` rows with `expires_at <= now`; the number removed."""

    @abstractmethod
    def idempotency_rows(self, org_id: str) -> list[StoredIdempotency]:
        """Every row of the organization, ordered (diagnostics and tests; no route reads this)."""

    def idempotency_begin(
        self,
        scope: Scope,
        key_hash: str,
        fingerprint: str,
        *,
        now: datetime,
        lease: int,
        ttl: int,
        fence: str,
    ) -> Outcome:
        """The atomic `begin` of section 5 inside this unit of work. `fence` is the fresh token a
        start would use; the caller draws it."""
        found = self.idempotency_get(scope, key_hash)
        action = decide(found.as_row() if found else None, fingerprint, now)
        if action == "mismatch":
            return Mismatch()
        if action == "in_progress":
            return InProgress()
        if action == "replay":
            assert found is not None and found.sealed is not None and found.status is not None
            return Replay(found.status, found.sealed)
        attempt = found.attempt + 1 if (action == "takeover" and found) else 1
        created = found.created_at if (action == "takeover" and found) else now
        self.idempotency_put(
            StoredIdempotency(
                scope=scope,
                key_hash=key_hash,
                fingerprint=fingerprint,
                state=State.IN_PROGRESS,
                attempt=attempt,
                fence=fence,
                subjects=frozenset(),
                created_at=created,
                lease_until=now + timedelta(seconds=lease),
                expires_at=expiry(now, ttl),
            )
        )
        return Started(attempt, fence)

    def idempotency_claim(self, scope: Scope, key_hash: str, fence: str) -> None:
        """Lock the row and require it `in_progress` with this fence; else `IdempotencyFenced`
        before any write."""
        found = self.idempotency_get(scope, key_hash)
        if found is None or found.state is not State.IN_PROGRESS or found.fence != fence:
            raise IdempotencyFenced("this attempt no longer owns the key")

    def idempotency_complete(
        self,
        scope: Scope,
        key_hash: str,
        fence: str,
        *,
        status: int,
        sealed: Sealed,
        now: datetime,
        ttl: int,
        subjects: frozenset[str] = frozenset(),
    ) -> None:
        """Mark the row `done` with the sealed response; `expires_at` is `now + ttl`. Raises
        `IdempotencyFenced` (nothing written) unless the row is `in_progress` with this fence."""
        self.idempotency_claim(scope, key_hash, fence)
        found = self.idempotency_get(scope, key_hash)
        assert found is not None
        self.idempotency_put(
            StoredIdempotency(
                scope=scope,
                key_hash=key_hash,
                fingerprint=found.fingerprint,
                state=State.DONE,
                attempt=found.attempt,
                fence=None,
                subjects=subjects,
                created_at=found.created_at,
                lease_until=found.lease_until,
                expires_at=expiry(now, ttl),
                status=status,
                sealed=sealed,
            )
        )

    # -- the security log (ND60): raw details of security events, mutable, expiring --

    @abstractmethod
    def append_security_log(self, entry: SecurityLogEntry) -> None:
        """Add one row (written in the same transaction as its ledger event)."""

    @abstractmethod
    def upsert_security_log_count(self, entry: SecurityLogEntry) -> int:
        """Insert `entry` (with `count` 1) if its id is absent, else add one to the stored `count`,
        saturating at `MAX_COUNT`; every other field of the stored row stays. Returns the count."""

    @abstractmethod
    def get_security_log(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        """One security-log row as seen inside this transaction, or None (T154)."""

    @abstractmethod
    def take_security_log_count(self, org_id: str, row_id: str) -> SecurityLogEntry | None:
        """Read and delete one row in this transaction (T154); None when it is absent. The key is
        `(org_id, row_id)`. There is no take outside a transaction: it would split the take from
        the write that accounts for it."""

    @abstractmethod
    def list_expired_security_log(self, now: datetime, reason: str) -> list[SecurityLogEntry]:
        """The rows of ALL organizations with `expires_at` at or before `now` and
        `details["reason"] == reason`, read in this transaction (T154; the purge is global)."""

    @abstractmethod
    def add_security_log_count(
        self, org_id: str, row_id: str, n: int, *, create: SecurityLogEntry | None = None
    ) -> int:
        """Add `n` (at least 1) to the `count` of a row, saturating at `MAX_COUNT`; every other
        field stays. An absent row is created from `create` (its count set to `min(n, MAX_COUNT)`),
        like `upsert_security_log_count`; without `create` an absent row raises `StoreError`.
        Returns the new count (T154: a pending count folded into a day-coalesced row)."""

    @abstractmethod
    def purge_security_log(self, now: datetime) -> int:
        """Delete every row whose `expires_at` is at or before `now`, except a pending halt-refusal
        count (`details["reason"] == "org_halted_pending"`, T154: `fold_expired_pending` flushes
        those first, so a direct call never loses a count); the number removed."""

    # -- identifier keys (ND60 crypto-shredding): 32 random bytes each, never derived --

    @abstractmethod
    def user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        """The user's own identifier key (made with the user; made now for a row from before
        migration 7). None for a missing or erased user: erasure deletes the key."""

    @abstractmethod
    def org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        """The organization's identifier key for `epoch`, created at its first use. None if the
        key of that epoch was destroyed: a destroyed key is never made again."""

    @abstractmethod
    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        """The organization's key for `epoch` as stored, **never creating one** (reading the
        previous epoch's key during the lockout window). None if destroyed or never made."""

    @abstractmethod
    def destroy_org_identifier_keys(self, before_epoch: int) -> int:
        """Destroy every organization identifier key of an epoch earlier than `before_epoch`
        (the key bytes are deleted, a tombstone stays so the epoch's key cannot be recreated);
        the number destroyed."""

    @abstractmethod
    def erase_security_log(self, org_id: str, user_id: str, email: str | None) -> int:
        """Erase the user from the organization's security log (ND60). Rows whose `subject` is
        `user_id`, or whose `details["email"]` is `email` (the user's normalised address), are
        deleted; the number deleted is returned. Of the rows that remain, those whose actor is
        `user_id` (they are only the actor) are kept, with the actor set to `ERASED_ACTOR` and
        the keys `ERASED_DETAIL_KEYS` removed from `details`; nothing else in them changes. Used
        when a user is erased."""

    # -- API tokens (R1.22 PR 3) --

    @abstractmethod
    def put_api_token(self, token: ApiToken) -> None:
        """Insert a token record; `AlreadyExistsError` if its id, its name (in the organization,
        revoked tokens included: a name is never reused) or its hash exists."""

    @abstractmethod
    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        """A token record by id, revoked or not."""

    @abstractmethod
    def revoke_api_token(self, org_id: str, token_id: str, at: datetime, by: str) -> bool:
        """Revoke a token now. True if it existed and was not revoked before; the first
        revocation stays."""

    @abstractmethod
    def rename_api_token(self, org_id: str, token_id: str, name: str) -> bool:
        """Set a token's name (#634). False if no such row in the organization;
        `AlreadyExistsError` with a fixed text holding no name if another row of the organization
        holds `name`. Changes nothing else."""

    @abstractmethod
    def delete_api_token(self, org_id: str, token_id: str, now: datetime) -> bool:
        """Delete an **inactive** token (revoked, or `expires_at < now`; the instant of expiry
        still counts as active) of the organization and, with it, the rows of
        `api_token_revoke_links` of the same organization that name it. False if there is no such
        row or it is active. PostgreSQL decides on `least(now, database now)`, so a far-future
        `now` never makes a live token deletable."""

    @abstractmethod
    def erase_api_token_issuer(self, org_id: str, user_id: str) -> int:
        """Erasure of a person, step 1c (#634 R2 to R4): every token of the organization created
        by `user_id` gets the replacement name (`next_erased_token_name`, rows in ascending id)
        and `created_by` `erased-user`; every row of the organization revoked by `user_id` gets
        `revoked_by` `erased-user`. Rows changed. Callers: `AuthService.erase_user` and the scrub
        command only."""

    @abstractmethod
    def normalise_api_token_issuer(self, org_id: str, token_id: str) -> int:
        """R9: apply the erasure rewrite to one row when its `created_by` or `revoked_by` is a
        `human:<id>` with no user row in the organization or an erased one (each column only when
        it qualifies; the name only with `created_by`). 1 if the row changed, else 0. Callers:
        `TokenService.rename` and `delete`."""

    @abstractmethod
    def api_token_name_retired(self, org_id: str, keyed: Sequence[tuple[int, str]]) -> bool:
        """Whether any `(epoch, name_hash)` of `keyed` is in the organization's retired names."""

    @abstractmethod
    def retire_api_token_name(
        self, org_id: str, name_hash: str, epoch: int, retired_at: datetime
    ) -> None:
        """Reserve a name (by its keyed hash) for the bounded time of #634 R16. A hash already
        stored for that epoch is ignored, not an error."""

    # -- plugin records (ND72) --

    @abstractmethod
    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        """A plugin record by id, as seen inside this unit of work."""

    @abstractmethod
    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        """The plugin record with this unique key, as seen inside this unit of work."""

    @abstractmethod
    def ext_put(self, record: ExtensionRecord) -> None:
        """Insert a plugin record; `AlreadyExistsError` if its id or its unique key exists."""

    @abstractmethod
    def ext_erase(self, org_id: str) -> int:
        """Destroy every `erasable` plugin record of the organization (called by its content key's
        destruction, in the same transaction). Returns how many were destroyed."""

    # -- agent credentials (E2) --

    @abstractmethod
    def put_agent_credential(self, credential: AgentCredential) -> None:
        """Insert a credential record; `AlreadyExistsError` if its id or its hash (in the
        organization) exists."""

    @abstractmethod
    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        """A credential record by id, revoked or not (read through the transaction)."""

    @abstractmethod
    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        """Every credential of the organization, oldest first (`created_at`, then `id`)."""

    @abstractmethod
    def revoke_agent_credential(
        self, org_id: str, credential_id: str, at: datetime, by: str
    ) -> bool:
        """Revoke now. True if it existed and was not revoked before; the first revocation stays."""

    @abstractmethod
    def shorten_agent_credential(
        self, org_id: str, credential_id: str, expires_at: datetime
    ) -> bool:
        """Set `expires_at` to `expires_at` if that is earlier than the stored one (never longer).
        True if it changed."""

    @abstractmethod
    def count_live_agent_credentials(self, org_id: str, agent_id: str, now: datetime) -> int:
        """Live (not revoked, not expired at `now`) credentials of the agent, across teams."""

    @abstractmethod
    def erase_agent_credential_user(
        self, org_id: str, user_id: str, now: datetime
    ) -> list[AgentCredential]:
        """Erasure of a person (Q4): every credential whose `created_by` is `user_id` gets
        `erased-user`, and every one whose `revoked_by` is `user_id` too. Credentials are not
        revoked. Returns the credentials that were **live at `now`** and had `user_id` as their
        issuer, as they were before the rewrite."""

    @abstractmethod
    def purge_agent_credentials(self, now: datetime) -> int:
        """Delete every ended credential (revoked or expired) whose end is 90 days or more before
        `now` (Q11); the number removed."""


class Store(ABC):
    """A place to keep state and the ledger. Implementations: memory, SQLite (Postgres later)."""

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self._clock = clock

    def now(self) -> datetime:
        """The store's injected clock (used to show a windowed budget as of now)."""
        return self._clock()

    @abstractmethod
    def transaction(self) -> AbstractContextManager[UnitOfWork]:
        """A unit of work: commits when the block ends normally, discards all if it raises."""

    @abstractmethod
    def read_stream(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[LedgerEvent]:
        """Committed events of a stream in order, after `after_seq`, at most `limit` of them."""

    @abstractmethod
    def read_stream_positioned(
        self, stream: str, *, after_seq: int = 0, limit: int | None = None
    ) -> Iterator[tuple[int, LedgerEvent]]:
        """Like `read_stream`, each event with its **commit position**: the store's one
        increasing number across streams (E13, T135). An event committed before positions
        existed (schema 17) reads as 0. The position is not part of any hash."""

    @abstractmethod
    def commit_position(self) -> int:
        """The latest commit position (0 for an empty ledger)."""

    @abstractmethod
    def entries_after_position(
        self, pos: int, limit: int | None = None
    ) -> list[tuple[int, LedgerEvent]]:
        """Every stream's committed events after `pos`, in commit order, at most `limit`."""

    @abstractmethod
    def autonomy_events(
        self,
        org_id: str,
        kinds: Collection[str] | None = None,
        *,
        streams: ProjectionStreams = "all",
    ) -> list[tuple[int, LedgerEvent]]:
        """Like `UnitOfWork.autonomy_events`, over committed entries (the seam of the
        earned-autonomy reads: one indexed read per kind, never a scan of the stream)."""

    @abstractmethod
    def projection_problems(self) -> list[str]:
        """A full audit: every way the projection differs from what the ledger implies (empty:
        consistent). Reads the whole ledger; not for the request path."""

    @abstractmethod
    def rebuild_projection(self) -> int:
        """Drop the projection and rebuild it from the ledger in one transaction; returns the
        number of indexed entries. The ledger is untouched."""

    @abstractmethod
    def task_events(self, run_id: str, task_id: str) -> list[LedgerEvent]:
        """Committed events of one task (its timeline and audit view), in order."""

    @abstractmethod
    def snapshot(self, stream: str) -> tuple[list[LedgerEvent], StreamHead | None]:
        """A stream's events and head read together, consistently (no write in between)."""

    @abstractmethod
    def head(self, stream: str) -> StreamHead | None:
        """The stream's latest committed event."""

    @abstractmethod
    def streams(self) -> list[str]:
        """Every stream with at least one committed event, sorted."""

    @abstractmethod
    def budget_definitions(self) -> list[LedgerEvent]:
        """Every committed `budget.defined` entry of an `org:` stream, earliest first (by time,
        then stream, then sequence), read through an index: no other entry is read, so the
        work grows with the number of budgets, never with the size of the streams
        (R8). `define_budget` writes one per budget id."""

    def heads(self, only: Collection[str] | None = None) -> dict[str, StreamHead]:
        """The heads of the given streams (default: every stream) in one call; live-event
        consumers poll this once per tick. The default asks per stream; stores override it with
        a single query. Streams with no events are left out, and so is a stream whose head holds
        a time that is not real (logged, and named by `damaged_streams`): one bad row never makes
        the listing fail for everyone."""
        found: dict[str, StreamHead] = {}
        for stream in self.streams() if only is None else sorted(set(only)):
            try:
                head = self.head(stream)
            except BadTimeError:
                _LOG.warning("the head of stream %s holds a time that is not real; skipped", stream)
                continue
            if head is not None:
                found[stream] = head
        return found

    def damaged_heads(self) -> list[str]:
        """Streams whose head holds a time that is not a real time (sorted): one query over the
        heads, cheap enough for the hot paths. Always empty for a store that keeps times as
        datetimes; the SQL stores check the stored text."""
        return []

    def damaged_streams(self) -> list[str]:
        """Streams with a head or ANY entry holding a time that is not a real time (sorted,
        ND238). Scans every entry, so it is for listings and checks, not for hot paths (the SQL
        stores skip the scan when the database itself has validated every row)."""
        return self.damaged_heads()

    def run_owner(self, stream: str) -> str | None:
        """The organization named by the first entry of a `run:` stream (its `run.started`), or
        None. Read from the raw row without parsing any time, so a run whose stored time is not
        real is still attributed to its organization (ND238)."""
        first = next(iter(self.read_stream(stream, after_seq=0, limit=1)), None)
        if first is None or first.type != EventType.RUN_STARTED.value:
            return None
        org = first.payload.get("org")
        return org if isinstance(org, str) else None

    @abstractmethod
    def get_task(self, task_id: str) -> StoredTask | None:
        """A committed task and its version."""

    @abstractmethod
    def tasks_for_run(self, run_id: str) -> list[StoredTask]:
        """Committed tasks of a run, ordered by id."""

    @abstractmethod
    def get_balance(self, org_id: str, budget_id: str) -> StoredBalance | None:
        """A committed budget balance and its version, by organization and budget id (ND90)."""

    @abstractmethod
    def get_reservation(self, reservation_id: str) -> StoredReservation | None:
        """A committed reservation and its version."""

    @abstractmethod
    def list_balances(self, org_id: str | None) -> list[StoredBalance]:
        """One organization's committed budget balances sorted by id; `None`: every
        organization's, sorted by organization then id (for reports and the local inspector)."""

    @abstractmethod
    def get_approval(self, approval_id: str) -> StoredApproval | None:
        """A committed approval request and its version."""

    @abstractmethod
    def approvals(
        self, *, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[StoredApproval]:
        """Committed approval requests (the inbox), optionally for one run or one status, by id."""

    @abstractmethod
    def due_approvals(self, now: datetime, *, after_id: str, limit: int) -> list[StoredApproval]:
        """Committed decided or consumed approvals whose retention is over at `now`, by id, with
        an id greater than `after_id`, at most `limit` (a cursor for the sweep, ND195 a). The SQL
        stores read only rows the anchor-day index selects (`due_anchor_day`), never the whole
        finished table."""

    @abstractmethod
    def reservations_for_run(
        self, run_id: str, status: ReservationStatus | None = None
    ) -> list[StoredReservation]:
        """A run's committed reservations, optionally only one status, sorted by id."""

    @abstractmethod
    def get_content(self, org_id: str, content_id: str) -> ContentRecord | None:
        """One content record by organization and keyed hash, or None (never stored, or gone)."""

    def content_subject_rows(self, org_id: str) -> list[ContentSubjectRow]:
        """Committed subject rows of an organization (R1.32 S3), for tests and audits."""
        with self.transaction() as uow:
            return uow.content_subject_rows(org_id)

    @abstractmethod
    def content_subject_schema(self) -> dict[str, list[Any]]:
        """The columns and indexes of the subject table as the database has them:
        `{"columns": [...], "indexes": [[columns], ...]}` (the shape is part of the contract with
        the parked R1.20 rest)."""

    def has_content(self, org_id: str) -> bool:
        """Whether the organization has any content row, read without writing (the start checks
        of a run; stores override this with a plain read, this default opens a transaction)."""
        with self.transaction() as uow:
            return uow.org_has_content(org_id)

    def org_content_key_ids(self, org_id: str) -> frozenset[str]:
        """As the unit of work's, read without writing (PH-KEY R5b)."""
        with self.transaction() as uow:
            return uow.org_content_key_ids(org_id)

    def org_keyed_ledger_key_ids(self, org_id: str) -> frozenset[str]:
        """As the unit of work's, read without writing (PH-KEY R5b)."""
        with self.transaction() as uow:
            return uow.org_keyed_ledger_key_ids(org_id)

    def org_key_material_in_use(self, org_id: str) -> bool:
        """Content rows or keyed ledger rows exist for the organization (PH-KEY R5b)."""
        return self.has_content(org_id) or bool(self.org_keyed_ledger_key_ids(org_id))

    @abstractmethod
    def expired_content(self, now: datetime, *, limit: int = 500) -> list[ContentRecord]:
        """Records whose expiry has passed, oldest first, skipping organizations on legal hold."""

    @abstractmethod
    def expiring_content(
        self, kind: ContentKind, after: datetime, until: datetime
    ) -> list[ContentRecord]:
        """Records of one kind with `after < expires_at <= until`, oldest first, skipping
        organizations on legal hold (the re-capture sweep, ND194). Not a page: the window is
        small by construction."""

    @abstractmethod
    def legal_hold(self, org_id: str) -> bool:
        """Whether the organization's legal hold is on."""

    @abstractmethod
    def key_tombstone(self, org_id: str) -> KeyTombstone | None:
        """The organization's key tombstone (R1.37), or None. A store error is raised, never
        turned into None: None means "no tombstone"."""

    @abstractmethod
    def key_tombstones(self) -> list[KeyTombstone]:
        """Every tombstone, sorted by organization id."""

    @abstractmethod
    def shredded_orgs(self) -> list[tuple[str, datetime, str]]:
        """`(org_id, time of the earliest entry, its actor)` for every organization stream with a
        `decision.recorded` of kind `content_shred` (the backfill read; reads entries by type only,
        never verifies the chain). A stored time that is not real raises `BadTimeError`: an entry
        is never dropped, because it could be the shred of an organization (ND277)."""

    @abstractmethod
    def store_id(self) -> str:
        """This store's identity: 32 lower-case hex, made once and never changed (R1.37 9.1). A
        backup carries it; a new empty database gets a new one."""

    def idempotency_begin(
        self,
        scope: Scope,
        key_hash: str,
        fingerprint: str,
        *,
        now: datetime,
        lease: int,
        ttl: int,
    ) -> Outcome:
        """`begin` (section 5) in its own transaction: Started, Replay, InProgress or Mismatch.
        A row past its expiry is replaced in the same transaction."""
        with self.transaction() as uow:
            return uow.idempotency_begin(
                scope,
                key_hash,
                fingerprint,
                now=now,
                lease=lease,
                ttl=ttl,
                fence=secrets.token_hex(16),
            )

    def idempotency_complete(
        self,
        scope: Scope,
        key_hash: str,
        fence: str,
        *,
        status: int,
        sealed: Sealed,
        now: datetime,
        ttl: int,
    ) -> bool:
        """Complete an at-least-once attempt in its own transaction; False if the fence is stale
        or the row is no longer `in_progress` (nothing changes)."""
        try:
            with self.transaction() as uow:
                uow.idempotency_complete(
                    scope, key_hash, fence, status=status, sealed=sealed, now=now, ttl=ttl
                )
        except IdempotencyFenced:
            return False
        return True

    def idempotency_release(self, scope: Scope, key_hash: str, fence: str) -> bool:
        """Delete the row if it is `in_progress` with this fence (a 4xx, 5xx or unstorable
        response); False and no change otherwise."""
        with self.transaction() as uow:
            found = uow.idempotency_get(scope, key_hash)
            if found is None or found.state is not State.IN_PROGRESS or found.fence != fence:
                return False
            return uow.idempotency_delete(scope, key_hash)

    def purge_idempotency(self, now: datetime, *, limit: int = 1000) -> int:
        """The sweep: delete at most `limit` rows with `expires_at <= now`."""
        with self.transaction() as uow:
            return uow.purge_idempotency_expired(now, limit)

    def idempotency_rows(self, org_id: str) -> list[StoredIdempotency]:
        """Every row of the organization (diagnostics and tests)."""
        with self.transaction() as uow:
            return uow.idempotency_rows(org_id)

    def upsert_security_log_count(self, entry: SecurityLogEntry) -> int:
        """`UnitOfWork.upsert_security_log_count` in its own small transaction (call it after the
        refused request's transaction has rolled back, never inside one). Returns the new count."""
        with self.transaction() as uow:
            return uow.upsert_security_log_count(entry)

    @abstractmethod
    def get_user(self, org_id: str, user_id: str) -> User | None:
        """One user (erased users keep their row)."""

    @abstractmethod
    def get_user_by_email(self, org_id: str, email: str) -> User | None:
        """The user with this email, case-insensitively; None if none (or the email is invalid)."""

    @abstractmethod
    def list_users(self, org_id: str) -> list[User]:
        """Every user of the organization, erased ones included, ordered by id."""

    @abstractmethod
    def get_credential(self, org_id: str, user_id: str) -> Credential | None:
        """The user's password hash, if any."""

    @abstractmethod
    def get_setup_token(self, org_id: str, token_hash: str) -> SetupToken | None:
        """A stored setup token by the hash of its secret."""

    @abstractmethod
    def list_setup_tokens(self, org_id: str) -> list[SetupToken]:
        """Every setup token of the organization (hashes only), oldest first."""

    @abstractmethod
    def get_auth_session(self, org_id: str, session_id: str) -> AuthSession | None:
        """A session by id, ended or not."""

    @abstractmethod
    def list_auth_sessions(self, org_id: str) -> list[AuthSession]:
        """Every session record of the organization, ordered by id."""

    @abstractmethod
    def get_auth_token(self, org_id: str, token_hash: str) -> AuthToken | None:
        """A stored token by the hash of its secret."""

    @abstractmethod
    def list_auth_tokens(self, org_id: str, session_id: str) -> list[AuthToken]:
        """The tokens of one session, oldest first."""

    @abstractmethod
    def get_auth_failure(self, key: str) -> AuthFailure | None:
        """The lockout counter for a key, or None."""

    @abstractmethod
    def list_mfa_factors(self, org_id: str) -> list[MfaFactor]:
        """Every factor of the organization (pending and active), by user id."""

    @abstractmethod
    def get_mfa_factor(self, org_id: str, user_id: str) -> MfaFactor | None:
        """The person's factor, pending or active (sealed secret as stored)."""

    @abstractmethod
    def count_mfa_recovery_codes(self, org_id: str, user_id: str) -> int:
        """How many MFA recovery codes are unused."""

    @abstractmethod
    def get_mfa_challenge(self, org_id: str, token_hash: str) -> MfaChallenge | None:
        """A challenge by the hash of its token, used or not."""

    @abstractmethod
    def get_org_auth_settings(self, org_id: str) -> OrgAuthSettings:
        """The organization's sign-in policy; with no stored row, MFA is not required."""

    @abstractmethod
    def get_user_identifier_key(self, org_id: str, user_id: str) -> bytes | None:
        """The user's identifier key as stored (never creates one); None once erased."""

    @abstractmethod
    def get_org_identifier_key(self, org_id: str, epoch: int) -> bytes | None:
        """The organization's key for an epoch as stored (never creates one; None if destroyed
        or never made)."""

    @abstractmethod
    def list_security_log(self, org_id: str) -> list[SecurityLogEntry]:
        """The organization's security log rows, oldest first (by time, then id)."""

    @abstractmethod
    def get_api_token_by_hash(self, org_id: str, token_hash: str) -> ApiToken | None:
        """A token record by the hash of its secret, revoked or not."""

    @abstractmethod
    def get_api_token(self, org_id: str, token_id: str) -> ApiToken | None:
        """A token record by id, revoked or not."""

    @abstractmethod
    def list_api_tokens(self, org_id: str) -> list[ApiToken]:
        """Every token record of the organization (hashes only), oldest first."""

    @abstractmethod
    def api_token_retired_rows(self, org_id: str) -> list[tuple[int, str, datetime]]:
        """The retired token names of the organization as `(epoch, name_hash, retired_at)`,
        oldest first (hashes only; for tests and the audit, never shown through the API)."""

    @abstractmethod
    def ext_get(
        self, org_id: str, namespace: str, kind: str, record_id: str
    ) -> ExtensionRecord | None:
        """A plugin record by id."""

    @abstractmethod
    def ext_find(
        self, org_id: str, namespace: str, kind: str, unique_key: str
    ) -> ExtensionRecord | None:
        """The plugin record with this unique key."""

    @abstractmethod
    def ext_list(
        self, org_id: str, namespace: str, kind: str, group_key: str
    ) -> list[ExtensionRecord]:
        """The records of one group, newest `sort_key` first, ties by `record_id` ascending."""

    @abstractmethod
    def count_security_log(
        self, org_id: str, type: str, actor: str | None, start: datetime, end: datetime
    ) -> int:
        """Rows of one type and actor with `start <= at < end` (bounds a plugin's refusal rows)."""

    @abstractmethod
    def get_agent_credential_by_hash(
        self, org_id: str, team: str, token_hash: str
    ) -> AgentCredential | None:
        """A credential by the hash of its secret, in the organization and the team key."""

    @abstractmethod
    def get_agent_credential(self, org_id: str, credential_id: str) -> AgentCredential | None:
        """A credential record by id, revoked or not."""

    @abstractmethod
    def get_email_change(self, org_id: str, token_hash: str) -> EmailChange | None:
        """The email change request with this token hash, or None (R1.40)."""

    @abstractmethod
    def list_email_changes(self, org_id: str, user_id: str) -> list[EmailChange]:
        """Every email change request of the user, oldest first."""

    @abstractmethod
    def get_invite(self, org_id: str, invite_id: str) -> Invite | None:
        """The invite, or None (R1.37)."""

    @abstractmethod
    def get_invite_by_token_hash(self, org_id: str, token_hash: str) -> Invite | None:
        """The invite with this token hash, or None."""

    @abstractmethod
    def list_invites(self, org_id: str) -> list[Invite]:
        """Every invite of the organization, newest first."""

    @abstractmethod
    def get_reset_token(self, org_id: str, token_hash: str) -> ResetToken | None:
        """The reset token with this hash, or None (R1.41)."""

    @abstractmethod
    def list_reset_tokens(self, org_id: str) -> list[ResetToken]:
        """Every reset token of the organization, oldest first (`created_at`, then hash)."""

    @abstractmethod
    def get_revoke_link(self, org_id: str, token_hash: str) -> RevokeLink | None:
        """The revoke link with this hash, or None."""

    @abstractmethod
    def list_revoke_links(self, org_id: str) -> list[RevokeLink]:
        """Every revoke link of the organization, oldest first (`created_at`, then hash)."""

    @abstractmethod
    def get_password_login_block(self, org_id: str, user_id: str) -> PasswordLoginBlock | None:
        """The user's password-login block, or None."""

    @abstractmethod
    def list_agent_credentials(self, org_id: str) -> list[AgentCredential]:
        """Every credential of the organization, oldest first (`created_at`, then `id`)."""

    def verify_stream(self, stream: str, *, expected_head: str | None = None) -> ChainReport:
        """Verify a stream against `expected_head`, or the store's own head if none is given.

        The store's own head only catches accidental damage; tamper evidence needs a head kept
        outside the database (ledger head anchoring).
        """
        try:
            events, head = self.snapshot(stream)
        except BadTimeError as bad:
            return ChainReport.failure(
                stream, 0, ChainProblem.BAD_TIME, bad.seq or 0, "a stored time is not a real time"
            )
        anchor = expected_head if expected_head is not None else (head.hash if head else None)
        return verify_chain(events, expected_head=anchor, expected_stream=stream)


def verify_projected(
    rows: Iterable[tuple[str, str, int, LedgerEvent]], counts: Mapping[tuple[str, str], int]
) -> list[tuple[int, LedgerEvent]]:
    """Check one projection read and return its entries in commit order.

    `rows` are `(stream, kind, position, event)` as the index gave them; `counts` is the
    maintained number of indexed entries per `(stream, kind)` for every pair the read covered.
    An entry whose kind is not the one the ledger implies, or a count that differs from the rows
    found, raises `ProjectionError`."""
    seen: dict[tuple[str, str], int] = {}
    out: list[tuple[int, LedgerEvent]] = []
    for stream, kind, pos, event in rows:
        key = projection_key(event)
        if key is None or key.kind != kind or event.stream != stream:
            raise ProjectionError(
                f"projection row {stream}#{event.seq} says '{kind}' but the ledger implies "
                f"'{None if key is None else key.kind}'"
            )
        seen[(stream, kind)] = seen.get((stream, kind), 0) + 1
        out.append((pos, event))
    for pair in seen.keys() | counts.keys():
        if seen.get(pair, 0) != counts.get(pair, 0):
            raise ProjectionError(
                f"projection for {pair[0]} '{pair[1]}' holds {seen.get(pair, 0)} entries, "
                f"the maintained count is {counts.get(pair, 0)}"
            )
    out.sort(key=lambda item: (item[0], item[1].stream, item[1].seq))
    return out
