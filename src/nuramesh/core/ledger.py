"""The ledger: an append-only, hash-chained record of everything that happens (PLAN G.1–G.2).

Events form **streams**: one per run (`run:<id>`), plus one per organization for events outside
runs (`org:<id>`). Within a stream, events are numbered 1, 2, 3, … and each carries the hash of
the one before it (the first carries 64 zeros). An event's hash is SHA-256 over the canonical JSON
of all its other fields (`core.canonical`), so changing, removing, inserting or reordering any
event breaks `verify_chain` from that point on.

Loading a stored event never checks its hash (a tampered ledger must still load, so the damage can
be reported); `verify_chain` does the checking. Any well-formed event type loads (older readers can
verify newer ledgers); writing with `next_event` needs a known `EventType` or a type an
installed plugin registered with `register_event_types`. Payloads are frozen
when an event is built, so later changes to the caller's data can't alter a sealed event.

Every cost carries a basis: `actual` (metered), `provisional` (an owner's override price, A3a),
`reserved` (a budget reservation) or `unknown` (no amount; recorded rather than guessed).

The chain proves integrity only against a head hash kept **outside** the writer's database; the
store records heads and the ledger head anchoring task exports them elsewhere.

Prompts and outputs are never stored here by default: model calls record content hashes and
metadata only (decision A7).
"""

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

from nuramesh.core.canonical import canonical_json, canonical_value, sha256_hex, thaw
from nuramesh.core.money import Money
from nuramesh.core.org import Slug
from nuramesh.core.people import HUMAN_ID_BODY

GENESIS_HASH = "0" * 64
MAX_PAYLOAD_BYTES = 256 * 1024

StreamId = Annotated[
    str, StringConstraints(pattern=r"^(run|org):[a-z0-9][a-z0-9_-]{0,62}$", strict=True)
]
"""`run:<run id>` or `org:<org id>`."""

Actor = Annotated[
    str,
    StringConstraints(
        pattern=(
            rf"^(system(:[a-z][a-z0-9-]{{0,63}})?|(agent|service):[A-Za-z0-9._@-]{{1,128}}"
            rf"|operator:[A-Za-z0-9._-]{{1,64}}|human:{HUMAN_ID_BODY})$"
        ),
        strict=True,
    ),
]
"""Who did it: `system` (or a named part of the system, `system:<name>`, for example
`system:catalog-seed`), `agent:<id>`, `human:<opaque id>` or `service:<id>`
(an API token is `service:tok_<24 hex>`, never its name: T85; the CLI is `service:cli`), or
`operator:<os user>`: the OS user who ran an operator command on the server (T128).

A person is only ever an opaque id, never an email or a name (T42, `core.people`)."""

Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]

TypeName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", strict=True)
]
"""An event type's text, for example `model.called`."""


class CostBasis(StrEnum):
    """Where a cost figure comes from."""

    ACTUAL = "actual"
    PROVISIONAL = "provisional"
    RESERVED = "reserved"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    """What happened. Payload fields per type are documented in docs/architecture.md §4.6."""

    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_CANCELLED = "run.cancelled"
    RUN_STOP_REQUESTED = "run.stop_requested"
    RUN_ENDED = "run.ended"
    ORG_HALTED = "org.halted"
    ORG_RESUMED = "org.resumed"
    ORG_ROLE_PAUSED = "org.role_paused"
    ORG_ROLE_RESUMED = "org.role_resumed"
    BREAKER_TRIPPED = "breaker.tripped"
    BREAKER_RESET = "breaker.reset"
    BREAKER_DISABLED = "breaker.disabled"
    BREAKER_ENABLED = "breaker.enabled"
    TASK_STOP_REQUESTED = "task.stop_requested"
    TASK_CREATED = "task.created"
    TASK_REFUSED = "task.refused"
    TASK_TRANSITIONED = "task.transitioned"
    MODEL_CALLED = "model.called"
    MODEL_BLOCKED = "model.blocked"
    MODEL_OUTPUT_CAPPED = "model.output_capped"
    TOOL_CALLED = "tool.called"
    POLICY_DECIDED = "policy.decided"
    BUDGET_DEFINED = "budget.defined"
    BUDGET_RESERVED = "budget.reserved"
    BUDGET_SETTLED = "budget.settled"
    BUDGET_RELEASED = "budget.released"
    BUDGET_EXCEEDED = "budget.exceeded"
    BUDGET_ADJUSTED = "budget.adjusted"
    BUDGET_BREACHED = "budget.breached"
    BUDGET_UNBLOCKED = "budget.unblocked"
    BUDGET_WINDOW_ROLLED = "budget.window_rolled"
    GATE_STARTED = "gate.started"
    GATE_COMPLETED = "gate.completed"
    EVAL_SCORED = "eval.scored"
    CALIBRATION_SCORED = "calibration.scored"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVALS_SWEPT = "approvals.swept"
    DECISION_RECORDED = "decision.recorded"
    CANON_LOADED = "canon.loaded"
    PRICE_OVERRIDE_USED = "price.override_used"
    SANDBOX_OPT_OUT = "sandbox.opt_out"
    NETWORK_PRIVATE_ALLOWED = "network.private_allowed"
    CONTENT_CAPTURED = "content.captured"
    CONTENT_EXPIRED = "content.expired"
    CONTENT_ERASED = "content.erased"
    CONTENT_READ = "content.read"
    NETWORK_TEMPORAL_PLAINTEXT_ALLOWED = "network.temporal_plaintext_allowed"
    RUNNER_REAL_MODELS_ON = "runner.real_models_on"
    USER_CREATED = "user.created"
    USER_ERASED = "user.erased"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DISABLED = "user.disabled"
    USER_ENABLED = "user.enabled"
    USER_INVITED = "user.invited"
    USER_INVITE_RESENT = "user.invite_resent"
    USER_INVITE_REVOKED = "user.invite_revoked"
    USER_INVITE_ACCEPTED = "user.invite_accepted"
    USER_INVITE_LINK_PRINTED = "user.invite_link_printed"
    USER_EMAIL_CHANGE_REQUESTED = "user.email_change_requested"
    USER_EMAIL_CHANGED = "user.email_changed"
    ORG_CLOSED = "org.closed"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOCKOUT = "auth.lockout"
    AUTH_SESSION_CREATED = "auth.session_created"
    AUTH_SESSION_REVOKED = "auth.session_revoked"
    AUTH_SETUP_TOKEN_USED = "auth.setup_token_used"
    AUTH_PASSWORD_CONFIRMED = "auth.password_confirmed"  # SA-8: payload {}, no security-log row
    AUTH_STEP_UP = "auth.step_up"  # #1484 SU-2: payload {method} only, no security-log row
    AUTH_TOKEN_CREATED = "auth.token_created"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"
    AUTH_TOKEN_RENAMED = "auth.token_renamed"
    AUTH_TOKEN_DELETED = "auth.token_deleted"
    AUTH_TOKEN_NAMES_ERASED = "auth.token_names_erased"
    AUTH_PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    AUTH_PASSWORD_RESET_ISSUED = "auth.password_reset_issued"
    AUTH_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    AUTH_TOKEN_REVOKE_LINKS_ISSUED = "auth.token_revoke_links_issued"
    AUTH_TOKEN_REVOKE_LINK_USED = "auth.token_revoke_link_used"
    AGENT_CREDENTIAL_ISSUED = "agent.credential_issued"
    AGENT_CREDENTIAL_REVOKED = "agent.credential_revoked"
    AGENT_CREDENTIAL_ISSUER_ERASED = "agent.credential_issuer_erased"
    AGENT_CREDENTIAL_ISSUER_DISABLED = "agent.credential_issuer_disabled"
    AUTH_LAST_OWNER_REFUSED = "auth.last_owner_refused"
    AUTH_MFA_ENROLLED = "auth.mfa_enrolled"
    AUTH_MFA_DISABLED = "auth.mfa_disabled"
    AUTH_MFA_RESET = "auth.mfa_reset"
    AUTH_MFA_RECOVERY_REGENERATED = "auth.mfa_recovery_regenerated"
    AUTH_MFA_RECOVERY_USED = "auth.mfa_recovery_used"
    AUTH_MFA_POLICY_SET = "auth.mfa_policy_set"
    AUTH_MFA_VERIFIED = "auth.mfa_verified"
    AUTH_KEY_CREATED = "auth.key_created"
    AUTH_KEY_ROTATED = "auth.key_rotated"
    AUTH_KEY_DESTROYED = "auth.key_destroyed"
    EMAIL_SENT = "email.sent"
    EMAIL_FAILED = "email.failed"
    MODEL_CONNECTION_CREATED = "model.connection_created"
    MODEL_CONNECTION_UPDATED = "model.connection_updated"
    MODEL_CONNECTION_REMOVED = "model.connection_removed"
    MODEL_CONNECTION_USED = "model.connection_used"
    MODEL_CONNECTION_TEST_RESERVED = "model.connection_test_reserved"
    MODEL_CONNECTION_TESTED = "model.connection_tested"
    MODEL_KEY_STORED = "model.key_stored"
    MODEL_KEY_TEST_FAILED = "model.key_test_failed"
    MODEL_KEY_ACTIVATED = "model.key_activated"
    MODEL_KEY_REVOKED = "model.key_revoked"
    MODEL_KEY_DESTROYED = "model.key_destroyed"
    MODEL_KEY_READ_FAILED = "model.key_read_failed"
    MODEL_KEY_ERASED = "model.key_erased"
    MODEL_KEY_ERASURE_VERIFIED = "model.key_erasure_verified"
    MODEL_KEY_ERASURE_PENDING = "model.key_erasure_pending"
    MODEL_CATALOG_SEEDED = "model.catalog_seeded"
    MODEL_CATALOG_ENTRY_ADDED = "model.catalog_entry_added"
    MODEL_CATALOG_ENTRY_EDITED = "model.catalog_entry_edited"
    MODEL_CATALOG_APPROVED = "model.catalog_approved"
    MODEL_CATALOG_PAUSED = "model.catalog_paused"
    MODEL_CATALOG_RETIRED = "model.catalog_retired"
    MODEL_CATALOG_ENTRY_REMOVED = "model.catalog_entry_removed"
    MODEL_CATALOG_PRICE_SET = "model.catalog_price_set"
    MODEL_CATALOG_REGIONS_SET = "model.catalog_regions_set"
    MODEL_ASSIGNMENT_SET = "model.assignment_set"
    OPERATOR_RECOVERY_CODE_ISSUED = "operator.recovery_code_issued"
    OPERATOR_RECOVERY_CODE_USED = "operator.recovery_code_used"
    MINTER_ANCHOR_RECORDED = "minter.anchor_recorded"
    MINTER_ANCHOR_CONFLICT = "minter.anchor_conflict"
    # Verified Merge (spec/verified-merge LA-1; actors per type in core.vm_events, F-8).
    # Additive: payloads hold ids, hashes, states, reason codes and times only (ND60).
    MERGE_EVIDENCE_RECORDED = "merge.evidence_recorded"
    MERGE_REVIEW_REQUESTED = "merge.review_requested"
    MERGE_REVIEW_FAILED = "merge.review_failed"
    MERGE_REVIEW_COST = "merge.review_cost"
    MERGE_REVIEW_RESET = "merge.review_reset"
    MERGE_VERDICT_ISSUED = "merge.verdict_issued"
    MERGE_APPROVAL_RECORDED = "merge.approval_recorded"
    MERGE_APPROVAL_WITHDRAWN = "merge.approval_withdrawn"
    MERGE_APPROVER_ENROLLED = "merge.approver_enrolled"
    MERGE_KEY_CHANGED = "merge.key_changed"
    MERGE_GATE_DECIDED = "merge.gate_decided"
    MERGE_QUEUE_REVERIFIED = "merge.queue_reverified"
    MERGE_COMPLETED = "merge.completed"
    MERGE_UNVERIFIED_LANDING = "merge.unverified_landing"
    MERGE_OVERRIDE = "merge.override"
    ANCHOR_KEY_ROTATED = "anchor.key_rotated"


_CONNECTION_FACT_KEYS = frozenset(
    {
        "provider",
        "endpoint_id",
        "auth",
        "key",
        "endpoint",
        "status",
        "reason_hash",
        "old_endpoint",
        "old_key",
        "key_ref",
        "via",
    }
)
CONNECTION_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "model.connection_created": _CONNECTION_FACT_KEYS,
    "model.connection_updated": _CONNECTION_FACT_KEYS,
    "model.connection_removed": frozenset({"provider", "endpoint_id", "reason_hash", "via"}),
    "model.connection_test_reserved": frozenset(
        {"provider", "endpoint_id", "connection_seq", "via"}
    ),
    "model.connection_tested": frozenset(
        {
            "provider",
            "endpoint_id",
            "endpoint",
            "connection_seq",
            "reservation_seq",
            "ok",
            "code",
            "status_class",
            "latency_ms",
            "probe",
            "via",
        }
    ),
    "model.connection_used": frozenset(
        {
            "stage",
            "provider",
            "endpoint_id",
            "endpoint",
            "connection_seq",
            "set_by",
            "set_at",
            "models",
            "agents",
            "data_class",
            "processing",
        }
    ),
}
"""The closed set of payload keys of each `model.connection_*` entry (R1.33 2.3.4). `key` and
`old_key` hold a validated key NAME (or null), `endpoint_id` a validated `EndpointId`; no key can
hold free text, so none can hold a secret. A test appends through the services and checks it."""


def format_time(at: datetime) -> str:
    """The canonical timestamp text: UTC with microseconds, for example
    `2026-10-20T12:00:00.000000Z`."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class LedgerEvent(BaseModel):
    """One ledger entry. Build new ones with `next_event`; never edit stored ones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream: StreamId
    seq: int = Field(ge=1)
    type: TypeName
    actor: Actor
    at: datetime
    run_id: Slug | None = None
    task_id: Slug | None = None
    payload: Mapping[str, Any] = Field(default_factory=dict)
    cost: Money | None = None
    cost_basis: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$", strict=True)] | None = (
        None
    )
    prev_hash: Hash
    hash: Hash

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def _canonical_payload(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            size = len(canonical_json(value))
        except TypeError as exc:  # pydantic only turns ValueError into a validation error
            raise ValueError(str(exc)) from exc
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES}")
        stored: Mapping[str, Any] = canonical_value(value)  # deep, read-only, storage form
        return stored

    @field_serializer("payload")
    def _plain_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        plain: dict[str, Any] = thaw(value)
        return plain

    def __hash__(self) -> int:
        return hash(self.hash)

    def hashed_content(self) -> dict[str, Any]:
        """Everything the hash covers: all fields except `hash`, in canonical form."""
        return event_content(
            stream=self.stream,
            seq=self.seq,
            type=self.type,
            actor=self.actor,
            at=self.at,
            run_id=self.run_id,
            task_id=self.task_id,
            payload=self.payload,
            cost=self.cost,
            cost_basis=self.cost_basis,
            prev_hash=self.prev_hash,
        )

    def computed_hash(self) -> str:
        """What `hash` should be for this content."""
        return sha256_hex(canonical_json(self.hashed_content()))


def event_content(
    *,
    stream: str,
    seq: int,
    type: str,
    actor: str,
    at: datetime,
    run_id: str | None,
    task_id: str | None,
    payload: Mapping[str, Any],
    cost: Money | None,
    cost_basis: str | None,
    prev_hash: str,
) -> dict[str, Any]:
    """The canonical dictionary an event's hash is computed over (spec: docs/architecture.md)."""
    return {
        "actor": actor,
        "at": format_time(at),
        "cost": None if cost is None else cost.to_json(),
        "cost_basis": None if cost_basis is None else str(cost_basis),
        "payload": thaw(payload),
        "prev_hash": prev_hash,
        "run_id": run_id,
        "seq": seq,
        "stream": stream,
        "task_id": task_id,
        "type": str(type),
    }


def _check_cost_basis(cost: Money | None, basis: CostBasis | None) -> None:
    """Writers must pair costs with a basis (loading stays lenient so damage can be reported)."""
    if basis is not None and not isinstance(basis, CostBasis):
        raise ValueError(f"unknown cost basis {basis!r}")
    if cost is not None and basis in (None, CostBasis.UNKNOWN):
        raise ValueError("a cost needs a basis: actual, provisional or reserved")
    if cost is None and basis not in (None, CostBasis.UNKNOWN):
        raise ValueError(f"cost basis '{basis}' needs a cost amount")


_EXTENSION_EVENT_TYPES: set[str] = set()
_EXTENSION_TYPE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", re.ASCII)


def register_event_types(*names: str) -> None:
    """Allow writers to append events of these types (a plugin's, ND72). A name is lower-case
    words joined by dots, never one of the built-in `EventType` values. Registering the same name
    twice is harmless."""
    for name in names:
        if not _EXTENSION_TYPE.fullmatch(name):
            raise ValueError(f"event type {name!r} is not lower-case words joined by dots")
        if name in {t.value for t in EventType}:
            raise ValueError(f"event type {name!r} is built in")
        _EXTENSION_EVENT_TYPES.add(name)


def next_event(
    previous: LedgerEvent | None,
    *,
    stream: str,
    type: EventType | str,
    actor: str,
    at: datetime,
    run_id: str | None = None,
    task_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    cost: Money | None = None,
    cost_basis: CostBasis | None = None,
) -> LedgerEvent:
    """The event that follows `previous` in `stream` (or starts it when `previous` is None)."""
    if isinstance(type, EventType):
        type_name = type.value
    elif isinstance(type, str) and type in _EXTENSION_EVENT_TYPES:
        type_name = type
    else:
        raise ValueError(f"unknown event type {type!r}; writers must use EventType")
    if type_name.startswith("merge."):
        # Verified Merge F-8 / GC-13: who may append which `merge.*` event is enforced here, on
        # the one append path, so no store or route can write a verdict as another actor.
        # Local import: `vm_events` imports `EventType` from this module.
        from nuramesh.core.vm_events import check_merge_actor

        refused = check_merge_actor(type_name, actor)
        if refused is not None:
            raise ValueError(f"{type_name} may not be appended by this actor ({refused})")
    _check_cost_basis(cost, cost_basis)
    if previous is not None and previous.stream != stream:
        raise ValueError(f"previous event is in stream '{previous.stream}', not '{stream}'")
    if previous is not None and at.astimezone(UTC) < previous.at:
        raise ValueError("ledger time can't go backwards within a stream")
    seq = 1 if previous is None else previous.seq + 1
    prev_hash = GENESIS_HASH if previous is None else previous.hash
    body = payload or {}
    draft: dict[str, Any] = {
        "stream": stream,
        "seq": seq,
        "type": type_name,
        "actor": actor,
        "at": at,
        "run_id": run_id,
        "task_id": task_id,
        "payload": body,
        "cost": cost,
        "cost_basis": cost_basis,
        "prev_hash": prev_hash,
    }
    # Validate first (normalises the timestamp to UTC), then hash the validated content.
    unhashed = LedgerEvent.model_validate({**draft, "hash": GENESIS_HASH})
    return unhashed.model_copy(update={"hash": unhashed.computed_hash()})


class ChainProblem(StrEnum):
    """Why verification stopped."""

    WRONG_STREAM = "wrong_stream"
    WRONG_SEQ = "wrong_seq"
    BROKEN_LINK = "broken_link"
    BAD_HASH = "bad_hash"
    TIME_WENT_BACKWARDS = "time_went_backwards"
    BAD_TIME = "bad_time"
    """A stored entry or head holds a time that is not a real time (a store reports this; the
    pure chain check never sees it because an event's `at` is already a datetime)."""


class ChainReport(BaseModel):
    """Result of `verify_chain`. `ok` is True only if every event checked out."""

    model_config = ConfigDict(frozen=True)

    stream: str | None
    checked: int
    ok: bool
    problem: ChainProblem | None = None
    at_seq: int | None = None
    detail: str | None = None
    head_hash: str | None = None

    @classmethod
    def failure(
        cls, stream: str | None, checked: int, problem: ChainProblem, seq: int, detail: str
    ) -> Self:
        """A report that stops at the first problem."""
        return cls(
            stream=stream, checked=checked, ok=False, problem=problem, at_seq=seq, detail=detail
        )


def verify_chain(
    events: Iterable[LedgerEvent],
    *,
    expected_head: str | None = None,
    expected_stream: str | None = None,
) -> ChainReport:
    """Check one stream from its first event: numbering, links, hashes and time order.

    Stops at the first problem. `expected_head`, if given, must equal the last event's hash; it
    catches events removed from the end, which a chain alone can't show. `expected_stream`, if
    given, must be every event's stream (catches a store returning the wrong, valid chain).
    """
    stream: str | None = expected_stream
    previous: LedgerEvent | None = None
    checked = 0
    for event in events:
        if stream is None:
            stream = event.stream
        expected_seq = 1 if previous is None else previous.seq + 1
        if event.stream != stream:
            return ChainReport.failure(
                stream, checked, ChainProblem.WRONG_STREAM, event.seq, f"found '{event.stream}'"
            )
        if event.seq != expected_seq:
            return ChainReport.failure(
                stream, checked, ChainProblem.WRONG_SEQ, event.seq, f"expected {expected_seq}"
            )
        expected_prev = GENESIS_HASH if previous is None else previous.hash
        if event.prev_hash != expected_prev:
            return ChainReport.failure(
                stream, checked, ChainProblem.BROKEN_LINK, event.seq, "prev_hash doesn't match"
            )
        if event.computed_hash() != event.hash:
            return ChainReport.failure(
                stream, checked, ChainProblem.BAD_HASH, event.seq, "content doesn't match its hash"
            )
        if previous is not None and event.at < previous.at:
            return ChainReport.failure(
                stream, checked, ChainProblem.TIME_WENT_BACKWARDS, event.seq, "earlier than seq-1"
            )
        previous = event
        checked += 1
    head = previous.hash if previous is not None else None
    if expected_head is not None and head != expected_head:
        last_seq = previous.seq if previous is not None else 0
        return ChainReport.failure(
            stream,
            checked,
            ChainProblem.BROKEN_LINK,
            last_seq,
            "the last event isn't the recorded head (events missing at the end?)",
        )
    return ChainReport(stream=stream, checked=checked, ok=True, head_hash=head)
