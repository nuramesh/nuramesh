"""Earned autonomy in the engine (E13 + E14, `spec/autonomy/README.md`): reads, writers, overlay.

The pure maths is in `nuramesh.core` (`autonomy`, `scorecard`, `proposals`, `overlay`). This module
reads the ledger of one organization (its `org:<id>` stream and the run streams that started under
it), calls the maths, and writes the human decisions of section 5, the demotion and void entries of
6.4 and 6.5, and the `config_loaded` record of section 2. Every write runs in one transaction;
the stores serialize writers, which is the exclusive lock on the organization stream the spec asks
for. Anything unreadable raises `AutonomyUnavailable` and the caller fails closed (`ask`, 503).

Two narrow seams: `autonomy_events` (organization-stream decisions, one indexed read of the
projection, T136) and `store_config` / `load_config` (the full configuration, kept in the content
store under its keyed hash, ND118).
"""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from nuramesh.core.autonomy import (
    AUTONOMY_KINDS,
    DEFAULT_EXPIRY_DAYS,
    GRANTABLE_KINDS,
    KIND_CEILING,
    KIND_CONFIG,
    KIND_CONFIG_SWAP,
    KIND_DEMOTION,
    KIND_GRANT,
    KIND_PROMOTION,
    KIND_REVOKE,
    KIND_SETTING,
    NEVER_GRANTABLE_KINDS,
    SCAN_BOUND,
    Entry,
    GateKindOf,
    Grant,
    GrantState,
    Settings,
    ceiling_of,
    config_document,
    config_payload,
    current_config_content,
    decision_kind,
    entry_id,
    expires_at,
    grant_states,
    missing_voids,
    parse_grant,
    setting_problem,
    swapped_config_content,
    voids_for_new_config,
)
from nuramesh.core.canonical import canonical_json
from nuramesh.core.content import ContentKind
from nuramesh.core.evals import EvalConfig, EvalRecord, check_eligibility
from nuramesh.core.ledger import EventType, LedgerEvent, format_time, next_event
from nuramesh.core.model_epoch import FLOOR_KINDS, KIND_FLOOR, floor_at
from nuramesh.core.org import OrgGraph
from nuramesh.core.overlay import Overlaid, effective, unavailable
from nuramesh.core.people import is_human_id
from nuramesh.core.policy import Action, Policy, PolicySet
from nuramesh.core.proposals import (
    EvalBlocked,
    Proposal,
    evaluate_cell,
    proposals_for,
    settings_at,
)
from nuramesh.core.scorecard import ScorecardError, SubjectScope, config_at, make_period, scorecard
from nuramesh.engine.content import REASON_DAYS, ContentService
from nuramesh.stores.base import EventDraft, ProjectionError, Store, UnitOfWork

ORG_KINDS = frozenset(AUTONOMY_KINDS | {KIND_PROMOTION, KIND_FLOOR})
SYSTEM = "system"


class AutonomyError(Exception):
    """A refused request: HTTP `status`, a stable `code`, a message, and extra fields."""

    def __init__(
        self, status: int, code: str, detail: str, extra: Mapping[str, Any] | None = None
    ) -> None:
        self.status, self.code, self.detail = status, code, detail
        self.extra = dict(extra or {})
        super().__init__(detail)


class AutonomyUnavailable(AutonomyError):
    """The grants or the evidence cannot be read: fail closed (503 `autonomy_unavailable`)."""

    def __init__(self, detail: str = "earned autonomy unavailable") -> None:
        super().__init__(503, "autonomy_unavailable", detail)


class AutonomyScanBound(AutonomyUnavailable):
    """More than `SCAN_BOUND` entries to read (scorecards answer 422 `range_too_large`)."""


def org_stream(org_id: str) -> str:
    return f"org:{org_id}"


# -- the seams ---------------------------------------------------------------------------------


def autonomy_events(
    uow: UnitOfWork, org_id: str, kinds: Collection[str] = ORG_KINDS
) -> list[Entry]:
    """Every entry of the organization stream whose `decision.recorded` kind is in `kinds`, in
    ledger order with commit positions. One indexed read of the autonomy projection (decision
    T136), never a scan of the stream; a projection that disagrees with the ledger raises
    `ProjectionError`, which is unavailable (fail closed)."""
    try:
        rows = uow.autonomy_events(org_id, kinds, streams="org")
    except ProjectionError as exc:
        raise AutonomyUnavailable() from exc
    stream = org_stream(org_id)
    return [Entry(p, e) for p, e in rows if e.stream == stream and decision_kind(e) in kinds]


def store_config(
    uow: UnitOfWork, content: ContentService, org_id: str, document: Mapping[str, Any]
) -> str:
    """Keep the full configuration in the content store, content-addressed (ND118), through this
    unit of work, and return its keyed hash for the `config_loaded` entry. A configuration that
    cannot be kept (key destroyed, store refuses) raises: it is not used."""
    text = canonical_json(document).decode("utf-8")
    return content.capture_config(uow, org_id, text)


def load_config(
    uow: UnitOfWork, content: ContentService, org_id: str, config_hash: str
) -> Mapping[str, Any] | None:
    """The stored full configuration of the `config_loaded` entry with this `config_hash`, or
    None (recorded without content, expired, or its key destroyed)."""
    items = autonomy_events(uow, org_id, {KIND_CONFIG, KIND_CONFIG_SWAP})
    swaps = swapped_config_content([i.event for i in items])
    for item in reversed(items):
        if decision_kind(item.event) != KIND_CONFIG:
            continue
        if item.event.payload.get("config_hash") != config_hash:
            continue
        content_id = item.event.payload.get("config_content")
        if not isinstance(content_id, str):
            return None
        content_id = current_config_content(swaps, content_id)
        text = content.read_unledgered(org_id, content_id)
        if text is None:
            return None
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else None
    return None


# -- keeping the configuration in use readable (ND194) --------------------------------------------

KIND_RECAPTURE = "config_recaptured"
KIND_CONTENT_SETTING = "content_settings_set"  # R11b: a change of `content.reason_days`
KIND_RECAPTURE_FAILED = "config_recapture_failed"
RECAPTURE_ACTOR = "system:config-recapture"
RECAPTURE_WINDOW = timedelta(days=30)


def _current_config(uow: UnitOfWork, org_id: str) -> tuple[str, str | None] | None:
    """The content id and `config_hash` of the latest `config_loaded` entry, or None."""
    items = autonomy_events(uow, org_id, {KIND_CONFIG, KIND_CONFIG_SWAP})
    configs = [i for i in items if decision_kind(i.event) == KIND_CONFIG]
    if not configs:
        return None
    payload = configs[-1].event.payload
    kept = payload.get("config_content")
    if not isinstance(kept, str):
        return None
    kept = current_config_content(swapped_config_content([i.event for i in items]), kept)
    hashed = payload.get("config_hash")
    return kept, hashed if isinstance(hashed, str) else None


def recapture_configs(store: Store, content: ContentService) -> int:
    """The re-capture sweep (spec 2, ND194): for every organization whose current configuration
    content is within 30 days of its expiry (and not yet past it), write the same content again
    with a fresh clock and append one `config_recaptured` entry (ids, hashes and times only,
    actor `system:config-recapture`). One transaction per record, so a failure rolls back with
    its entry and the next record is still tried. Idempotent: a re-captured record leaves the
    window. Never revives expired content; never touches a shredded, held or replaced
    configuration. Returns how many were re-captured."""
    now = content._clock().astimezone(UTC)
    done = 0
    for due in store.expiring_content(ContentKind.CONFIG, now, now + RECAPTURE_WINDOW):
        try:
            with store.transaction() as uow:
                record = uow.get_content(due.org_id, due.id)
                if (
                    record is None
                    or record.expires_at is None
                    or not now < record.expires_at <= now + RECAPTURE_WINDOW
                    or uow.legal_hold(due.org_id)
                    or content.unreadable_at(uow, due.org_id, due.id, now)
                ):
                    continue
                current = _current_config(uow, due.org_id)
                if current is None or current[0] != due.id:
                    continue
                text = content.read_in(
                    uow, due.org_id, due.id, actor=RECAPTURE_ACTOR, why="recapture"
                )
                if text is None:
                    continue
                kept = content.capture_config(uow, due.org_id, text)
                if kept != due.id:
                    raise AutonomyUnavailable("re-captured content has another hash")
                stored = uow.get_content(due.org_id, due.id)  # the expiry as written, exactly
                if stored is None or stored.expires_at is None:
                    raise AutonomyUnavailable("re-captured content has no expiry")
                _append(
                    uow,
                    due.org_id,
                    {
                        "kind": KIND_RECAPTURE,
                        "config_content": due.id,
                        "config_hash": current[1],
                        "previous_expires_at": format_time(record.expires_at),
                        "expires_at": format_time(stored.expires_at),
                    },
                    actor=RECAPTURE_ACTOR,
                )
                done += 1
        except Exception as exc:
            _record_recapture_failure(store, due, now, exc)
    return done


def _org_kind_events(uow: UnitOfWork, org_id: str, kinds: Collection[str]) -> list[LedgerEvent]:
    """The organization stream's `decision.recorded` entries of these kinds, in ledger order.
    A read of the stream itself (the autonomy projection does not index the re-capture kinds,
    which no policy decision reads); used only on the rare paths: a lapsed configuration and a
    failed re-capture."""
    stream, after = org_stream(org_id), 0
    found: list[LedgerEvent] = []
    while batch := uow.events_after(stream, after, 500):
        found.extend(e for e in batch if decision_kind(e) in kinds)
        after = batch[-1].seq
    return found


def _record_recapture_failure(store: Store, due: Any, now: datetime, exc: Exception) -> None:
    """A failed re-capture is ledgered (ids, times and an error code only, no content), once per
    cause: the same error for the same content is not written again until a re-capture succeeds.
    Best effort: a failure here is retried by the next sweep."""
    code = type(exc).__name__[:64]
    try:
        with store.transaction() as uow:
            seen = _org_kind_events(uow, due.org_id, {KIND_RECAPTURE, KIND_RECAPTURE_FAILED})
            for event in reversed(seen):
                p = event.payload
                if p.get("config_content") != due.id:
                    continue
                if p.get("kind") == KIND_RECAPTURE_FAILED and p.get("error") == code:
                    return
                break
            current = _current_config(uow, due.org_id)
            _append(
                uow,
                due.org_id,
                {
                    "kind": KIND_RECAPTURE_FAILED,
                    "config_content": due.id,
                    "config_hash": current[1] if current is not None else None,
                    "previous_expires_at": format_time(due.expires_at)
                    if due.expires_at is not None
                    else None,
                    "attempted_at": format_time(now),
                    "error": code,
                },
                actor=RECAPTURE_ACTOR,
            )
    except Exception:  # noqa: S110
        pass


def _lapsed_expiry(uow: UnitOfWork, org_id: str, content_id: str, days: int) -> datetime | None:
    """When the (now absent) current configuration content expired, from the ledger: the latest
    `config_recaptured` expiry for it, else its `config_loaded` time plus the retention."""
    kinds = {KIND_CONFIG, KIND_RECAPTURE, KIND_CONFIG_SWAP}
    for event in reversed(_org_kind_events(uow, org_id, kinds)):
        p = event.payload
        key = "new_content" if p.get("kind") == KIND_CONFIG_SWAP else "config_content"
        if p.get(key) != content_id:
            continue
        if p.get("kind") in (KIND_RECAPTURE, KIND_CONFIG_SWAP) and isinstance(
            p.get("expires_at"), str
        ):
            return datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00")).astimezone(UTC)
        return event.at.astimezone(UTC) + timedelta(days=days)
    return None


def config_expiry_warning(
    store: Store, content: ContentService, org_id: str
) -> dict[str, Any] | None:
    """The owner's warning: the current configuration content expires within 30 days (or has
    expired) and has not been re-captured. None for a shredded or unconfigured organization."""
    now = content._clock().astimezone(UTC)
    try:
        with store.transaction() as uow:
            current = _current_config(uow, org_id)
            if current is None:
                return None
            record = uow.get_content(org_id, current[0])
            if uow.legal_hold(org_id):
                return None
            if uow.key_tombstone(org_id) is not None or content.keys.tombstone(org_id) is not None:
                return None
            if record is None:  # the expiry sweep deleted it: still told, from the ledger
                expires_at = _lapsed_expiry(
                    uow, org_id, current[0], content.config_retention_days()
                )
            else:
                expires_at = record.expires_at
            if expires_at is None:
                return None
            left = expires_at - now
    except Exception:
        return None
    if left > RECAPTURE_WINDOW:
        return None
    expired = left <= timedelta(0)
    return {
        "code": "config_expired" if expired else "config_expiring",
        "expires_at": format_time(expires_at),
        "days_left": 0 if expired else left.days,
    }


# -- reading the ledger of one organization ------------------------------------------------------


def org_entries(uow: UnitOfWork, org_id: str, after: int | None = None) -> list[Entry]:
    """The evidence entries that belong to the organization: its own stream and the streams of
    runs that started under it, from the autonomy projection (only the kinds the reads look at,
    one indexed read each). With `after`, entries committed after that position plus the
    organization stream and the run context (`policy.decided`, `task.transitioned`,
    `run.started`) from before it, because a verdict is credited to the assignee of its task at
    that time and a denial joins the `policy.decided` entry of its approval. More than
    `SCAN_BOUND` entries, or a projection that disagrees with the ledger: unavailable."""
    try:
        rows = uow.autonomy_events(org_id)
    except ProjectionError as exc:
        raise AutonomyUnavailable() from exc
    stream = org_stream(org_id)
    # spec 6.1: step 2 reads (and is bounded on) what was committed after the oldest grant; the
    # organization-stream entries before it (step 1) have their own bound. Older run evidence is
    # context only and counts toward neither.
    # `after` None is "from the beginning": rows from before migration 16 have no commit
    # position (read as 0) and must not be dropped by it (spec 9: never a partial number).
    if after is None:
        kept = [Entry(p, e) for p, e in rows]
        if len(kept) > SCAN_BOUND:
            raise AutonomyScanBound()
        return kept
    kept = [
        Entry(p, e) for p, e in rows if p > after or e.stream == stream or e.type in _CONTEXT_TYPES
    ]
    if sum(1 for x in kept if x.pos > after) > SCAN_BOUND:
        raise AutonomyScanBound()
    if sum(1 for x in kept if x.pos <= after and x.event.stream == stream) > SCAN_BOUND:
        raise AutonomyScanBound()
    return kept


_CONTEXT_TYPES = frozenset({"policy.decided", "task.transitioned", "run.started"})


def required_suites_of(config: EvalConfig | None) -> dict[str, frozenset[str]]:
    """Role to the suites it requires (what an `eval.scored` failure must name to be a trigger)."""
    if config is None:
        return {}
    return {r.role: frozenset(r.suites) for r in config.requirements}


def gate_kind_reader(uow: UnitOfWork) -> GateKindOf:
    """The kind of the gate behind a `gate.completed` entry, looked up in the task's definition of
    done; a payload `kind` wins (fixtures); an unknown gate counts as `custom` (never silently
    dropped from a failure streak)."""
    cache: dict[tuple[str, str], str] = {}

    def kind_of(event: LedgerEvent) -> str | None:
        direct = event.payload.get("kind")
        if isinstance(direct, str):
            return direct
        gate = event.payload.get("gate")
        if event.task_id is None or not isinstance(gate, str):
            return "custom"
        key = (event.task_id, gate)
        if key not in cache:
            stored = uow.get_task(event.task_id)
            try:
                cache[key] = (
                    stored.task.definition_of_done.gate(gate).kind.value
                    if stored is not None
                    else "custom"
                )
            except KeyError:
                cache[key] = "custom"
        return cache[key]

    return kind_of


def eval_blocked_reader(
    entries: Sequence[Entry], config: EvalConfig | None, as_of: datetime
) -> EvalBlocked:
    """`eval_blocked` for proposals: the subject (an agent; or any agent of a role, fail closed)
    is not eligible by `check_eligibility` at `as_of`, on the results that exist by then."""
    cfg = config_at(entries, as_of)
    records: list[EvalRecord] = []
    for item in entries:
        if item.event.type == EventType.EVAL_SCORED.value and item.event.at <= as_of:
            try:
                records.append(EvalRecord.from_event(item.event))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                raise AutonomyUnavailable("an eval result is unreadable") from None

    def blocked(scope: SubjectScope) -> bool:
        if config is None or cfg is None:
            return False
        for agent in sorted(scope.agents):
            model = cfg.agents[agent][1] if agent in cfg.agents else None
            if not check_eligibility(
                config, role=scope.role, agent=agent, model=model, records=records, now=as_of
            ).eligible:
                return True
        return False

    return blocked


def eval_rows(
    entries: Sequence[Entry], config: EvalConfig | None, kind: str, subject: str, as_of: datetime
) -> list[dict[str, Any]]:
    """The scorecard's `evals` rows: one per suite the subject's role requires."""
    cfg = config_at(entries, as_of)
    if config is None or cfg is None:
        return []
    role = cfg.agents[subject][0] if kind == "agent" and subject in cfg.agents else subject
    records = [
        EvalRecord.from_event(i.event)
        for i in entries
        if i.event.type == EventType.EVAL_SCORED.value and i.event.at <= as_of
    ]
    agent = subject if kind == "agent" else ""
    model = cfg.agents[subject][1] if kind == "agent" and subject in cfg.agents else None
    reasons = check_eligibility(
        config, role=role, agent=agent, model=model, records=records, now=as_of
    ).reasons
    rows: list[dict[str, Any]] = []
    for suite in config.required_for(role):
        why = next((r for r in reasons if r.startswith(f"suite '{suite.id}':")), None)
        rows.append(
            {
                "suite": suite.id,
                "status": "blocked" if why else "satisfied",
                "reason": why.split(": ", 1)[1] if why else None,
            }
        )
    return rows


@dataclass(frozen=True)
class Reading:
    """What one read of an organization's ledger holds."""

    entries: list[Entry]
    position: int
    states: dict[str, GrantState] = field(default_factory=dict)


def read_org(uow: UnitOfWork, org_id: str) -> Reading:
    return Reading(org_entries(uow, org_id), uow.commit_position())


# -- the configuration record (spec 2, 6.5) ------------------------------------------------------


def _probe(at: datetime, payload: Mapping[str, Any], org_id: str) -> Callable[..., LedgerEvent]:
    def make(pos: int, when: datetime, body: Mapping[str, Any]) -> LedgerEvent:
        return next_event(
            None,
            stream=org_stream(org_id),
            type=EventType.DECISION_RECORDED,
            actor=SYSTEM,
            at=when,
            payload=body,
        )

    return make


def ensure_config(
    uow: UnitOfWork,
    org: OrgGraph,
    policies: PolicySet,
    now: datetime,
    *,
    required_suites: Mapping[str, frozenset[str]],
    settings: Settings | None = None,
    content: ContentService | None = None,
) -> bool:
    """Record the configuration about to be used when it differs from the latest recorded one,
    with the `autonomy_void` entries the change causes, in this transaction and before any policy
    check can use it (spec 2). True if a record was written. If this raises, the configuration is
    not used."""
    if content is not None:
        _record_reason_days(uow, org.id, content)
    payload = config_payload(org, policies)
    stored = autonomy_events(uow, org.id)
    latest = next((i for i in reversed(stored) if decision_kind(i.event) == KIND_CONFIG), None)
    if latest is not None and latest.event.payload.get("config_hash") == payload["config_hash"]:
        return False
    voids: list[dict[str, Any]] = []
    revoked = {
        i.event.payload.get("grant_id")
        for i in stored
        if decision_kind(i.event) == KIND_REVOKE and i.event.at <= now
    }
    # spec 6.1 step 2: scan only from the oldest grant that is neither revoked nor expired
    live = [
        g
        for i in stored
        if (g := parse_grant(i)) is not None and g.id not in revoked and now < g.expires_at
    ]
    if live:
        entries = org_entries(uow, org.id, after=min(g.pos for g in live) - 1)
        voids = voids_for_new_config(
            entries,
            payload,
            now,
            required_suites=required_suites,
            gate_kind_of=gate_kind_reader(uow),
            settings=settings or settings_at(stored, now),
            event_factory=_probe(now, payload, org.id),
        )
    if content is not None:  # ND118: the full configuration, content-addressed (after the scan)
        payload = {
            **payload,
            "config_content": store_config(uow, content, org.id, config_document(org, policies)),
        }
    _append(uow, org.id, payload)
    for void in voids:
        _append(uow, org.id, void)
    return True


def _record_reason_days(uow: UnitOfWork, org_id: str, content: ContentService) -> None:
    """R11b: one `content_settings_set` entry (integers only) when the effective retention of a
    person's reason here (`reason_retention_days`: 90 on a hosted deployment) differs from the
    last one recorded (the 396-day default if none). The stream is read only when the value
    cached on the service does not already equal the new one: once on first load and after a
    change, never per policy check (T136). The cache is set only from a confirmed read, so a
    rolled-back append is read again, never trusted."""
    new = content.reason_retention_days()
    confirmed: dict[str, int] = content.__dict__.setdefault("_recorded_reason_days", {})
    if confirmed.get(org_id) == new:
        return
    last = _org_kind_events(uow, org_id, {KIND_CONTENT_SETTING})
    old = last[-1].payload.get("new") if last else REASON_DAYS
    if old != new:
        _append(
            uow,
            org_id,
            {"kind": KIND_CONTENT_SETTING, "setting": "reason_days", "old": old, "new": new},
        )
    else:
        confirmed[org_id] = new


def record_config(
    store: Store,
    org: OrgGraph,
    policies: PolicySet,
    evals: EvalConfig | None,
    *,
    content: ContentService | None = None,
) -> bool:
    """The load path (spec 2, 7 item 4): record the configuration about to be used, with the voids
    it causes, in one transaction before anything uses it (RunEngine start, `nuramesh try`, the
    server's config load). A failure propagates: the configuration is not used."""
    with store.transaction() as uow:
        return ensure_config(
            uow,
            org,
            policies,
            uow.at,
            required_suites=required_suites_of(evals),
            content=content,
        )


def _append(
    uow: UnitOfWork,
    org_id: str,
    payload: Mapping[str, Any],
    actor: str = SYSTEM,
) -> LedgerEvent:
    return uow.append(
        EventDraft(
            stream=org_stream(org_id),
            type=EventType.DECISION_RECORDED,
            actor=actor,
            payload=dict(payload),
        )
    )


# -- the overlay inside one policy check (spec 6.1) ----------------------------------------------


def overlay_check(
    uow: UnitOfWork,
    org_id: str,
    policy: Policy | None,
    action: Action,
    *,
    agent: str,
    role: str,
    required_suites: Mapping[str, frozenset[str]],
    content: ContentService | None = None,
) -> Overlaid:
    """The decision of one check under the active grants. Only called when the plain verdict is
    `ask`. Anything unreadable gives the fail-closed `ask`. Writes the backstop `autonomy_void`
    entries it finds missing. An approval is never consumed here. Every time decision uses the
    unit of work's store time (ND243); no caller supplies a clock."""
    now = uow.at
    try:
        stored = autonomy_events(uow, org_id)
        grants = [g for i in stored if (g := parse_grant(i)) is not None]
        revoked = {
            i.event.payload.get("grant_id")
            for i in stored
            if decision_kind(i.event) == KIND_REVOKE and i.event.at <= now
        }
        live = [g for g in grants if g.id not in revoked and now < g.expires_at]
        if not live:
            return effective(policy, action, agent=agent, role=role, states={})
        configs = [i for i in stored if decision_kind(i.event) == KIND_CONFIG]
        kept = configs[-1].event.payload.get("config_content") if configs else None
        if isinstance(kept, str):
            kept = current_config_content(swapped_config_content([i.event for i in stored]), kept)
        if isinstance(kept, str) and (
            content is None or content.unreadable_at(uow, org_id, kept, now)
        ):
            # ND188: content that expired, was shredded or cannot be checked (no content service)
            # is unreadable, so the grant asks (default deny)
            return unavailable(policy, action)
        entries = org_entries(uow, org_id, after=min(g.pos for g in live) - 1)
        states = grant_states(
            entries,
            now,
            required_suites=required_suites,
            gate_kind_of=gate_kind_reader(uow),
            settings=settings_at(stored, now),
        )
        for void in missing_voids(entries, states, now):
            _append(uow, org_id, void)
        return effective(policy, action, agent=agent, role=role, states=states)
    except Exception:  # noqa: BLE001  any failure to read or derive leaves the ask (fail closed)
        return unavailable(policy, action)


# -- reads: scorecards, proposals, grants (spec 3, 4, 5.4) -------------------------------------


def _settings(entries: Sequence[Entry], as_of: datetime) -> Settings:
    return settings_at(entries, as_of)


def read_scorecard(
    store: Store,
    org_id: str,
    *,
    kind: str,
    subject: str,
    as_of: datetime,
    start: datetime | None,
    end: datetime | None,
    min_results: int,
    evals: EvalConfig | None,
) -> dict[str, Any]:
    """One scorecard. `ScorecardError` for 400/404; 422 `range_too_large`, 503
    `scorecard_unavailable` when the ledger cannot be read in full."""
    period, minimum = make_period(start, end, as_of, min_results)
    with store.transaction() as uow:
        try:
            entries = org_entries(uow, org_id)
        except AutonomyScanBound as exc:
            raise ScorecardError(422, "range_too_large", "more than 100000 entries") from exc
        except AutonomyUnavailable as exc:
            raise ScorecardError(503, "scorecard_unavailable", "an entry is unreadable") from exc
        try:
            rows = eval_rows(entries, evals, kind, subject, as_of)
            return scorecard(
                entries,
                kind=kind,
                subject=subject,
                period=period,
                min_results=minimum,
                evals=rows,
                required_suites=required_suites_of(evals),
                gate_kind_of=gate_kind_reader(uow),
                settings=_settings(entries, as_of),
            )
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise ScorecardError(503, "scorecard_unavailable", "an entry is unreadable") from exc


def read_proposals(
    store: Store,
    org_id: str,
    *,
    kind: str,
    subject: str,
    as_of: datetime,
    evals: EvalConfig | None,
    rule_key: int | None = None,
) -> tuple[list[Proposal], int]:
    """The proposals of a subject and the ledger position they were read at."""
    with store.transaction() as uow:
        try:
            entries = org_entries(uow, org_id)
            blocked = eval_blocked_reader(entries, evals, as_of)
            common: dict[str, Any] = {
                "kind": kind,
                "subject": subject,
                "as_of": as_of,
                "required_suites": required_suites_of(evals),
                "eval_blocked": blocked,
                "gate_kind_of": gate_kind_reader(uow),
            }
            if rule_key is not None:
                found = [evaluate_cell(entries, rule_key=rule_key, **common)]
            else:
                found = proposals_for(entries, **common)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise AutonomyUnavailable() from exc
        return found, uow.commit_position()


def list_grants(
    store: Store, org_id: str, *, as_of: datetime, evals: EvalConfig | None, active_only: bool
) -> list[dict[str, Any]]:
    """Grants with their state at `as_of` (spec 5.4)."""
    with store.transaction() as uow:
        try:
            entries = org_entries(uow, org_id)
            states = grant_states(
                entries,
                as_of,
                required_suites=required_suites_of(evals),
                gate_kind_of=gate_kind_reader(uow),
                settings=_settings(entries, as_of),
            )
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            raise AutonomyUnavailable() from exc
    out = []
    for state in states.values():
        if state.state == "not_yet" and not active_only:
            continue
        if active_only and not state.active:
            continue
        g = state.grant
        out.append(
            {
                "grant_id": g.id,
                "subject": {"kind": g.subject_kind, "id": g.subject_id},
                "role": g.role,
                "rule_key": g.rule_key,
                "action_kind": g.action_kind,
                "state": state.state,
                "granted_at": g.granted_at.isoformat(),
                "expires_at": g.expires_at.isoformat(),
                "void_reason": state.void_reason,
                "promotion_entry": state.promotion_entry,
                "trigger": state.trigger,
                "entry": g.entry,
            }
        )
    return out


# -- human decisions (spec 5) --------------------------------------------------------------------


def _need_human(actor: str, roles: Collection[str], role: str) -> None:
    if not is_human_id(actor):
        raise AutonomyError(403, "human_required", "only a person can decide this")
    if role not in roles:
        raise AutonomyError(403, "forbidden", "your role may not do this")


def _reason(uow: UnitOfWork, content: ContentService, org_id: str, reason: str) -> str:
    text = reason.strip()
    if not text:
        raise AutonomyError(400, "invalid_request", "a reason is required")
    return content.capture_reason(uow, org_id, text)


def grant(
    store: Store,
    org_id: str,
    *,
    actor: str,
    actor_role: str,
    kind: str,
    subject: str,
    rule_key: int,
    as_of: datetime,
    ledger_position: int,
    evidence_hash: str,
    expires_in_days: int = DEFAULT_EXPIRY_DAYS,
    reason: str,
    evals: EvalConfig | None,
    content: ContentService,
    grant_id: str,
) -> dict[str, Any]:
    """Run `grant_in` in its own transaction."""
    with store.transaction() as uow:
        return grant_in(
            uow,
            org_id,
            actor=actor,
            actor_role=actor_role,
            kind=kind,
            subject=subject,
            rule_key=rule_key,
            as_of=as_of,
            ledger_position=ledger_position,
            evidence_hash=evidence_hash,
            expires_in_days=expires_in_days,
            reason=reason,
            evals=evals,
            content=content,
            grant_id=grant_id,
        )


def grant_in(
    uow: UnitOfWork,
    org_id: str,
    *,
    actor: str,
    actor_role: str,
    kind: str,
    subject: str,
    rule_key: int,
    as_of: datetime,
    ledger_position: int,
    evidence_hash: str,
    expires_in_days: int = DEFAULT_EXPIRY_DAYS,
    reason: str,
    evals: EvalConfig | None,
    content: ContentService,
    grant_id: str,
) -> dict[str, Any]:
    """Record a grant (spec 5.1): the checks in the spec's order, in one transaction."""
    _need_human(actor, {"owner"}, actor_role)
    # The checks run at the unit of work's one store-clock time (ND234), raised to the org
    # stream's last event when the clock went backwards: exactly the time `append` gives the
    # entry (ND230). So `granted_at` is the entry's `at` and the evidence hash is recomputable
    # from the ledger alone at that `at` (spec Q9); the payload holds only `expires_in_days`.
    at = uow.at
    last = uow.last_event(org_stream(org_id))
    if last is not None and last.at > at:
        at = last.at
    entries = org_entries(uow, org_id)
    cfg = config_at(entries, at)
    if cfg is None:
        raise AutonomyError(404, "not_found", "no configuration is recorded")
    settings = _settings(entries, at)
    try:
        if (
            kind not in ("agent", "role")
            or (kind == "agent" and subject not in cfg.agents)
            or (kind == "role" and subject not in cfg.roles)
        ):
            raise AutonomyError(404, "not_found", f"{kind} '{subject}' is not in the organization")
    except AttributeError:
        raise AutonomyUnavailable() from None
    if not 1 <= expires_in_days <= settings.max_expiry_days or not reason.strip():
        raise AutonomyError(400, "invalid_request", "expiry or reason is not valid")
    if not 0 <= ledger_position <= uow.commit_position():
        # a position the store never issued would switch step 3b off (spec 5.1)
        raise AutonomyError(400, "invalid_request", "ledger_position is not a position")
    required = required_suites_of(evals)
    kinds = gate_kind_reader(uow)

    def propose(when: datetime) -> Proposal:
        return evaluate_cell(
            entries,
            kind=kind,
            subject=subject,
            rule_key=rule_key,
            as_of=when,
            required_suites=required,
            eval_blocked=eval_blocked_reader(entries, evals, when),
            gate_kind_of=kinds,
            settings=settings,
        )

    shown = propose(as_of)
    if shown.evidence_hash != evidence_hash:
        raise AutonomyError(409, "evidence_changed", "the evidence is not what the ledger says")
    late = _triggers_after(entries, ledger_position, shown, cfg, kinds)
    if late:
        raise AutonomyError(409, "evidence_changed", "a trigger was committed after you looked")
    now_proposal = propose(at)
    if now_proposal.status == "already_granted":
        raise AutonomyError(409, "already_granted", "an active grant exists")
    if now_proposal.status != "proposable":
        raise AutonomyError(
            409,
            "not_proposable",
            "the cell is not proposable",
            {"reasons": list(now_proposal.reasons)},
        )
    reason_hash = _reason(uow, content, org_id, reason)
    role = shown.role
    rule_hash = str(now_proposal.evidence["rule_hash"])
    payload = {
        "kind": KIND_GRANT,
        "grant_id": grant_id,
        "subject_kind": kind,
        "subject_id": subject,
        "role": role,
        "rule_key": rule_key,
        "rule_hash": rule_hash,
        "action_kind": now_proposal.action_kind,
        "from": "ask",
        "to": "allow",
        "policy_contract": cfg.contract,
        "expires_in_days": expires_in_days,
        "evidence_hash": now_proposal.evidence_hash,
        "scorecard_version": 1,
        "approver": actor,
        "reason_hash": reason_hash,
    }
    event = _append(uow, org_id, payload, actor)
    # the entry's own `at` (assigned by the store inside the append) is the grant time (spec 5.1)
    return {
        **payload,
        "granted_at": event.at.isoformat(),
        "expires_at": expires_at(event.at, expires_in_days).isoformat(),
        "entry": entry_id(event),
    }


def _triggers_after(
    entries: Sequence[Entry],
    position: int,
    proposal: Proposal,
    cfg: Any,
    kind_of: GateKindOf,
) -> bool:
    """Step 3b: a failed verdict, denial, breach or failed eval attributable to the subject
    committed after `position` (whatever its `at`). Counts failed verdicts only."""
    from nuramesh.core.autonomy import verdicts_of

    agents = (
        {proposal.subject_id}
        if proposal.subject_kind == "agent"
        else {a for a, (r, _) in cfg.agents.items() if r == proposal.role}
    )
    for item in entries:
        if item.pos <= position:
            continue
        e, p = item.event, item.event.payload
        if e.type == "budget.breached" and (p.get("agent") is None or p.get("agent") in agents):
            return True
        if e.type == "approval.decided" and p.get("approved") is False and p.get("agent") in agents:
            return True
        if e.type == "eval.scored" and p.get("passed") is False:
            who = p.get("subject_id")
            if p.get("subject_kind") == "role" and who == proposal.role:
                return True
            if who in agents:
                return True
    return any(
        not v.passed and v.pos > position and v.agent in agents
        for v in verdicts_of(entries, kind_of)
    )


def revoke(
    store: Store,
    org_id: str,
    grant_id: str,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    evals: EvalConfig | None,
    content: ContentService,
) -> dict[str, Any]:
    """Run `revoke_in` in its own transaction."""
    with store.transaction() as uow:
        return revoke_in(
            uow,
            org_id,
            grant_id,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            evals=evals,
            content=content,
        )


def revoke_in(
    uow: UnitOfWork,
    org_id: str,
    grant_id: str,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    evals: EvalConfig | None,
    content: ContentService,
) -> dict[str, Any]:
    """Revoke a grant (spec 5.2): owner or approver, a person."""
    _need_human(actor, {"owner", "approver"}, actor_role)
    at = uow.now()
    entries = org_entries(uow, org_id)
    states = grant_states(
        entries,
        at,
        required_suites=required_suites_of(evals),
        gate_kind_of=gate_kind_reader(uow),
        settings=_settings(entries, at),
    )
    state = states.get(grant_id)
    if state is None:
        raise AutonomyError(404, "not_found", f"grant '{grant_id}' doesn't exist")
    if not state.active:
        raise AutonomyError(409, "not_active", f"the grant is {state.state}")
    reason_hash = _reason(uow, content, org_id, reason)
    event = _append(
        uow,
        org_id,
        {
            "kind": KIND_REVOKE,
            "grant_id": grant_id,
            "approver": actor,
            "reason_hash": reason_hash,
        },
        actor,
    )
    return {"grant_id": grant_id, "entry": entry_id(event)}


def set_ceiling(
    store: Store,
    org_id: str,
    role: str,
    kind: str,
    value: str,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Run `set_ceiling_in` in its own transaction."""
    with store.transaction() as uow:
        return set_ceiling_in(
            uow,
            org_id,
            role,
            kind,
            value,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            content=content,
        )


def set_ceiling_in(
    uow: UnitOfWork,
    org_id: str,
    role: str,
    kind: str,
    value: str,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Set the ceiling of `(role, kind)` (spec 5.3). Raising is owner-only; lowering owner or
    approver; setting the current value writes nothing."""
    if not is_human_id(actor):
        raise AutonomyError(403, "human_required", "only a person can decide this")
    if value not in ("none", "allow"):
        raise AutonomyError(400, "invalid_request", "value is none or allow")
    if value == "allow" and kind in NEVER_GRANTABLE_KINDS:
        raise AutonomyError(400, "never_grantable", f"'{kind}' can never be granted")
    if kind not in GRANTABLE_KINDS | NEVER_GRANTABLE_KINDS:
        raise AutonomyError(400, "invalid_request", f"unknown action kind '{kind}'")
    at = uow.now()
    entries = autonomy_events(uow, org_id)
    old = ceiling_of(entries, role, kind, at)
    if value == old:
        return {"role": role, "kind": kind, "value": value, "entry": None}
    allowed = {"owner"} if value == "allow" else {"owner", "approver"}
    if actor_role not in allowed:
        raise AutonomyError(403, "forbidden", "your role may not do this")
    reason_hash = _reason(uow, content, org_id, reason)
    event = _append(
        uow,
        org_id,
        {
            "kind": KIND_CEILING,
            "role": role,
            "action_kind": kind,
            "old": old,
            "new": value,
            "approver": actor,
            "reason_hash": reason_hash,
        },
        actor,
    )
    return {"role": role, "kind": kind, "value": value, "entry": entry_id(event)}


def set_floor(
    store: Store,
    org_id: str,
    role: str,
    kinds: Any,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Run `set_floor_in` in its own transaction."""
    with store.transaction() as uow:
        return set_floor_in(
            uow, org_id, role, kinds, actor=actor, actor_role=actor_role, reason=reason,
            content=content,
        )  # fmt: skip


def set_floor_in(
    uow: UnitOfWork,
    org_id: str,
    role: str,
    kinds: Any,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Set the model-change floor of `role` (spec/model-change MC6): the only writer of
    `model_change_floor_set`. A person who is the owner only; `kinds` must be a list of floor
    kinds (else 400 `not_floor_kind`); setting the value in force writes nothing."""
    _need_human(actor, {"owner"}, actor_role)
    if not isinstance(kinds, (list, tuple)) or not all(
        isinstance(k, str) and k in FLOOR_KINDS for k in kinds
    ):
        raise AutonomyError(
            400, "not_floor_kind", "kinds must be a list of: " + ", ".join(sorted(FLOOR_KINDS))
        )
    if not reason.strip():
        raise AutonomyError(400, "invalid_request", "a reason is required")
    new = sorted(set(kinds))
    entries = autonomy_events(uow, org_id, {KIND_FLOOR})
    old = sorted(floor_at(entries, role, 2**62))
    if new == old:
        return {"role": role, "kinds": new, "entry": None}
    reason_hash = _reason(uow, content, org_id, reason)
    event = _append(
        uow,
        org_id,
        {
            "kind": KIND_FLOOR,
            "role": role,
            "kinds": new,
            "old_kinds": old,
            "approver": actor,
            "reason_hash": reason_hash,
        },
        actor,
    )
    return {"role": role, "kinds": new, "entry": entry_id(event)}


def set_setting(
    store: Store,
    org_id: str,
    name: str,
    value: int | Decimal,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Run `set_setting_in` in its own transaction."""
    with store.transaction() as uow:
        return set_setting_in(
            uow,
            org_id,
            name,
            value,
            actor=actor,
            actor_role=actor_role,
            reason=reason,
            content=content,
        )


def set_setting_in(
    uow: UnitOfWork,
    org_id: str,
    name: str,
    value: int | Decimal,
    *,
    actor: str,
    actor_role: str,
    reason: str,
    content: ContentService,
) -> dict[str, Any]:
    """Make a threshold stricter (spec 8): owner only, a person, never looser than the default."""
    _need_human(actor, {"owner"}, actor_role)
    at = uow.now()
    entries = autonomy_events(uow, org_id)
    current = settings_at(entries, at)
    problem = setting_problem(name, value, current)
    if problem is not None:
        raise AutonomyError(400, problem[0], problem[1])
    old = {
        "MIN_APPROVED": current.min_approved,
        "MIN_VERDICTS": current.min_verdicts,
        "MIN_PASS_RATE": current.min_pass_rate,
        "COOLDOWN_DAYS": current.cooldown_days,
        "MAX_EXPIRY_DAYS": current.max_expiry_days,
        "STREAK_FAILS": current.streak_fails,
        "STREAK_LENGTH": current.streak_length,
    }[name]
    reason_hash = _reason(uow, content, org_id, reason)
    event = _append(
        uow,
        org_id,
        {
            "kind": KIND_SETTING,
            "name": name,
            "old": str(old),
            "new": str(value),
            "approver": actor,
            "reason_hash": reason_hash,
        },
        actor,
    )
    return {"name": name, "old": str(old), "new": str(value), "entry": entry_id(event)}


def record_demotion(
    store: Store,
    org_id: str,
    grant_id: str,
    *,
    evals: EvalConfig | None,
) -> bool:
    """Append the `autonomy_demotion` entry for a demoted grant, once (spec 6.4). False when
    the grant is not demoted by derivation or one was already written."""
    with store.transaction() as uow:
        at = uow.now()
        entries = org_entries(uow, org_id)
        states = grant_states(
            entries,
            at,
            required_suites=required_suites_of(evals),
            gate_kind_of=gate_kind_reader(uow),
            settings=_settings(entries, at),
        )
        state = states.get(grant_id)
        if state is None or state.state != "demoted" or state.trigger_entry is None:
            return False
        if any(
            decision_kind(i.event) == KIND_DEMOTION and i.event.payload.get("grant_id") == grant_id
            for i in entries
        ):
            return False
        _append(
            uow,
            org_id,
            {
                "kind": KIND_DEMOTION,
                "grant_id": grant_id,
                "trigger": state.trigger,
                "trigger_entry": state.trigger_entry,
                "demoted_at": at.isoformat(),
            },
        )
        return True


__all__ = [
    "AutonomyError",
    "AutonomyScanBound",
    "AutonomyUnavailable",
    "Grant",
    "autonomy_events",
    "ensure_config",
    "record_config",
    "grant",
    "list_grants",
    "load_config",
    "overlay_check",
    "read_proposals",
    "read_scorecard",
    "record_demotion",
    "revoke",
    "set_ceiling",
    "set_setting",
    "store_config",
]
