"""The model catalog, assignments and connection endpoints (ND80 Q6, ND92 spec section 10.3).

- Reads: `GET /v1/models/catalog` and `/assignments` (read); `GET /v1/models/catalog/events`,
  `/connections` and `/resolution` (admin: they carry endpoints, who set them and the reasons).
- Writes (admin, a person in a session with the CSRF token; a token is `human_required`): add, edit,
  approve, pause, retire and remove a catalog entry, add a price version, set the customer regions,
  assign a model to an agent or role, put or remove a connection, and test a connection.

Approval is only ever a recorded act of a person through this admin API (the team configuration
merely seeds the catalog, `engine.catalog_service.ensure_seed`). Every refusal appends nothing.
There is no field that could hold a key: bodies reject unknown fields, only keyless providers
connect, and every change is a minimal ledger fact. A reason is stored in the content store and the
ledger keeps its keyed hash.

M4's fixed `admin` and `builder` roles are not built: the existing `admin` permission (the `owner`
role, a person only) stands in.
"""

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from nuramesh.config.shipped_prices import ShippedList, load_shipped, price_facts
from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.content import ContentKind
from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import (
    CATALOG_EVENT_TYPES,
    CatalogEntry,
    CatalogFold,
    ConnectionRecord,
    accepted_classes,
    apply_catalog_event,
    connection_key,
    effective_ref,
    effective_role_default,
    new_fold,
    seed_payload,
)
from nuramesh.core.models import ENDPOINT_NOT_ALLOWED, U_ENDPOINT_OTHER
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.core.prices import (
    P_BOUND_SUPPLIED,
    PriceBasis,
    PriceBook,
    UnknownPriceError,
    resolve_price,
)
from nuramesh.core.residency import DATA_CLASSES
from nuramesh.engine.azure_openai import (
    ProbeTransport,
    TokenProvider,
)
from nuramesh.engine.catalog_service import (
    CatalogRefused,
    CatalogResult,
    CatalogService,
    ensure_seed,
    residency_violations,
)
from nuramesh.engine.connection_history import read_history
from nuramesh.engine.connection_rules import (
    SUPPORTED_PROVIDERS,
    connection_problem,
    default_endpoint,
)
from nuramesh.engine.connection_test import ConnectionTester, TestRefused
from nuramesh.engine.content import ContentService, KeyDestroyedError, OrphanedContentError
from nuramesh.engine.credentials import CredentialSource
from nuramesh.engine.governance import Governance
from nuramesh.engine.key_readers import KeyReaders
from nuramesh.engine.model_connections import ConnectionRefused, ModelConnections
from nuramesh.engine.openai_compatible import U_FIELD
from nuramesh.engine.org_halt import halt_predicate
from nuramesh.engine.org_ledger import PAGE, OrgLedger, org_stream
from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.openapi_text import ALREADY_200
from nuramesh.server.pagination import (
    Cursor,
    decode_position,
    encode_cursor,
    position,
    refuse_old_parameters,
    respond,
    seq_position,
)
from nuramesh.server.problems import problem_response as render_problem
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores.base import Store, UnitOfWork

NO_STORE = {"Cache-Control": "no-store"}
LOG = logging.getLogger("nuramesh.server.models")
MAX_SEQ = 2**62
"""The upper bound of a `before` query (the same as `server.app.MAX_SEQ`; a test pins them)."""
REASON_MAX = 5_000
"""A bound on the request text only; the service enforces the 3 to 500 characters."""


class _Reasoned(Body):
    reason: str | None = Field(default=None, max_length=REASON_MAX)


class EntryBody(_Reasoned):
    """A catalog entry to add."""

    provider: str = Field(max_length=100)
    endpoint_id: str | None = Field(default=None, max_length=100)
    model: str = Field(max_length=200)
    family: str = Field(max_length=100)
    processing: str = Field(max_length=100)


class EditBody(_Reasoned):
    """A change of an entry's family and/or processing scope."""

    provider: str = Field(max_length=100)
    endpoint_id: str | None = Field(default=None, max_length=100)
    model: str = Field(max_length=200)
    family: str | None = Field(default=None, max_length=100)
    processing: str | None = Field(default=None, max_length=100)


class RefBody(_Reasoned):
    """An entry named by provider and model (approve, pause, retire, remove)."""

    provider: str = Field(max_length=100)
    endpoint_id: str | None = Field(default=None, max_length=100)
    model: str = Field(max_length=200)


class PriceBody(_Reasoned):
    """One price version. Rates are decimal text; a JSON number is refused. The bound endpoint
    is set by the service (spec openai-compatible 12.3) and a body that names it is refused."""

    @model_validator(mode="before")
    @classmethod
    def _no_binding(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(k in data for k in ("bound_endpoint", "bound_epoch")):
            raise PydanticCustomError("field_refused", "{text}", {"text": P_BOUND_SUPPLIED})
        return data

    provider: str = Field(max_length=100)
    endpoint_id: str | None = Field(default=None, max_length=100)
    model: str = Field(max_length=200)
    input_per_million: str = Field(max_length=40)
    output_per_million: str = Field(max_length=40)
    cached_input_per_million: str | None = Field(default=None, max_length=40)
    cache_write_per_million: str | None = Field(default=None, max_length=40)
    effective_from: str = Field(max_length=40)
    source: str = Field(max_length=1_000)


class RegionsBody(_Reasoned):
    """The customer's chosen regions (ND45)."""

    customer_regions: list[str] = Field(max_length=1_000)


class AssignmentBody(_Reasoned):
    """A model for an agent or role; `model` null clears the ledger assignment."""

    scope: str = Field(max_length=20)
    id: str = Field(max_length=100)
    model: str | None = Field(max_length=300)


class AssignmentPutBody(_Reasoned):
    """The body of `PUT /v1/models/assignments/{scope}/{id}`: scope and id are in the path."""

    model: str | None = Field(max_length=300)


class ConnectionBody(_Reasoned):
    """A connection. Nothing else is accepted, so no secret and no file path can be sent; `key`
    is the *name* of an operator key binding (spec openai-compatible 4)."""

    endpoint: str | None = Field(default=None, max_length=300)
    status: str = Field(default="active", max_length=20)
    endpoint_id: str | None = Field(default=None, max_length=100)
    auth: str | None = Field(default=None, max_length=20)
    key: str | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _no_path(cls, data: Any) -> Any:
        """A key file path, or any field a connection body does not have, is U-field."""
        if isinstance(data, dict) and "key_ref" in data:
            text = "a key reference is written only by the key route"
            raise PydanticCustomError("key_ref_server_only", "{text}", {"text": text})
        if isinstance(data, dict) and any(k not in cls.model_fields for k in data):
            raise PydanticCustomError("field_refused", "{text}", {"text": U_FIELD})
        return data


class OptionalReasonBody(_Reasoned):
    """A body whose only field is an optional reason."""


@dataclass
class ModelServices:
    """Everything the model routes use for one organization."""

    config: NuraMeshConfig
    store: Store
    ledger: OrgLedger
    catalog: CatalogService
    connections: ModelConnections
    tester: ConnectionTester
    governance: Governance
    content: ContentService | None
    _seeded: bool = False
    _seed_lock: threading.Lock = field(default_factory=threading.Lock)

    def ensure_seeded(self) -> None:
        """Seed the catalog from the configuration before the first read (once per process; the
        write itself is one transaction, so two processes write exactly one seed). Done on first
        use rather than at construction so that building the app never touches the store; a
        failure is not remembered, so the next request tries again. The startup warnings are
        logged here, once."""
        if self._seeded:
            return
        with self._seed_lock:
            if self._seeded:
                return
            status = ensure_seed(
                self.store,
                self.config.org.id,
                self.config.models,
                ledger=self.ledger,
                base_prices=self.config.prices,
            )
            for message in status.messages:
                LOG.warning(message)
            self._seeded = True


def build_model_services(
    store: Store,
    config: NuraMeshConfig,
    *,
    content: ContentService | None = None,
    halted: Callable[[], bool] | None = None,
    token_provider: TokenProvider | None = None,
    probe_transport: ProbeTransport | None = None,
    probe_clock: Callable[[], float] | None = None,
    key_readers: KeyReaders | None = None,
) -> ModelServices:
    """Build the services (nothing is read or written here; `ModelServices.ensure_seeded` seeds).

    The halt predicate is the organization's real halt state (E19, ND112): `HaltWatch` over the org
    stream, which raises when the stream cannot be read so every user fails closed. `halted` is
    only an override for tests. `key_readers` (one reader per org) lets the connection test read a
    `stored` key; without it a `stored` connection is refused (`auth_kind`), fail closed."""
    org = config.org
    ledger = OrgLedger(store, org.id, config.prices)
    is_halted = halted if halted is not None else halt_predicate(store, org.id)
    catalog = CatalogService(
        store, org, ledger, prices=config.prices, overrides=config.price_overrides, content=content
    )
    connections = ModelConnections(store, org, ledger, content=content, catalog_lock=catalog.lock)
    tester = ConnectionTester(
        store,
        org.id,
        ledger,
        credentials=(
            CredentialSource(entra=token_provider)
            if key_readers is None
            else key_readers.source_for(org.id, entra=token_provider)
        ),
        halted=is_halted,
        transport=probe_transport,
        clock=time.monotonic if probe_clock is None else probe_clock,
    )
    governance = Governance(
        store,
        org,
        prices=config.prices,
        overrides=config.price_overrides,
        ledger=ledger,
        models=config.models,
        halted=is_halted,
    )
    return ModelServices(config, store, ledger, catalog, connections, tester, governance, content)


# -- views -----------------------------------------------------------------------------------------


def connection_view(record: ConnectionRecord) -> dict[str, Any]:
    """What the API shows: the facts, who last set them and when."""
    return {
        **record.facts,
        "set_by": record.set_by,
        "set_at": record.set_at.isoformat(),
        "seq": record.seq,
    }


def price_view(
    config: NuraMeshConfig,
    book: PriceBook,
    provider: str,
    model: str,
    at: datetime,
    endpoint_id: str | None = None,
    shipped: ShippedList | None = None,
) -> dict[str, Any] | None:
    """The price in effect at `at`, or None (unknown). An owner override counts as provisional.
    With `shipped` the view also says where the price comes from (`origin`: shipped, operator or
    override), the list row's `retrieved_on`, its age in days and whether it is stale (spec
    default-prices 6.2); those three are null unless the price is a shipped one."""
    try:
        resolved = resolve_price(
            book, provider, model, at, overrides=config.price_overrides, endpoint_id=endpoint_id
        )
    except (UnknownPriceError, ValueError):  # no price (or an ambiguous override): unknown
        return None
    cached = resolved.cached_input_per_million
    known = resolved.basis is PriceBasis.KNOWN and resolved.price is not None
    if known:
        assert resolved.price is not None
        effective_from, source = resolved.price.effective_from, resolved.price.source
    else:
        assert resolved.override is not None
        effective_from, source = resolved.override.set_at, "owner override"
    view: dict[str, Any] = {
        "currency": resolved.currency.value,
        "input_per_million": str(resolved.input_per_million),
        "output_per_million": str(resolved.output_per_million),
        "cached_input_per_million": None if cached is None else str(cached),
        "effective_from": effective_from.isoformat(),
        "source": source,
        "provisional": not known,
    }
    if shipped is not None:
        facts = price_facts(book, shipped, config.price_overrides, provider, model, at, endpoint_id)
        view["origin"] = facts.origin
        view["retrieved_on"] = (
            None if facts.retrieved_on is None else facts.retrieved_on.isoformat()
        )
        view["age_days"] = facts.age_days
        view["stale"] = facts.stale
    return view


def entry_summary(entry: CatalogEntry) -> dict[str, str]:
    """The stored fields of one entry (`endpoint_id` only for a provider that has one)."""
    return {
        "provider": entry.provider,
        **({"endpoint_id": entry.endpoint_id} if entry.endpoint_id is not None else {}),
        "model": entry.model,
        "family": entry.family,
        "processing": entry.processing,
        "status": entry.status,
    }


def config_drift(config: NuraMeshConfig, fold: CatalogFold) -> bool:
    """Whether the configuration's `models:` part differs from the catalog in force (the ledger
    wins; spec section 2.3 item 4)."""
    if config.models is None:
        return False
    if fold.seeded:
        return bool(fold.config_hash != seed_payload(config.models)["config_hash"])
    return fold.exists


def result_view(result: CatalogResult, *, listing: Iterable[str] = ()) -> dict[str, Any]:
    """The answer to a write: what it did and the ledger position it created."""
    body: dict[str, Any] = {"already": result.already, "ledger_seq": result.ledger_seq}
    body["events"] = list(result.events)
    if result.entry is not None:
        body["entry"] = entry_summary(result.entry)
    if result.previous is not None:
        body["previous"] = result.previous
    if "affected" in listing:
        body["affected"] = list(result.affected)
    if isinstance(result.repaused, bool):
        if "repaused" in listing:
            body["repaused"] = result.repaused
    else:
        body["repaused"] = list(result.repaused)
    if result.paused_because is not None:
        body["paused_because"] = result.paused_because
    if result.price is not None:
        p = result.price
        body["price"] = {
            "provider": p.provider,
            "model": p.model,
            "input_per_million": str(p.input_per_million),
            "output_per_million": str(p.output_per_million),
            "effective_from": p.effective_from.isoformat(),
            "source": p.source,
        }
    if result.customer_regions is not None:
        body["customer_regions"] = list(result.customer_regions)
    if result.assignment is not None:
        body["assignment"] = result.assignment
    return body


def reveal_reasons(
    content: ContentService | None, org_id: str, by: str, hashes: Sequence[str | None]
) -> list[str | None]:
    """The reason text behind each hash, for a read that shows reasons (R1.27-H H3, ND164 Q2 = c).

    Every text shown is ledgered as the reveal route does (`content.reveal(..., why="api")`: who
    and which hash, never the text), so the caller passes only the hashes of the rows it returns.
    A hash that is None, absent, expired, erased, not a reason, or unreadable gives None and writes
    nothing; it is never a failure. Call it after the stream scan so the new `content.read`
    entries never sit inside the stream being read."""
    out: list[str | None] = []
    for reason_hash in hashes:
        text: str | None = None
        if content is not None and reason_hash is not None:
            try:
                info = content.describe(org_id, reason_hash)
                if info is not None and info.kind == ContentKind.REASON:
                    text = content.reveal(org_id, reason_hash, actor=by, why="api")
            except Exception:  # unreadable is the same as expired: null, never a failure
                text = None
        out.append(text)
    return out


def _problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return render_problem(status, code, detail, headers=NO_STORE, **extra)


class _NoConnection(Exception):
    """No such connection (a 404 from inside a keyed transaction: it rolls back, nothing stored)."""


def install_models(
    app: FastAPI,
    config: NuraMeshConfig | None,
    services: ModelServices | None,
    caller: Callable[..., Caller],
    now: Callable[[], datetime],
    idem: Callable[..., IdemContext],
) -> None:
    """Add the model routes (each has a row in `authz.ROUTE_RULES` and `routes.ROUTE_SCOPES`)."""

    @app.exception_handler(CatalogRefused)
    async def _catalog_refused(_: Request, exc: CatalogRefused) -> JSONResponse:
        return _problem(
            exc.status,
            exc.code,
            exc.message,
            problems=[{"path": p, "message": m} for p, m in exc.problems],
        )

    @app.exception_handler(KeyDestroyedError)
    @app.exception_handler(OrphanedContentError)
    async def _key_refused(_: Request, exc: Exception) -> JSONResponse:
        """A reason that can't be stored because the organization's key is destroyed or missing
        (R1.37 section 8): 409, the same answer and one coalesced security-log row as the other
        capture routes. The write rolled back, so nothing was changed."""
        assert isinstance(exc, KeyDestroyedError | OrphanedContentError)
        if services is not None and services.content is not None:
            services.content.flush_alerts()
            kind = "tombstone" if isinstance(exc, KeyDestroyedError) else "orphaned_content"
            services.content.record_security_count(
                services.config.org.id, "content.capture_refused", kind
            )
        return _problem(409, str(exc.code), str(exc))

    @app.exception_handler(ConnectionRefused)
    async def _connection_refused(_: Request, exc: ConnectionRefused) -> JSONResponse:
        code = "key_required" if exc.path == "$.provider" else exc.code
        message = exc.problem
        if code == "key_required":
            message = f"{message}; supported providers: {', '.join(SUPPORTED_PROVIDERS)}"
        return _problem(422, code, message, problems=[{"path": exc.path, "message": message}])

    @app.exception_handler(TestRefused)
    async def _test_refused(_: Request, exc: TestRefused) -> JSONResponse:
        extra: dict[str, Any] = {}
        reply = _problem(exc.status, exc.code, exc.text)
        if exc.retry_after_seconds is not None:
            extra["retry_after_seconds"] = exc.retry_after_seconds
            reply = _problem(exc.status, exc.code, exc.text, **extra)
            reply.headers["Retry-After"] = str(exc.retry_after_seconds)
        return reply

    def need() -> ModelServices:
        if services is None or config is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        services.ensure_seeded()
        return services

    def person(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(person)]
    Idem = Annotated[IdemContext, Depends(idem)]
    Svc = Annotated[ModelServices, Depends(need)]

    def answer(result: CatalogResult, *, listing: Iterable[str] = ()) -> Response:
        return JSONResponse(
            result_view(result, listing=listing), status_code=result.status, headers=NO_STORE
        )

    # -- reads ---------------------------------------------------------------------------------

    @app.get("/v1/models/catalog")
    def get_catalog(
        svc: Svc, data_class: Annotated[str | None, Query(max_length=32)] = None
    ) -> Response:
        if data_class is not None and data_class not in DATA_CLASSES:
            return _problem(
                422,
                "invalid_request",
                "data_class is synthetic or customer",
                problems=[
                    {"path": "$.data_class", "message": "data_class is synthetic or customer"}
                ],
            )
        view = svc.ledger.view()
        fold = view.catalog
        book = fold.price_book(svc.config.prices, view.connections)
        connected = {
            connection_key(c.provider, c.endpoint_id)
            for c in view.connections.records()
            if c.status == "active"
        }
        at = now()
        shipped = load_shipped()
        entries: list[dict[str, Any]] = []
        for key, entry in fold.entries.items():
            classes = accepted_classes(entry.processing, fold.customer_regions)
            if data_class is not None and data_class not in classes:
                continue
            entries.append(
                {
                    **entry_summary(entry),
                    "data_classes": list(classes),
                    "price": price_view(
                        svc.config,
                        book,
                        entry.provider,
                        entry.model,
                        at,
                        entry.endpoint_id,
                        shipped=shipped,
                    ),
                    "connected": connection_key(entry.provider, entry.endpoint_id) in connected,
                    "origin": fold.origin.get(key),
                    "updated_by": fold.updated_by.get(key),
                    "updated_at": (
                        fold.updated_at[key].isoformat() if key in fold.updated_at else None
                    ),
                }
            )
        return JSONResponse(
            {
                "seeded": fold.seeded,
                "config_drift": config_drift(svc.config, fold),
                "ignored_events": fold.ignored,
                "customer_regions": list(fold.customer_regions),
                "entries": entries,
            },
            headers=NO_STORE,
        )

    @app.get("/v1/models/catalog/events")
    def get_catalog_events(
        request: Request,
        svc: Svc,
        by: By,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        before: Annotated[int | None, Query(ge=0, le=MAX_SEQ)] = None,
        cursor: Cursor = None,
    ) -> Response:
        """The `limit` newest honored events with `seq < before`, ascending within the page. The
        fold still runs from the start (it decides which events are honored); only the response,
        and the reasons read for it, are bounded."""
        route = "GET /v1/models/catalog/events"
        refuse_old_parameters(cursor, before=before)
        if cursor is not None:
            before = seq_position(decode_position(cursor, route))
        fold = new_fold(svc.config.prices)
        stream = org_stream(svc.config.org.id)
        wanted = CATALOG_EVENT_TYPES | {EventType.MODEL_ASSIGNMENT_SET.value}
        newest: deque[Any] = deque(maxlen=limit + 1)
        after, finished = 0, False
        while not finished:
            page = list(svc.store.read_stream(stream, after_seq=after, limit=PAGE))
            for event in page:
                after = event.seq
                if before is not None and event.seq >= before:
                    finished = True
                    break
                if event.type in wanted and apply_catalog_event(fold, event):
                    newest.append(event)
            if len(page) < PAGE:
                break
        events = list(newest)
        more = len(events) > limit
        if more:
            events = events[1:]
        out: list[dict[str, Any]] = []
        hashes: list[str | None] = []
        for event in events:
            payload = json.loads(json.dumps(event.payload, default=dict))
            reason_hash = payload.pop("reason_hash", None)
            hashes.append(reason_hash if isinstance(reason_hash, str) else None)
            out.append(
                {
                    "seq": event.seq,
                    "type": event.type,
                    "actor": event.actor,
                    "at": event.at.isoformat(),
                    "payload": payload,
                    "reason": None,
                }
            )
        for row, text in zip(
            out, reveal_reasons(svc.content, svc.config.org.id, by, hashes), strict=True
        ):
            row["reason"] = text
        older = out[0]["seq"] if more else None
        return respond(
            request,
            items=out,
            old_key="events",
            next_cursor=None if older is None else encode_cursor(route, str(older)),
            old_extra={"next_before": older},
            native=True,
            headers=NO_STORE,
        )

    @app.get("/v1/models/assignments")
    def get_assignments(svc: Svc) -> Response:
        fold = svc.ledger.view().catalog
        org = svc.config.org
        rows: list[dict[str, Any]] = []
        for agent in org.agents:
            found = effective_ref(org, fold.assignments, agent.id)
            level = None if found.ref is None else found.source.split()[0]
            source = None if found.ref is None else found.source.split()[1]
            rows.append(
                {
                    "subject": f"agent={agent.id}",
                    "scope": "agent",
                    "id": agent.id,
                    "role": agent.role,
                    "ref": found.ref,
                    "level": level,
                    "source": source,
                }
            )
        held = {a.role for a in org.agents}
        for role in org.roles:
            if role.id in held:
                continue
            ref = effective_role_default(org, fold.assignments, role.id)
            ledger_set = ("role", role.id) in fold.assignments
            rows.append(
                {
                    "subject": f"role={role.id}",
                    "scope": "role",
                    "id": role.id,
                    "role": role.id,
                    "ref": ref,
                    "level": None if ref is None else "role",
                    "source": None if ref is None else ("ledger" if ledger_set else "config"),
                }
            )
        return JSONResponse({"assignments": rows}, headers=NO_STORE)

    @app.get("/v1/models/resolution")
    def get_resolution(
        svc: Svc, by: By, data_class: Annotated[str, Query(max_length=32)] = "synthetic"
    ) -> Response:
        if data_class not in DATA_CLASSES:
            return _problem(
                422,
                "invalid_request",
                "data_class is synthetic or customer",
                problems=[
                    {"path": "$.data_class", "message": "data_class is synthetic or customer"}
                ],
            )
        report = svc.governance.preflight(data_class)
        return JSONResponse(
            {
                "ok": report.ok,
                "blocks": [
                    {
                        "subject": b.subject,
                        "agent": b.agent,
                        "role": b.role,
                        "provider": b.provider,
                        "model": b.model,
                        "reason": b.reason,
                        "detail": b.detail,
                        "text": b.text,
                    }
                    for b in report.blocks
                ],
                "resolutions": [
                    {
                        "subject": subject,
                        "ref": r.ref,
                        "family": r.family,
                        "processing": r.processing,
                        "endpoint": r.endpoint,
                        "connection_seq": r.connection_seq,
                        "set_by": r.set_by,
                        "set_at": None if r.set_at is None else r.set_at.isoformat(),
                        "price_basis": r.price_basis,
                    }
                    for subject, r in report.resolutions
                ],
            },
            headers=NO_STORE,
        )

    # -- writes ----------------------------------------------------------------------------------
    # Every honored write runs its `_in` variant inside `idem.effect()` (R1.35 6.1): with a key the
    # effect and the stored response are one transaction. The service lock is taken first (global
    # lock order), then the transaction.

    def catalog_write(
        idem: IdemContext,
        svc: ModelServices,
        call: Callable[[UnitOfWork], CatalogResult],
        listing: Iterable[str] = (),
    ) -> Response:
        idem.require_alive()
        with svc.catalog.locked(), idem.effect() as uow:
            result = call(uow)
            return idem.respond(
                uow, result.status, result_view(result, listing=listing), headers=NO_STORE
            )

    @app.post("/v1/models/catalog/entries", status_code=201, responses=ALREADY_200)
    def add_entry(body: EntryBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.add_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                family=body.family,
                processing=body.processing,
                reason=body.reason,
            ),
        )

    @app.post("/v1/models/catalog/entries/edit")
    @app.post("/v1/models/catalog/edit", deprecated=True)
    def edit_entry(body: EditBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.edit_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                family=body.family,
                processing=body.processing,
                reason=body.reason,
            ),
            ("repaused",),
        )

    @app.post("/v1/models/catalog/entries/approve")
    @app.post("/v1/models/catalog/approve", deprecated=True)
    def approve_entry(body: RefBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.approve_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                reason=body.reason,
            ),
        )

    @app.post("/v1/models/catalog/entries/pause")
    @app.post("/v1/models/catalog/pause", deprecated=True)
    def pause_entry(body: RefBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.pause_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                reason=body.reason,
                via="api",
            ),
            ("affected",),
        )

    @app.post("/v1/models/catalog/entries/retire")
    @app.post("/v1/models/catalog/retire", deprecated=True)
    def retire_entry(body: RefBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.retire_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                reason=body.reason,
                via="api",
            ),
            ("affected",),
        )

    @app.post("/v1/models/catalog/entries/remove")
    @app.post("/v1/models/catalog/remove", deprecated=True)
    def remove_entry(body: RefBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.remove_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                reason=body.reason,
            ),
        )

    @app.post("/v1/models/catalog/prices", status_code=201, responses=ALREADY_200)
    def add_price(body: PriceBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.set_price_in(
                uow,
                actor=by,
                provider=body.provider,
                endpoint_id=body.endpoint_id,
                model=body.model,
                input_per_million=body.input_per_million,
                output_per_million=body.output_per_million,
                cached_input_per_million=body.cached_input_per_million,
                cache_write_per_million=body.cache_write_per_million,
                effective_from=body.effective_from,
                source=body.source,
                reason=body.reason,
            ),
        )

    @app.put("/v1/models/catalog/regions")
    @app.post("/v1/models/catalog/regions", deprecated=True)
    def set_regions(body: RegionsBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.set_regions_in(
                uow, actor=by, customer_regions=body.customer_regions, reason=body.reason
            ),
        )

    @app.post("/v1/models/assignments", deprecated=True)
    def assign(body: AssignmentBody, svc: Svc, by: By, idem: Idem) -> Response:
        return catalog_write(
            idem,
            svc,
            lambda uow: svc.catalog.assign_in(
                uow,
                actor=by,
                scope=body.scope,
                subject=body.id,
                model=body.model,
                reason=body.reason,
            ),
        )

    @app.put("/v1/models/assignments/{scope}/{subject}")
    def put_assignment(
        scope: Annotated[str, Path(max_length=20)],
        subject: Annotated[str, Path(max_length=100)],
        body: AssignmentPutBody,
        svc: Svc,
        by: By,
        idem: Idem,
    ) -> Response:
        """The upsert of one assignment (R1.43 A85); `model: null` clears it. Always 200."""
        idem.require_alive()
        with svc.catalog.locked(), idem.effect() as uow:
            result = svc.catalog.assign_in(
                uow,
                actor=by,
                scope=scope,
                subject=subject,
                model=body.model,
                reason=body.reason,
            )
            return idem.respond(uow, 200, result_view(result), headers=NO_STORE)

    # -- connections -----------------------------------------------------------------------------

    @app.get("/v1/models/connections")
    def list_connections(svc: Svc, by: By) -> Response:
        records = svc.connections.list()
        return JSONResponse(
            {"connections": [connection_view(r) for r in records]}, headers=NO_STORE
        )

    @app.get("/v1/models/connections/events")
    def get_connection_events(
        request: Request,
        svc: Svc,
        by: By,
        provider: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")] = None,
        endpoint_id: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")] = None,
        before: Annotated[int | None, Query(ge=0, le=MAX_SEQ)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Cursor = None,
    ) -> Response:
        """The change history of the connections, newest page first (R1.33 spec 2.2)."""
        route = "GET /v1/models/connections/events"
        refuse_old_parameters(cursor, before=before)
        filters = f"{provider or ''}|{endpoint_id or ''}"  # a cursor opens only its own filter
        if cursor is not None:
            before = seq_position(decode_position(cursor, route, filters))
        found = read_history(
            svc.store,
            svc.config.org.id,
            provider=provider,
            endpoint_id=endpoint_id,
            before=before,
            limit=limit,
        )
        texts = reveal_reasons(
            svc.content, svc.config.org.id, by, [row.reason_hash for row in found.rows]
        )
        rows: list[dict[str, Any]] = []
        for row, text in zip(found.rows, texts, strict=True):
            shown = dict(row.data)
            if shown["valid"] and "reason" in shown:
                shown["reason"] = text
            rows.append(shown)
        older = found.next_before
        return respond(
            request,
            items=rows,
            old_key="rows",
            next_cursor=(
                None if older is None else encode_cursor(route, position(filters, str(older)))
            ),
            old_extra={"next_before": older},
            native=True,
            headers=NO_STORE,
        )

    def endpoint_id_of(query: str | None, body: str | None) -> str | None:
        """The endpoint id from the query and/or the body; both given must be equal."""
        if query is not None and body is not None and query != body:
            raise ConnectionRefused(
                "the endpoint id of the query and of the body differ", "$.endpoint_id"
            )
        return query if query is not None else body

    @app.put("/v1/models/connections/{provider}")
    def put_connection(
        provider: str,
        body: ConnectionBody,
        svc: Svc,
        by: By,
        idem: Idem,
        endpoint_id: Annotated[str | None, Query(max_length=100)] = None,
    ) -> Response:
        named = endpoint_id_of(endpoint_id, body.endpoint_id)
        fixed = default_endpoint(provider)
        if fixed is not None and body.endpoint is not None:
            raise ConnectionRefused("the endpoint of this provider is fixed", "$.endpoint")
        endpoint = fixed or (body.endpoint or "")
        if provider != OPENAI_COMPATIBLE:
            endpoint = endpoint.strip().lower()
            if body.auth is not None or body.key is not None:
                raise ConnectionRefused(
                    "this provider has a fixed credential type", "$.auth", "invalid_request"
                )
        problem = connection_problem(
            provider, endpoint, endpoint_id=named, auth=body.auth, key=body.key
        )
        if problem is not None:  # validation first: a bad request is never a tombstone refusal
            if problem == ENDPOINT_NOT_ALLOWED:
                raise ConnectionRefused(
                    U_ENDPOINT_OTHER, "$.endpoint_id", "endpoint_id_not_allowed"
                )
            raise ConnectionRefused(
                problem,
                "$.provider" if "stored key" in problem else "$.endpoint",
                "invalid_request",
            )
        idem.require_alive()
        with svc.connections.locked(), idem.effect() as uow:
            outcome = svc.connections.put_in(
                uow,
                provider,
                endpoint,
                by=by,
                status=body.status,
                reason=body.reason,
                endpoint_id=named,
                auth=body.auth,
                key=body.key,
            )
            assert outcome.record is not None
            view = connection_view(outcome.record)
            view["repaused"] = list(outcome.repaused)
            if provider == OPENAI_COMPATIBLE:
                view["residency_violations"] = residency_violations(
                    svc.connections.ledger.view(uow), named, everywhere=False
                )
            setter = outcome.record.set_by  # a no-op put shows the earlier setter (M1)
            return idem.respond(
                uow,
                201 if outcome.result == "created" else 200,
                view,
                subject=setter if setter.startswith("human:") else None,
                headers=NO_STORE,
            )

    @app.post("/v1/models/connections/{provider}/remove")
    def remove_connection(
        provider: str,
        body: OptionalReasonBody,
        svc: Svc,
        by: By,
        idem: Idem,
        endpoint_id: Annotated[str | None, Query(max_length=100)] = None,
    ) -> Response:
        if provider not in SUPPORTED_PROVIDERS:
            return _problem(404, "not_found", "no such connection")
        idem.require_alive()
        with svc.connections.locked(), idem.effect() as uow:
            if not svc.connections.remove_in(
                uow, provider, by=by, reason=body.reason, endpoint_id=endpoint_id
            ):
                raise _NoConnection
            return idem.respond(
                uow,
                200,
                {
                    "provider": provider,
                    **({"endpoint_id": endpoint_id} if endpoint_id is not None else {}),
                    "removed": True,
                },
                headers=NO_STORE,
            )

    @app.exception_handler(_NoConnection)
    async def _no_connection(_: Request, exc: Exception) -> JSONResponse:
        return _problem(404, "not_found", "no such connection")

    @app.post("/v1/models/connections/{provider}/test")
    def test_connection(
        provider: str,
        body: EmptyBody,
        svc: Svc,
        by: By,
        idem: Idem,
        endpoint_id: Annotated[str | None, Query(max_length=100)] = None,
    ) -> Response:
        idem.require_alive()
        found = svc.tester.test(provider, by=by, endpoint_id=endpoint_id)
        return JSONResponse(
            {
                "provider": found.provider,
                "ok": found.ok,
                "code": found.code,
                "text": found.text,
                "latency_ms": found.latency_ms,
                "ledger_seq": found.ledger_seq,
            },
            headers=NO_STORE,
        )
