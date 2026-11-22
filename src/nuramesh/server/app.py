"""The REST API over the engine's services (R1.19b, T36; `docs/architecture-platform.md` §5).

The API has **no business rules of its own**: it validates input, calls the engine's services
(which write state and ledger together, A1) and returns the engine's own models. Everything is
under `/v1`; errors are `application/problem+json` with stable codes.

**Who is calling (R1.22 PR 3, §11):** every route has one row in `authz.ROUTE_RULES`, applied
before the route runs. With `require_auth=True` (`nuramesh serve --auth`) every route but the
public ones needs a human cookie session or an API token, and the actor recorded for an action is
the session's `human:u_...` or the token's `service:<id>`; it is **never** taken from a request
body or header. With authentication off the server answers loopback hosts only (the CLI refuses
any other bind), reads are open and human actions are refused; `dev_human` is an in-process
harness for tests and the app's contract scripts, refused together with `require_auth`.
"""

import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from nuramesh.config.shipped_prices import ShippedPriceError, load_shipped
from nuramesh.core.auth import Permission, address_key
from nuramesh.core.breakers import DisableReason
from nuramesh.core.budgets import BudgetExceededError
from nuramesh.core.config import (
    INTEGRATIONS_NOT_ACCEPTED,
    TOP_LEVEL_KEYS,
    ConfigValidationError,
    NuraMeshConfig,
    config_json_schema,
    parse_config,
)
from nuramesh.core.content import ContentKind
from nuramesh.core.entitlements import EntitlementRequired, Entitlements, NoEntitlements
from nuramesh.core.identity import MailSender
from nuramesh.core.ledger import Actor, EventType
from nuramesh.core.model_catalog import effective_ref
from nuramesh.core.money import Money
from nuramesh.core.people import is_human_id
from nuramesh.core.policy import ApprovalError, ApprovalMode, ApprovalStatus
from nuramesh.core.presets import effective_suites
from nuramesh.core.redaction import redact_payload
from nuramesh.core.secret_store import FakeSecretStore, SecretStore
from nuramesh.engine import net_guard
from nuramesh.engine.autonomy import config_expiry_warning, record_config
from nuramesh.engine.azure_openai import ProbeTransport, TokenProvider
from nuramesh.engine.breaker_tick import SHUTDOWN_WAIT_SECONDS, BreakerTicker
from nuramesh.engine.budgets import unblock_budget_in
from nuramesh.engine.calibration import CalibrationLedgerError
from nuramesh.engine.calibration import standings as calibration_standings
from nuramesh.engine.content import (
    ContentError,
    ContentService,
    KeyDestroyedError,
    OrphanedContentError,
)
from nuramesh.engine.decision_wake import (
    WAKE_TIMEOUT_SECONDS,
    SweepState,
    WakeFold,
    deliver_held,
    parked_warning,
    place_marker,
    reconcile_decisions,
    wake_gate,
)
from nuramesh.engine.deny_checks import deny_checks
from nuramesh.engine.evals import agent_eligibility, load_eval_records
from nuramesh.engine.gate_runners import DeciderNotAllowed, GateError
from nuramesh.engine.halt_refusal import record_halt_refusal
from nuramesh.engine.human_decisions import DecisionRefused, RecordedDecision, record_decision
from nuramesh.engine.key_readers import KeyReaders
from nuramesh.engine.key_sweeper import KeySweeper, SweepLock
from nuramesh.engine.killswitch import (
    NoSuchRole,
    OrgErasedError,
    StateConflict,
    disable_breaker_in,
    enable_breaker_in,
    halt_org,
    pause_role,
    reset_breaker_in,
    resume_org_in,
    resume_role_in,
)
from nuramesh.engine.org_halt import read_org_events
from nuramesh.engine.org_ledger import OrgLedger, org_stream
from nuramesh.engine.originator import SelfApproval
from nuramesh.engine.policy import answer_approval_in
from nuramesh.engine.prompt_cache import (
    RETENTION_NOTE,
    PromptCacheHosted,
    refuse_if_hosted,
    set_prompt_cache_in,
)
from nuramesh.engine.run import build_report
from nuramesh.engine.run_control import RunFinished, request_stop_in, stop_state, takes_part
from nuramesh.engine.run_request import (
    MAX_BODY_BYTES as RUN_BODY_BYTES,
)
from nuramesh.engine.run_request import (
    PlanProblem,
    RunStartSettings,
    StartRunBody,
    derive_run_id,
    validate_shape,
)
from nuramesh.engine.run_start import record_start_request
from nuramesh.engine.solo import SoloRefused
from nuramesh.engine.solo_status import solo_status_view
from nuramesh.engine.start_budget import RunBudgetRefused, check_start_headroom
from nuramesh.engine.starter import RunIdConflict, RunStarter, StartRequest, parse_plan
from nuramesh.plugins import PluginContext
from nuramesh.server import downgrade, org_views, views
from nuramesh.server.agent_auth import AgentAuth, invalid_credential
from nuramesh.server.agent_credential_routes import install_agent_credentials
from nuramesh.server.agent_credentials import (
    AgentCredentialService,
    AgentSettings,
    agent_directory,
)
from nuramesh.server.auth import install_auth, problem_response
from nuramesh.server.authz import Authorizer, Caller
from nuramesh.server.autonomy_routes import install_autonomy
from nuramesh.server.costs_api import install_costs
from nuramesh.server.dashboards import install_dashboards
from nuramesh.server.deprecations import DeprecationHeaders, document_renames
from nuramesh.server.email_change import EmailChangeService, EmailVerifySettings
from nuramesh.server.email_change_routes import install_email_change
from nuramesh.server.extensions import RouteKit, bound_store
from nuramesh.server.headers import (
    DOCS_OPENAPI_URL,
    SecurityHeaders,
    header_items,
    secure_transport,
    swagger_ui_page,
)
from nuramesh.server.idempotency import (
    IdemContext,
    IdempotencyMiddleware,
    IdemService,
    document_idempotency,
    idem_of,
)
from nuramesh.server.invite_routes import install_invites
from nuramesh.server.invites import InviteService, InviteSettings
from nuramesh.server.key_routes import install_keys
from nuramesh.server.limits import RateLimiter, client_address
from nuramesh.server.mail import MailConfig, MailService
from nuramesh.server.mail_routes import install_mail
from nuramesh.server.mfa_notice import NoticeDesk
from nuramesh.server.mfa_routes import install_mfa
from nuramesh.server.mfa_service import MfaService
from nuramesh.server.model_routes import build_model_services, install_models
from nuramesh.server.openapi_text import ALREADY_200, scrub_schema
from nuramesh.server.operation_ids import check_operation_ids, operation_id_for
from nuramesh.server.pagination import (
    Cursor,
    Limit,
    decode_position,
    encode_cursor,
    invalid,
    paginate,
    position,
    refuse_old_parameters,
    respond,
    seq_position,
)
from nuramesh.server.password_reset import ResetService, ResetSettings
from nuramesh.server.problems import document_problems
from nuramesh.server.problems import problem_response as render_problem
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.reset_routes import install_password_reset
from nuramesh.server.routes import check_route_table
from nuramesh.server.savings_api import install_savings
from nuramesh.server.scope import (
    LOCAL_INSPECTOR,
    MAX_NEW_LOOKUPS,
    MAX_NEW_LOOKUPS_PER_TICK,
    LookupBudget,
    OrgScope,
    is_org,
)
from nuramesh.server.sessions import AuthProblem, AuthService, AuthSettings
from nuramesh.server.solo_routes import (
    install_password_confirm,
    solo_problem,
    solo_terms,
    terms_view,
)
from nuramesh.server.token_routes import install_tokens
from nuramesh.server.tokens import TokenService
from nuramesh.server.user_routes import install_users
from nuramesh.server.versions import CURRENT, HEADER, VersionHeader
from nuramesh.server.wait_warnings import WarningState, deliver_warnings
from nuramesh.server.webapp import WebApp, WebBundle
from nuramesh.stores import (
    AlreadyExistsError,
    ConcurrencyError,
    EventDraft,
    NotFoundError,
    Store,
    StoreBusyError,
    StoreError,
    UnitOfWork,
)

SECURITY_PREFIXES = ("auth.", "user.", "agent.credential_")
MAX_PAGE = 500
MAX_BODY = 1_000_000  # bytes; configs are small text
MAX_SEQ = 2**62
_ITEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
DEV_HUMAN_HINT = "dev_human must be an opaque id like human:u_7k2x9q (no email or name)"
EVENTS_FIRST_MESSAGE_SECONDS = 5.0
EVENTS_MAX_STREAMS = 100
EVENTS_POLL_SECONDS = 0.5  # default; p95 event latency stays under PLAN J.1's 1 s
MAX_RUNS = 200
MAX_LIVE_CONNECTIONS = 50  # concurrent WebSocket connections (live ledger views)
# Per client (IPv6 /64) per minute, for UNAUTHENTICATED callers; a signed-in caller (session,
# API token) is never throttled on health. See an internal decision record.
HEALTH_ANON_PER_MINUTE = 120  # GET /v1/health: a prober every 10 s is 6; 60 tabs polling is 120
HEALTH_PROBE_SECONDS = 5.0  # GET /v1/health reuses the Temporal reachability probe this long
# The three ledger verify routes recompute hash chains (and run the anchor check) on every call,
# so they are bounded for EVERY caller (TM-10, an internal decision record): one budget across the
# routes, per credential for a signed-in caller, per client address for an unauthenticated one.
VERIFY_ANON_PER_MINUTE = 60  # verify routes without a login (auth off, loopback only), per client
VERIFY_PER_MINUTE = 600  # per session, API token or agent credential (the T96 call cap)
MAX_VERIFY_IN_FLIGHT = 8  # verifications at once per process; the next is 429 busy, never a wait
MAX_VERIFY_STREAMS = 100  # streams one `GET /v1/ledger/verify` checks
MAX_BUDGET_HISTORY_EVENTS = 20_000  # org-stream entries one budget detail reads
CLOSE_CREDENTIAL_ENDED = 4401
"""WebSocket close code: the caller's credential stopped working mid-stream (expired, revoked,
logged out, erased). Not final: sign in or refresh, then reconnect. 1008 stays a real refusal."""
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
# SA-0.12 (spec/rbac/solo-approval.md): on these mutating routes a `Sec-Fetch-Site` header must be
# `same-origin` or `none` (typed address, bookmark), else 403 `foreign_origin`. Exact bytes: a
# browser sends lower case, and anything else is treated as foreign.
SEC_FETCH_SITE_ROUTES = frozenset({"/v1/auth/setup/redeem"})
SAME_SITE_VALUES = frozenset({b"same-origin", b"none"})
_AMOUNT = re.compile(r"^[0-9]{1,30}(\.[0-9]{1,18})?$")
_ACTOR = TypeAdapter(Actor)


class Problem(Exception):
    """An error response: HTTP status, a stable `code` and a message for people."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        self.status, self.code, self.detail, self.extra = status, code, detail, extra
        super().__init__(detail)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictBody(_Body):
    """No coercion: a string is never a number, a number never a bool (and 1.0 is not 1)."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PromptCacheBody(_StrictBody):
    """Turn provider-side prompt caching on or off for the organization (R1.34, ND320)."""

    enabled: bool


_ABSENT_DIGEST = ""
"""`action_digest` was not sent. Never a valid value: a present one is 64 hex characters."""


class _SoloFields(_StrictBody):
    """The two fields of a solo approve (spec/rbac/solo-approval.md SA-5, SA-6, SA-16). Their
    shape is validated for every caller; their values are read only for a solo approve. An
    absent `acknowledge_solo` is the same as `false`; a JSON null is refused for both."""

    acknowledge_solo: bool = False
    action_digest: str = Field(default=_ABSENT_DIGEST, pattern=r"^[0-9a-f]{64}$")

    @property
    def digest(self) -> str | None:
        """The digest sent, or None when the field was absent."""
        return None if self.action_digest == _ABSENT_DIGEST else self.action_digest


class ApprovalDecision(_SoloFields):
    """A human's answer to an approval request. The solo fields are read only when `approve` is
    true."""

    approve: bool
    reason: str = Field(min_length=1, max_length=1_000)


class ApprovalReason(_StrictBody):
    """The written reason of a reject; the verb is in the path."""

    reason: str = Field(min_length=1, max_length=1_000)


class ApprovalApprove(_SoloFields):
    """The written reason of an approve and, for a sole decide-holder, the acknowledgement and
    the digest of the action they were shown."""

    reason: str = Field(min_length=1, max_length=1_000)


class FindingBody(_StrictBody):
    """A person's verdict and written reason on one criterion."""

    approved: bool
    reason: str = Field(max_length=10_000)


class GateDecisionBody(_StrictBody):
    """A person's decision on a human gate of a durable run. `attempt` and `submission` name the
    round they looked at (what makes a retry safe). `acknowledge_solo` is read only for a
    decision that passes the gate, made by the organization's only person who can decide
    (spec/rbac/solo-approval.md SA-19); its shape is validated for every caller."""

    attempt: int = Field(ge=1, le=1_000_000)
    submission: int = Field(ge=1, le=1_000_000)
    findings: dict[str, FindingBody] = Field(max_length=100)
    acknowledge_solo: bool = False


class StopRequestBody(_StrictBody):
    """A human asks a run to stop. The reason is optional; the ledger keeps only its hash."""

    reason: str | None = Field(default=None, max_length=1_000)


_CONFLICTS = {
    "already_disabled": "the breaker is already disabled",
    "breaker_tripped": "the breaker is tripped; reset it first",
    "not_disabled": "the breaker is not disabled",
}


class EmptyBody(_Body):
    """No body, or `{}`: the org safety actions that take nothing."""

    model_config = ConfigDict(extra="forbid")


class DisableBody(_StrictBody):
    """Why a person switches a breaker off: one of four fixed codes, never free text."""

    reason: Literal["false_positive", "planned_work", "rule_being_replaced", "other"]


@dataclass(frozen=True)
class AnchorCheck:
    """Where the anchor repository and the public key are, so `verify` can also check the anchors
    (`nuramesh serve --anchors REPO --pubkey FILE`). Read on each check; nothing is cached."""

    repo: Path
    pubkey: Path
    ref: str = "ledger-anchors"
    extra_pubkeys: tuple[Path, ...] = ()
    tail_valid_until: str | None = None


class Unblock(_StrictBody):
    """A human lifts a budget block, optionally with a higher limit (ND23)."""

    reason: str = Field(min_length=1, max_length=1_000)
    new_limit: str | None = Field(default=None, max_length=40, description="a decimal amount")


_DECLARED_FIELDS = frozenset(
    name
    for model in (ApprovalDecision, FindingBody, GateDecisionBody, StopRequestBody, Unblock)
    for name in model.model_fields
)


def _where(loc: tuple[Any, ...]) -> str:
    """Where a validation error is, by position and declared names only: a key the client sent
    (an unknown field, a map key, an index) is never echoed (spec/auth/org-scope.md, R11)."""
    parts: list[str] = []
    for i, part in enumerate(loc):
        if i == 0 and part in ("body", "query", "path", "header", "cookie"):
            parts.append(str(part))
        elif isinstance(part, int):
            parts.append("<n>")
        elif i == 1 and loc[0] in ("query", "path", "header", "cookie"):
            parts.append(str(part))  # a parameter name the route declares
        elif part in _DECLARED_FIELDS:
            parts.append(str(part))
        else:
            parts.append("<key>")
    return ".".join(parts)


def _model_field_names() -> frozenset[str]:
    """Every field name a NuraMesh core model declares (what a configuration may spell)."""
    names: set[str] = set()
    todo: list[type[BaseModel]] = [BaseModel]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub.__module__.startswith("nuramesh.core"):
                names.update(sub.model_fields)
            todo.append(sub)
    return frozenset(names)


def _client_strings(value: Any, into: set[str] | None = None) -> set[str]:
    """Every string (key or value) in a JSON document the client sent, numbers as text."""
    found = set() if into is None else into
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(str(k))
            _client_strings(v, found)
    elif isinstance(value, list):
        for v in value:
            _client_strings(v, found)
    elif value is not None and not isinstance(value, bool):
        found.add(str(value))
    return found


def _config_issue(path: str, message: str, sent: set[str]) -> dict[str, str]:
    """A configuration problem for the API: where (declared names and indexes only; a key the
    client chose is `<key>`) and what, with every string the client sent taken out of the text,
    quoted or not (spec/auth/org-scope.md R11). Names the server declares stay."""
    if path == "$.integrations" and message.startswith(INTEGRATIONS_NOT_ACCEPTED):
        # fixed text: only built-in pack names, R2.04 and E4, never a value the client sent
        return {"path": path, "message": message}
    declared = _model_field_names() | set(TOP_LEVEL_KEYS)
    parts = []
    for part in path.removeprefix("$").split("."):
        if not part:
            continue
        name, bracket, index = part.partition("[")
        shown = name if name in declared else "<key>"
        parts.append(shown + (bracket + "<n>]" if index else ""))
    secret = sorted((t for t in sent - declared if t), key=len, reverse=True)
    for text in secret:
        message = re.sub(rf"(?<![\w]){re.escape(text)}(?![\w])", "<value>", message)
        message = message.replace(text, "<value>") if len(text) >= 8 else message
    return {"path": "$" + "".join("." + p for p in parts), "message": message}


def _parse_subscription(request: Any) -> tuple[set[str] | None, dict[str, int]] | None:
    """Validate the first message: streams (None = all) and the replay cursor, or None if bad."""
    if not isinstance(request, dict) or not request.keys() <= {"streams", "cursor"}:
        return None
    streams = request.get("streams")
    cursor_in = request.get("cursor", {})
    wanted: set[str] | None
    if streams is None or streams == ["*"]:
        wanted = None
    elif (
        isinstance(streams, list)
        and 0 < len(streams) <= EVENTS_MAX_STREAMS
        and all(isinstance(x, str) and 0 < len(x) <= 200 for x in streams)
    ):
        wanted = set(streams)
    else:
        return None
    if not isinstance(cursor_in, dict) or len(cursor_in) > EVENTS_MAX_STREAMS:
        return None
    cursor: dict[str, int] = {}
    for key, value in cursor_in.items():
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_SEQ
            or (wanted is not None and key not in wanted)
        ):
            return None
        cursor[key] = value
    return wanted, cursor


def _inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A model's JSON schema with its `$defs` written in place (no dangling `#/$defs` refs once
    the schema sits inside the OpenAPI document)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(defs[ref.removeprefix("#/$defs/")])
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)  # type: ignore[no-any-return]


class _Guard:
    """ASGI middleware: Host allow-list, Origin and Content-Type rules for mutations, body limits,
    and (for a server on a network address) TLS only.

    Pure ASGI (not `BaseHTTPMiddleware`) so it can count the bytes actually received: a
    `Content-Length` that lies, or chunked bodies, can't get past the limit.

    **Origin rule:** a browser's `Origin` must name an allowed host with the **same scheme and
    port** the request arrived on: the scheme is https when the request came over TLS (or, only
    with `trusted_proxy`, when the proxy's `X-Forwarded-Proto` says https), and a missing port is
    that scheme's default (443 or 80) on both sides.
    """

    def __init__(
        self,
        app: Any,
        allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
        *,
        trusted_proxy: TrustedProxies | bool = False,
        require_tls: bool = False,
        preflight: Callable[[Any], Awaitable[AuthProblem | None]] | None = None,
    ) -> None:
        self.app = app
        self.hosts = {h.strip("[]").lower() for h in allowed_hosts}
        self.trusted_proxy = trusted_proxy
        self.require_tls = require_tls
        self.preflight = preflight

    @staticmethod
    def _refuse(code: int, title: str, detail: str) -> JSONResponse:
        return render_problem(code, title, detail)

    def _origin_allowed(self, origin: str, port: str, secure: bool) -> bool:
        try:
            parts = urlsplit(origin)
            scheme = parts.scheme.lower()
            if scheme != ("https" if secure else "http"):
                return False
            default = 443 if secure else 80
            arrived = int(port) if port else default
            return (parts.hostname or "").lower() in self.hosts and (
                parts.port or default
            ) == arrived
        except ValueError:  # a malformed Origin (or port) is a foreign one
            return False

    def _websocket_allowed(self, scope: Any) -> bool:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        name, port = _split_host(headers.get("host", ""))
        if name.lower() not in self.hosts:
            return False
        secure = secure_transport(scope, self.trusted_proxy)
        if self.require_tls and not secure:
            return False
        origin = headers.get("origin")
        if origin is None:
            return True  # a non-browser client; browsers always send one
        return self._origin_allowed(origin, port, secure)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            # browsers send an Origin on WebSocket handshakes and WebSockets ignore CORS: without
            # this a page on any site could read the live ledger (cross-site WebSocket hijacking)
            if not self._websocket_allowed(scope):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        name, port = _split_host(host)
        if name.lower() not in self.hosts:
            await self._refuse(400, "invalid_host", "this server doesn't answer for that host")(
                scope, receive, send
            )
            return
        secure = secure_transport(scope, self.trusted_proxy)
        if self.require_tls and not secure and scope["path"] != "/v1/health":
            await self._refuse(
                400, "tls_required", "this server answers over https only (through its proxy)"
            )(scope, receive, send)
            return
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            origin = headers.get("origin")
            foreign = origin is not None and not self._origin_allowed(origin, port, secure)
            if not foreign and scope["path"] in SEC_FETCH_SITE_ROUTES:
                # SA-0.12: the browser's own statement of where the request comes from, checked
                # at the Origin step (before the content type, the body and the token). Every
                # header line must be one of the two values; a client that sends none (the CLI,
                # curl) is admitted, the token is the secret.
                sites = [v for k, v in scope["headers"] if k.lower() == b"sec-fetch-site"]
                foreign = any(v not in SAME_SITE_VALUES for v in sites)
            if foreign:
                await self._refuse(403, "foreign_origin", "cross-site requests are refused")(
                    scope, receive, send
                )
                return
            if self.preflight is not None:
                # a route that must know who asks before it reads anything (authz.AUTH_FIRST):
                # 401, 403 and the like come before 415, 411, 413 and 422
                refusal = await self.preflight(scope)
                if refusal is not None:
                    await problem_response(refusal)(scope, receive, send)
                    return
            media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            bodiless = headers.get("content-length") == "0" and "transfer-encoding" not in headers
            if media_type != "application/json" and not bodiless:  # F20: no body, no media check
                await self._refuse(415, "unsupported_media_type", "send application/json")(
                    scope, receive, send
                )
                return
            length = headers.get("content-length")
            if "transfer-encoding" in headers or length is None or not length.isdigit():
                await self._refuse(
                    411, "length_required", "send a plain Content-Length (no chunked bodies)"
                )(scope, receive, send)
                return
            if int(length) > MAX_BODY:
                await self._refuse(413, "too_large", f"bodies are limited to {MAX_BODY} bytes")(
                    scope, receive, send
                )
                return
            received = 0

            async def counted() -> Any:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > MAX_BODY:
                        raise _TooLarge
                return message

            try:
                await self.app(scope, counted, send)
            except _TooLarge:
                await self._refuse(413, "too_large", f"bodies are limited to {MAX_BODY} bytes")(
                    scope, receive, send
                )
            return
        await self.app(scope, receive, send)


def _split_host(host: str) -> tuple[str, str]:
    """(name, port) of a Host header value; IPv6 literals are bracketed (`[::1]:8765`)."""
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return "!" + host, ""  # unterminated: never a valid name
        rest = host[end + 1 :]
        return host[1:end], rest[1:] if rest.startswith(":") else ""
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name, port
    if ":" in host:
        return "!" + host, ""  # an unbracketed IPv6 literal isn't a valid Host
    return host, ""


def _loopback_host(host: str) -> bool:
    """`localhost` or a loopback IP literal (what an unauthenticated server may answer for)."""
    import ipaddress

    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class _TooLarge(Exception):
    pass


DECISION_TICK_SECONDS = 15.0
WAIT_WARNING_SECONDS = 300.0
"""How often the expiry warning mail pass runs (R1.20 PR D)."""
DECISION_SWEEP_SECONDS = 300.0
DISPATCH_PASS_SECONDS = 30.0
"""How often the server re-calls `starter.start` for start requests the first call missed
(spec 4.6)."""
DISPATCH_MIN_AGE_SECONDS = 60.0
"""A `pending` request younger than this is left to the route's own call."""
CONTENT_EXPIRY_SECONDS = 3600.0
KEY_SWEEP_SECONDS = 60.0


TEMPORAL_ENGINE = "temporal"


def _start_request_of(row: Any) -> StartRequest:
    return StartRequest(
        org_id=row.org_id,
        run_id=row.run_id,
        plan_hash=row.plan_hash,
        plan=parse_plan(row.plan),
        data_class=row.data_class,
        started_by=row.started_by,
        idempotency_key_hash=row.idempotency_key_hash,
        engine=row.engine,
    )


def start_stored_request(store: Store, engine: RunStarter, org_id: str, run_id: str) -> bool:
    """Spec 4.5 step 11: hand the stored request to the engine's starter. The row is already
    durable, so `start` is idempotent on it and, for Temporal, starts the workflow by id. Never
    raises: the answer stays 202 whatever happens here, a failure leaves the row `pending` and
    the dispatch pass starts it later (the row, not the queue, is the source of truth)."""
    try:
        with store.transaction() as uow:
            row = uow.get_start_request(org_id, run_id)
        if row is None:
            return False
        engine.start(_start_request_of(row))
        return True
    except Exception as exc:  # the request is recorded; never turn a 202 into an error
        logging.getLogger("nuramesh.server").warning(
            "run start dispatch failed for %s (%s); the request stays pending",
            run_id,
            type(exc).__name__,
        )
        return False


def dispatch_pending_starts(
    store: Store, engine: RunStarter, min_age_seconds: float = DISPATCH_MIN_AGE_SECONDS
) -> int:
    """Spec 4.6: re-call `engine.start` for this engine's `pending` requests older than
    `min_age_seconds` (a duplicate is `already_requested`: the workflow id is the run id).
    Returns how many were handed over without error. Only for Temporal: the light runner scans
    its own `pending` rows (4.6), so another engine gets 0."""
    if engine.name != TEMPORAL_ENGINE:
        return 0
    with store.transaction() as uow:
        now = uow.now()
        due = [
            (r.org_id, r.run_id)
            for r in uow.start_requests("pending")
            if r.engine == engine.name and (now - r.created_at).total_seconds() >= min_age_seconds
        ]
    return sum(1 for org_id, run_id in due if start_stored_request(store, engine, org_id, run_id))


def create_app(
    store: Store,
    config: NuraMeshConfig | None = None,
    *,
    dev_human: str | None = None,
    invite_settings: InviteSettings | None = None,
    email_verify_settings: EmailVerifySettings | None = None,
    reset_settings: ResetSettings | None = None,
    allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
    content: ContentService | None = None,
    anchors: AnchorCheck | None = None,
    events_poll_seconds: float = EVENTS_POLL_SECONDS,
    max_live_connections: int = MAX_LIVE_CONNECTIONS,
    auth: AuthSettings | None = None,
    on_human_decision: Callable[[str, str], None] | None = None,
    require_auth: bool = False,
    require_tls: bool = False,
    entitlements: Entitlements | None = None,
    halted: Callable[[], bool] | None = None,
    model_token_provider: TokenProvider | None = None,
    model_probe_transport: ProbeTransport | None = None,
    model_probe_clock: Callable[[], float] | None = None,
    breaker_tick: bool = True,
    plugins: PluginContext | None = None,
    plugin_settings: Mapping[str, Any] | None = None,
    decision_tick_seconds: float = DECISION_TICK_SECONDS,
    wait_warning_seconds: float = WAIT_WARNING_SECONDS,
    decision_sweep_seconds: float = DECISION_SWEEP_SECONDS,
    dispatch_pass_seconds: float = DISPATCH_PASS_SECONDS,
    otel_env: Mapping[str, str] | None = None,
    agent_team: str = "default",
    agent_settings: AgentSettings | None = None,
    content_expiry_seconds: float = CONTENT_EXPIRY_SECONDS,
    allow_private_network: bool = False,
    mail_config: MailConfig | None = None,
    mail_sender: MailSender | None = None,
    starter: RunStarter | None = None,
    run_start: RunStartSettings | None = None,
    secret_store: SecretStore | None = None,
    key_sweep_seconds: float = KEY_SWEEP_SECONDS,
    key_sweep_lock: SweepLock | None = None,
    web: WebBundle | None = None,
) -> FastAPI:
    """Build the API over `store` (and the org `config` the reports and eligibility need).

    `entitlements` is the entitlement source a plugin supplies (default: grants nothing).
    `content` is the content store service: approval and unblock reasons are written to it (the
    ledger keeps only their keyed hash, T47). Without it those two actions answer 503.

    `on_human_decision(run_id, task_id)` wakes a durable run's workflow after a decision on a human
    gate is recorded. The database is the truth: a failing or missing waker never undoes the
    decision (the workflow sees it at its next timer or step).

    `require_auth` enforces sessions and API tokens on every route (`authz.ROUTE_RULES`); it needs
    an organization and excludes `dev_human`. `require_tls` refuses every request (but the health
    check) that did not arrive over https or, with `auth.trusted_proxy`, through a TLS proxy.

    `web` is the packaged web app (`webapp.load_web_bundle`; #876, ND394): its page and listed
    files are answered at `/` and the app's known routes, public, with the page's own policy
    header. `None` (the default) serves nothing outside the routes below.

    `plugins` is what the installed plugins registered (ND72): their routes are added, with the
    rules they declared, and their refusal notes are called; `plugin_settings` holds their server
    settings by namespace. Without `plugins` the app is the open one.

    `breaker_tick` starts the circuit breaker ticker (`engine/breaker_tick.py`) in the app's
    lifespan when the configuration has breaker rules; tests and the read-only inspector pass
    False (the status then says `stopped`).

    `otel_env` is the environment the OpenTelemetry exporter reads (E3): `None` (the default, and
    what every test passes) means the exporter does not exist; `nuramesh serve` passes
    `os.environ`. Even then nothing starts unless `NURAMESH_OTEL_ENDPOINT` is set, and a refused
    setting only logs `otel export disabled: <code>`; the server still starts.

    `content_expiry_seconds` is how often the server's ticker runs content expiry (default one
    hour, at start and then on every tick; 0 turns it off): retention does not depend on a CLI run
    or a cron job. `allow_private_network` is `NURAMESH_ALLOW_PRIVATE_NETWORK` as `serve` read it:
    when true, one `network.private_allowed` entry is written to the organization's stream at
    start (the setting widens what outbound requests may reach, so it is on the record).

    `agent_team` is the team key (`core.auth.team_key` of the team folder's name) that agent
    credentials are issued under and looked up by; `agent_settings` the three
    `NURAMESH_AGENT_CREDENTIAL_*` settings (E2, spec 2.6 and 14). The start sweep of spec 2.6 is
    the caller's job (`app.state.agent_credentials.revoke_absent_agents()`, before the port
    opens).

    `mail_config` and `mail_sender` are the outgoing email settings and sender (R1.39); without
    them email is off and `GET /v1/auth/email/status` says so.

    `starter` is the engine's `RunStarter` (R1.20): `POST /v1/runs` stores a start request and the
    engine's runner takes it; without one the route answers 503 `engine_unavailable`.
    `run_start` holds the route's rate, cap, request TTL and the customer-data switch.

    `secret_store` is the stored-model-key store (R1.14c; `nuramesh serve` builds it from
    `NURAMESH_SECRET_DIR`). With it, the org-erasure hook is attached to `content` (a shred then
    destroys the org's stored keys) and the key sweeper runs in the lifespan, at start and then
    every `key_sweep_seconds` (default 60; 0 turns the periodic run off, the hook stays), under
    `key_sweep_lock` (default: in-process). Without it both are off."""
    if starter is not None and starter.name == "light":
        from nuramesh.engine.run_engine import refuse_light_when_hosted

        refuse_light_when_hosted()  # S12: a hosted API never takes the light starter
    if max_live_connections < 1:
        raise ValueError("max_live_connections must be at least 1")
    if not 0.01 <= events_poll_seconds <= 10:
        raise ValueError("events_poll_seconds must be between 0.01 and 10")
    if dev_human is not None:
        try:
            _ACTOR.validate_python(dev_human)
        except ValidationError:
            raise ValueError(
                "dev_human must be an opaque id like human:u_7k2x9q (no email or name)"
            ) from None
        if not is_human_id(dev_human):
            raise ValueError(
                "dev_human must be an opaque id like human:u_7k2x9q (no email or name)"
            )
        if require_auth:
            raise ValueError("dev_human is a harness for authentication off; not with require_auth")
    if require_auth and config is None:
        raise ValueError("authentication needs an organization (a team configuration)")
    if not require_auth and not all(_loopback_host(h) for h in allowed_hosts):
        raise ValueError("without require_auth the API answers loopback hosts only")
    humans = dev_human is not None or require_auth
    # The interactive docs page loads Swagger UI from a CDN (pinned version, sha384 integrity,
    # exact-URL CSP: headers.swagger_ui_page). It is served only where no human can be signed in
    # (no organization, no dev_human, no auth).
    docs_enabled = config is None and not humans
    breaker_rules = () if config is None else tuple(config.breakers.rules)
    ticker: BreakerTicker | None = None
    if config is not None and breaker_rules and breaker_tick:
        ticker = BreakerTicker(
            store,
            config.org.id,
            breaker_rules,
            tick_seconds=config.breakers.tick_seconds,
            halt_after=config.breakers.halt_after_failed_ticks,
            org=config.org,
        )

    wake_fold = (
        WakeFold(store, config.org.id)
        if config is not None and on_human_decision is not None
        else None
    )
    sweep_state = SweepState()

    def wake_with_limit(run_id: str, task_id: str) -> None:
        """The waker on its own small pool with a 3 s limit (a `TimeoutError` means the signal may
        have gone out). A slow Temporal cannot hold up the threads that authorize requests."""
        assert waker_pool is not None and on_human_decision is not None
        future = waker_pool.submit(on_human_decision, run_id, task_id)
        try:
            future.result(timeout=WAKE_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            if future.cancel():  # never started (a saturated pool): nothing was sent
                raise RuntimeError("the wake never started (the waker pool is busy)") from None
            raise TimeoutError("the wake timed out") from None

    def delivery_pass(trigger: Any = "tick") -> None:
        if config is not None and wake_fold is not None:
            deliver_held(
                store,
                config.org.id,
                wake_with_limit,
                content=content,
                trigger=trigger,
                fold=wake_fold,
            )

    def decision_sweep() -> None:
        if config is not None and wake_fold is not None:
            reconcile_decisions(
                store,
                config.org.id,
                wake_with_limit,
                content=content,
                fold=wake_fold,
                state=sweep_state,
            )

    def dispatch_pass() -> None:
        """One dispatch pass for a Temporal starter; a failure is logged, the next tick retries."""
        if starter is None or starter.name != TEMPORAL_ENGINE:
            return
        try:
            dispatch_pending_starts(store, starter)
        except Exception:  # noqa: BLE001 - the ticker must outlive one bad pass
            logging.getLogger("nuramesh.server").error("dispatch pass failed", exc_info=True)

    warning_state = WarningState()

    def wait_warning_pass() -> None:
        """Mail the approvers of a waiting task about its expiry (R1.20 PR D); never raises."""
        if config is not None:
            deliver_warnings(store, config.org.id, app.state.mail, warning_state, content=content)

    def content_expiry() -> None:
        """One expiry pass; a failure is logged and the next tick tries again."""
        assert content is not None
        try:
            content.expire()
        except Exception:  # noqa: BLE001 - the ticker must outlive one bad pass
            logging.getLogger("nuramesh.server").error("content expiry failed", exc_info=True)

    key_sweeper: KeySweeper | None = None
    if secret_store is not None:
        key_sweeper = KeySweeper(store, secret_store, lock=key_sweep_lock)
        if content is not None:
            key_sweeper.erasure.attach(content)  # a shred then destroys the org's stored keys

    def key_sweep() -> None:
        """One sweep pass; a failure is logged with a fixed code and the next tick tries again."""
        assert key_sweeper is not None
        try:
            key_sweeper.run_once()
        except Exception:  # noqa: BLE001 - the ticker must outlive one bad pass
            logging.getLogger("nuramesh.server").warning("key_sweep_failed")  # no text, no trace

    async def periodic(seconds: float, job: Callable[[], None], *, first: bool) -> None:
        if not first:
            await anyio.sleep(seconds)
        while True:
            await anyio.to_thread.run_sync(job, abandon_on_cancel=True)
            await anyio.sleep(seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        """Start the idempotency key sweep, the breaker ticker (the first tick does not block
        startup), the held-decision delivery tick (15 s) and the reconcile sweep (at start, then
        every 5 minutes); on shutdown cancel their sleeps and wait up to 5 seconds for a breaker
        tick in progress (spec 9.4). The telemetry exporter (E3) starts on its own thread when
        asked and is stopped, flushed, on shutdown."""
        # E13: first the configuration is recorded (with the voids it causes) before the server
        # uses it; a failure propagates and the server does not start (spec/autonomy 2 and 7)
        if config is not None:
            await anyio.to_thread.run_sync(
                lambda: record_config(
                    store, config.org, config.effective_policies, config.evals, content=content
                )
            )
        if config is not None and allow_private_network:
            await anyio.to_thread.run_sync(net_guard.record_allowance, store, config.org.id)
        exporter = None
        if otel_env is not None:
            from nuramesh.telemetry.runtime import start_exporter

            exporter = start_exporter(store, otel_env).tailer
            _app.state.otel = exporter
        try:
            wakes = wake_fold is not None and waker_pool is not None
            async with anyio.create_task_group() as tg:
                tg.start_soon(idem_service.sweep_forever)  # expired keys go without any login
                if ticker is not None:
                    tg.start_soon(ticker.run)
                if content is not None and content_expiry_seconds > 0:
                    tg.start_soon(
                        partial(periodic, content_expiry_seconds, content_expiry, first=True)
                    )
                if (
                    starter is not None
                    and starter.name == TEMPORAL_ENGINE
                    and dispatch_pass_seconds > 0
                ):
                    tg.start_soon(
                        partial(periodic, dispatch_pass_seconds, dispatch_pass, first=True)
                    )
                if key_sweeper is not None and key_sweep_seconds > 0:
                    tg.start_soon(partial(periodic, key_sweep_seconds, key_sweep, first=True))
                if wakes and decision_tick_seconds > 0:
                    tg.start_soon(
                        partial(periodic, decision_tick_seconds, delivery_pass, first=False)
                    )
                if config is not None and wait_warning_seconds > 0:
                    tg.start_soon(
                        partial(periodic, wait_warning_seconds, wait_warning_pass, first=False)
                    )
                if wakes and decision_sweep_seconds > 0:
                    tg.start_soon(
                        partial(periodic, decision_sweep_seconds, decision_sweep, first=True)
                    )
                try:
                    yield
                finally:
                    tg.cancel_scope.cancel()
                    if ticker is not None:
                        with anyio.CancelScope(shield=True):
                            await anyio.to_thread.run_sync(ticker.wait_idle, SHUTDOWN_WAIT_SECONDS)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(app.state.mail.close)  # queued mails go first
            if exporter is not None:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(exporter.stop)

    check_operation_ids()
    app = FastAPI(
        lifespan=lifespan,
        title="NuraMesh API",
        version="1",
        # every schema route takes its operation id (and so its SDK method name) from the one
        # table in `operation_ids.py`; a route without a row cannot be served
        generate_unique_id_function=operation_id_for,
        docs_url=None,  # the pinned page with integrity attributes is added below
        redoc_url=None,
        redirect_slashes=False,  # a trailing slash is a 404, never a 307 ahead of the auth check
        swagger_ui_oauth2_redirect_url=None,  # no OAuth2 in the docs page: one route fewer
        openapi_url=DOCS_OPENAPI_URL,
    )
    if docs_enabled:
        app.add_route(
            "/v1/docs",
            lambda _request: swagger_ui_page(DOCS_OPENAPI_URL),
            include_in_schema=False,
        )
    # DNS rebinding: a web page can make its own domain point at 127.0.0.1 and then act as the
    # browser's same origin. The guard serves only requests that name a loopback Host, refuses
    # mutations from a foreign or different-port Origin, and bounds request bodies.
    auth_settings = auth or AuthSettings()
    auth_service = (
        None if config is None else AuthService(store, config.org.id, auth_settings, content)
    )
    token_service = (
        None
        if config is None
        else TokenService(
            store, config.org.id, auth_settings.clock or store.now, auth_settings.monotonic
        )
    )
    agent_cfg = agent_settings or AgentSettings()
    agent_service = (
        None
        if config is None
        else AgentCredentialService(
            store,
            config.org.id,
            agent_team,
            agent_directory(config.org),
            auth_settings.clock or store.now,
            default_days=agent_cfg.days,
            max_live=agent_cfg.max_live,
        )
    )
    agent_auth = (
        None
        if agent_service is None
        else AgentAuth(agent_service, agent_cfg.calls_per_minute, auth_settings.monotonic)
    )
    app.state.auth = auth_service
    app.state.agent_credentials = agent_service
    app.state.breaker_ticker = ticker
    app.state.key_sweeper = key_sweeper
    app.state.otel = None
    app.state.tokens = token_service
    # read-only views: a route installer holds `app`, and must not add to its own declaration
    # before the route checks (and the Authorizer) read it
    extra_rules: Mapping[tuple[str, str], Permission] = MappingProxyType(
        {
            key: Permission(rule.permission)
            for key, rule in (plugins.route_rules if plugins else {}).items()
        }
    )
    extra_scopes: Mapping[tuple[str, str], str] = MappingProxyType(
        {key: rule.scope for key, rule in (plugins.route_rules if plugins else {}).items()}
    )
    app.state.plugin_route_rules = extra_rules
    app.state.plugin_route_scopes = extra_scopes
    # innermost: who is calling and may they, for the route that matched (authz.ROUTE_RULES)
    authz_settings: dict[str, Any] = {
        "routes": lambda: app.router.routes,
        "required": require_auth,
        "sessions": auth_service,
        "tokens": token_service,
        "agents": agent_auth,
        "trusted_proxy": auth_settings.trusted_proxy,
        "dev_human": dev_human,
        "local_org": None if config is None else config.org.id,
        "extra_rules": extra_rules,
        "denial_hooks": {
            key: functools.partial(hook, store)
            for key, hook in (plugins.denial_hooks if plugins is not None else {}).items()
        },
    }
    idem_service = IdemService(store, content)
    app.state.idem = idem_service

    def _idem_dep(request: Request) -> IdemContext:
        return idem_of(request, idem_service)

    Idem = Annotated[IdemContext, Depends(_idem_dep)]
    app.add_middleware(
        IdempotencyMiddleware, routes=lambda: app.router.routes, service=idem_service
    )
    app.add_middleware(Authorizer, **authz_settings)
    if web is not None:
        # #876, ND394: the page and its listed files, public, GET and HEAD only, never `/v1`.
        # Inside the guard (Host allow-list, TLS rule), outside the authorizer and the router.
        app.add_middleware(WebApp, bundle=web)
    # the guard asks the same question early for the routes in `authz.AUTH_FIRST`
    early = Authorizer(None, **authz_settings)
    app.add_middleware(
        _Guard,
        allowed_hosts=allowed_hosts,
        trusted_proxy=auth_settings.trusted_proxy,
        require_tls=require_tls,
        preflight=early.preflight,
    )
    app.add_middleware(VersionHeader)  # before authentication; its 400 carries the headers below
    # outermost, so the headers also reach the guard's refusals (R1.22 PR 2, BASELINE A3)
    app.add_middleware(DeprecationHeaders, routes=lambda: app.router.routes)
    app.add_middleware(
        SecurityHeaders, trusted_proxy=auth_settings.trusted_proxy, docs=docs_enabled, web=web
    )

    def _render(exc: "Problem") -> JSONResponse:
        return render_problem(exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/v1/auth/"):
            # passwords and tokens: say nothing about which field or value was wrong
            return _render(Problem(422, "invalid_request", "the request is invalid"))
        problems = [
            {
                "where": _where(e["loc"]),
                "path": "$." + _where(e["loc"]).removeprefix("body."),
                "message": "unknown field" if e["type"] == "extra_forbidden" else e["msg"],
            }
            for e in exc.errors()
        ]
        if any(e["type"] == "key_ref_server_only" for e in exc.errors()):
            # R1.14c section 9: only the key route writes `key_ref`
            return _render(
                Problem(422, "key_ref_server_only", "the request is invalid", problems=problems)
            )
        return _render(Problem(422, "invalid_request", "the request is invalid", problems=problems))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 400 and exc.detail == "There was an error parsing the body":
            # FastAPI's catch-all for a body it could not parse (a RecursionError from absurd
            # nesting): a client fault like any other bad JSON, never quoted back (#1121)
            return _render(Problem(422, "invalid_request", "the request is invalid"))
        titles = {404: "not_found", 405: "method_not_allowed"}
        return _render(
            Problem(
                exc.status_code,
                titles.get(exc.status_code, "http_error"),
                "no such resource or method",
            )
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # answered by the outermost error middleware, outside SecurityHeaders: add them here
        response = _render(Problem(500, "internal_error", "something went wrong"))
        response.headers.update(
            header_items(request.scope, auth_settings.trusted_proxy, docs=docs_enabled)
        )
        if request.url.path.startswith("/v1/"):
            response.headers[HEADER.decode()] = CURRENT  # VersionHeader is inside this handler
        return response

    @app.exception_handler(AuthProblem)
    async def _auth_problem(_: Request, exc: AuthProblem) -> JSONResponse:
        return problem_response(exc)

    @app.exception_handler(EntitlementRequired)
    async def _entitlement(_: Request, exc: EntitlementRequired) -> JSONResponse:
        return _render(Problem(403, exc.code, "this feature is not enabled", key=exc.key))

    @app.exception_handler(Problem)
    async def _problem(_: Request, exc: Problem) -> JSONResponse:
        return _render(exc)

    @app.exception_handler(StoreBusyError)
    async def _busy(_: Request, exc: StoreBusyError) -> JSONResponse:
        return await _problem(_, Problem(503, "store_busy", "the database is busy; retry"))

    @app.exception_handler(ConcurrencyError)
    async def _conflict(_: Request, exc: ConcurrencyError) -> JSONResponse:
        return await _problem(_, Problem(409, "conflict", "the data changed meanwhile; retry"))

    @app.exception_handler(AlreadyExistsError)
    async def _exists(_: Request, exc: AlreadyExistsError) -> JSONResponse:
        # no retry advice (a retry cannot succeed) and nothing echoed back, not even the email
        return await _problem(_, Problem(409, "already_exists", "that account already exists"))

    @app.exception_handler(NotFoundError)
    async def _missing(_: Request, exc: NotFoundError) -> JSONResponse:
        return await _problem(_, Problem(404, "not_found", "no such record"))

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError) -> JSONResponse:
        return await _problem(_, Problem(500, "store_error", "the store failed"))

    def need_config() -> NuraMeshConfig:
        if config is None:
            raise Problem(409, "no_config", "this server was started without an organization")
        return config

    def caller(request: Request) -> Caller:
        found = getattr(request.state, "caller", None)
        if not isinstance(found, Caller):  # the authorizer always sets one; never guess
            raise Problem(403, "no_rule", "this route has no authorization rule; refused")
        return found

    def actor(who: Annotated[Caller, Depends(caller)]) -> str:
        """The actor of an action: the session's person, the token's `service:<id>`, or the
        dev harness person. Never read from the body or a header."""
        if who.actor is None:
            raise Problem(403, "authentication_required", "this needs a signed-in person")
        return who.actor

    def human_caller(who: Annotated[Caller, Depends(caller)]) -> Caller:
        """`human`, for a route that also needs the session the person came in on."""
        if not who.human or who.actor is None:
            raise Problem(403, "human_required", "this needs a person signed in")
        return who

    def human(who: Annotated[Caller, Depends(caller)]) -> str:
        """A person only (approvals, unblocks, reading a reason): checked here again although the
        route's rule already excludes tokens, so a wrong table row can't let a program through."""
        if not who.human or who.actor is None:
            raise Problem(403, "human_required", "this needs a person signed in")
        return who.actor

    # commercial handlers read this; the open routes never do (ND72). Default grants nothing.
    app.state.entitlements = entitlements if entitlements is not None else NoEntitlements()
    install_auth(app, auth_service, required=require_auth)
    mail = MailService(
        mail_config
        or MailConfig(None, ("NURAMESH_SMTP_HOST", "NURAMESH_SMTP_FROM", "NURAMESH_PUBLIC_URL")),
        mail_sender,
        store,
        None if config is None else config.org.id,
        auth_settings.clock or store.now,
    )
    app.state.mail = mail
    install_mail(app, mail, caller)
    install_tokens(app, token_service, caller, _idem_dep)
    install_users(app, auth_service, caller, _idem_dep, agent_service)
    install_password_confirm(app, auth_service, caller, trusted_proxy=auth_settings.trusted_proxy)
    install_mfa(
        app,
        None
        if auth_service is None
        else MfaService(
            auth_service,
            NoticeDesk(mail, store, auth_service.org_id, auth_settings.clock or store.now),
        ),
        caller,
        _idem_dep,
        trusted_proxy=auth_settings.trusted_proxy,
    )
    invite_service = (
        None if auth_service is None else InviteService(auth_service, mail, invite_settings)
    )
    install_invites(
        app, invite_service, caller, _idem_dep, trusted_proxy=auth_settings.trusted_proxy
    )
    install_email_change(
        app,
        None
        if auth_service is None
        else EmailChangeService(auth_service, mail, email_verify_settings),
        caller,
        trusted_proxy=auth_settings.trusted_proxy,
    )
    reset_service = (
        None if auth_service is None else ResetService(auth_service, mail, reset_settings)
    )
    install_password_reset(app, reset_service, caller, trusted_proxy=auth_settings.trusted_proxy)
    install_agent_credentials(app, agent_service, caller, _idem_dep)
    hashing = None if content is None else content.ledger_hashing
    key_readers = (
        None
        if secret_store is None or hashing is None
        else KeyReaders.from_env(
            store,
            secret_store,
            lambda org: lambda uow: hashing.bind(uow, org),
            os.environ,
        )
    )
    model_services = (
        None
        if config is None
        else build_model_services(
            store,
            config,
            content=content,
            halted=halted,
            token_provider=model_token_provider,
            probe_transport=model_probe_transport,
            probe_clock=model_probe_clock,
            key_readers=key_readers,
        )
    )
    install_models(app, config, model_services, caller, store.now, _idem_dep)
    install_keys(
        app,
        config,
        model_services,
        secret_store,
        caller,
        store,
        store.now,
        mfa_available=(lambda: False) if auth_service is None else auth_service.mfa_available,
        injected_store=isinstance(secret_store, FakeSecretStore),
    )

    install_autonomy(app, config, store, content, caller, store.now, _idem_dep)

    Config = Annotated[NuraMeshConfig, Depends(need_config)]
    Human = Annotated[str, Depends(human)]
    HumanCaller = Annotated[Caller, Depends(human_caller)]
    Actor_ = Annotated[str, Depends(actor)]

    scope = OrgScope(store, orgs=() if config is None else (config.org.id,))

    def org_of(who: Annotated[Caller, Depends(caller)]) -> str | None:
        """The caller's organization (from the credential only; spec/auth/org-scope.md)."""
        return who.org_id

    Org = Annotated[str | None, Depends(org_of)]
    Who = Annotated[Caller, Depends(caller)]

    def no_run() -> Problem:
        # one answer for a run that doesn't exist and one that isn't the caller's; never the id
        return Problem(404, "not_found", "no such run")

    def shown_actor(who: Caller, actor_id: str) -> str:
        """A stop request's actor as this caller may see it: an agent sees only the kind of a
        person's or program's id (E2 spec 6.2.1)."""
        return views.agent_actor_view(actor_id) or "" if who.kind == "agent" else actor_id

    def need_run(org: str | None, run_id: str) -> None:
        """Refuse (the missing answer) unless the run exists and belongs to the caller's
        organization. The one check every run route makes; it reads at most the first entry."""
        if not _SLUG.fullmatch(run_id) or not scope.visible(org, f"run:{run_id}"):
            raise no_run()

    def shown_to(who: Caller | None, stream: str, event_type: str) -> bool:
        """Sign-in times, failed logins, account changes and who issued or revoked an agent
        credential sit in the organization's own stream (`org:<id>`); they are workforce
        monitoring, so only ADMIN reads them there (R1.30 review 1). Everyone with READ still
        sees the rest of the stream."""
        if not stream.startswith("org:") or not event_type.startswith(SECURITY_PREFIXES):
            return True
        return isinstance(who, Caller) and Permission.ADMIN in who.permissions

    def need_stream(org: str | None, stream: str) -> None:
        if not scope.visible(org, stream):
            raise Problem(404, "not_found", "no such stream")

    def need_approval(org: str | None, approval_id: str) -> Any:
        stored = store.get_approval(approval_id)
        if stored is None or not _SLUG.fullmatch(stored.approval.run_id):
            raise Problem(404, "not_found", "no such approval")
        if not scope.visible(org, f"run:{stored.approval.run_id}"):
            raise Problem(404, "not_found", "no such approval")
        return stored

    # -- service ---------------------------------------------------------------------------

    health_limit = RateLimiter(HEALTH_ANON_PER_MINUTE, 60.0, auth_settings.monotonic)
    verify_limit = RateLimiter(VERIFY_ANON_PER_MINUTE, 60.0, auth_settings.monotonic)
    verify_caller_limit = RateLimiter(VERIFY_PER_MINUTE, 60.0, auth_settings.monotonic)
    verify_slots = threading.BoundedSemaphore(MAX_VERIFY_IN_FLIGHT)

    def credential_key(caller: Caller) -> str:
        """The limiter key of a signed-in caller: its credential, never its address (programs
        behind one proxy do not share it; two tokens of one person are two keys)."""
        own = caller.session_id or caller.token_id or caller.credential_id or caller.actor
        return f"{caller.kind}:{own or ''}"

    def throttle(
        limiter: RateLimiter, request: Request, signed_in: RateLimiter | None = None
    ) -> None:
        """429 with `Retry-After` when the caller is over its limit: an unauthenticated client
        (its IPv6 /64) against `limiter`; a caller the authorizer recognised (session, API token,
        agent credential, dev harness) against `signed_in` per credential, or never counted when
        `signed_in` is None."""
        known = getattr(request.state, "caller", None)
        if isinstance(known, Caller) and known.kind != "anonymous":
            if signed_in is None:
                return
            wait = signed_in.allow(credential_key(known))
        else:
            key = address_key(client_address(request, trusted_proxy=auth_settings.trusted_proxy))
            wait = limiter.allow(key)
        if wait > 0:
            raise AuthProblem(
                429,
                "rate_limited",
                "too many requests; slow down",
                {"Retry-After": str(max(1, math.ceil(wait)))},
            )

    probe_cache: list[tuple[float, bool]] = []  # (monotonic time, reachable), at most one

    def temporal_reachable() -> bool:
        """Cached for HEALTH_PROBE_SECONDS: each probe opens a Temporal connection (<= 5 s)."""
        now = auth_settings.monotonic()
        if probe_cache and 0 <= now - probe_cache[0][0] < HEALTH_PROBE_SECONDS:
            return probe_cache[0][1]
        try:
            reachable = bool(starter.health().ok)  # type: ignore[union-attr]
        except Exception:
            reachable = False
        probe_cache[:] = [(now, reachable)]
        return reachable

    def engine_health() -> dict[str, Any]:
        """`engine` (the starter's name) and, for the light engine, `light_engine`: `absent` (no
        runner heartbeat), `standby` or `active`. Fixed words only."""
        if starter is None:
            return {}
        found: dict[str, Any] = {"engine": starter.name}
        if starter.name == "temporal":
            found["starter"] = {
                "reachable": temporal_reachable(),
                "namespace": str(getattr(starter, "namespace", "")),
                "task_queue": str(getattr(starter, "task_queue", "")),
            }
        if starter.name == "light":
            try:
                with store.transaction() as uow:
                    leader = uow.get_leader("light")
                    now = uow.now()
                found["light_engine"] = (
                    "active" if leader is not None and leader.lease_until > now else "absent"
                )
            except Exception:
                found["light_engine"] = "absent"
        return found

    @app.get("/v1/health")
    def health(request: Request) -> dict[str, Any]:
        throttle(health_limit, request)
        return {
            "status": "ok",
            "streams": len(store.streams()),
            "org": config.org.id if config else None,
            "humans_enabled": humans,
            "auth_required": require_auth,
            "mail_enabled": bool(mail.enabled),  # one boolean: no host, sender or setting
            **engine_health(),
            "warnings": (content.warnings() if content is not None else [])
            + (parked_warning(wake_fold, store.now()) if wake_fold is not None else []),
        }

    @app.get("/v1/schema/config")
    def schema() -> dict[str, Any]:
        return config_json_schema()

    @app.post("/v1/config/validate")
    def validate(body: dict[str, Any]) -> dict[str, Any]:
        sent = _client_strings(body)
        try:
            parsed = parse_config(body, shipped=load_shipped().prices)
        except ShippedPriceError:  # fails closed: nothing is guessed
            raise Problem(
                503, "shipped_prices_invalid", "the shipped price list is invalid"
            ) from None
        except ConfigValidationError as exc:
            raise Problem(
                422,
                "invalid_config",
                "the configuration has problems",
                issues=[_config_issue(i.path, i.message, sent) for i in exc.issues],
            ) from None
        if content is not None:  # an erased organization id is retired for good (R1.37, F01)
            try:
                retired = content.retired(parsed.org.id)
            except ContentError:  # fail closed: an unreadable tombstone is not "valid"
                raise _content_unavailable() from None
            if retired is not None:
                issue = _config_issue(
                    "org.id",
                    "this organization id was erased and can't be used again; use a new id",
                    sent,
                )
                raise Problem(
                    422,
                    "invalid_config",
                    "the configuration has problems",
                    issues=[{**issue, "code": "org_id_retired"}],
                )
        return {"valid": True}

    # -- runs and tasks ----------------------------------------------------------------------

    def run_events(org: str | None, run_id: str) -> list[Any]:
        need_run(org, run_id)
        events = views.read_all(store, f"run:{run_id}")
        if not events:
            raise no_run()
        return events

    @app.get("/v1/runs")
    def runs(
        request: Request,
        org: Org,
        who: Who,
        limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 50,
        status: str | None = None,
        cursor: Cursor = None,
    ) -> Response:
        """Runs, most recently active first (`limit` at most 200): `{"runs": [...], "truncated",
        "scanned"}`. Each row has the status, task and gate counts and the spend, derived from the
        run's ledger (docs/api.md). `status` is one of the documented values (else 422). The work
        per request is bounded; `truncated` says the bound stopped the search."""
        if status is not None and status not in views.RUN_STATUSES:
            raise Problem(
                422,
                "invalid_request",
                "status must be one of " + ", ".join(views.RUN_STATUSES),
            )
        route = "GET /v1/runs"
        after = None if cursor is None else decode_position(cursor, route)
        lookups = LookupBudget(MAX_NEW_LOOKUPS)  # first-time owner lookups of other organizations
        try:  # the cursor names the last run sent; the scan starts behind it, under this scope
            rows, truncated, scanned, more = views.scan_runs(
                store,
                limit=limit,
                status=status,
                visible=lambda stream: scope.visible_within(org, stream, lookups),
                after=after,
            )
        except views.NoSuchRunPosition:
            raise invalid() from None
        if who.kind == "agent":  # narrowed before paging: a page holds only what page one may
            rows = [views.agent_run_row(r) for r in rows]
        # a page the bound cut short can still be continued from its last run
        nxt = encode_cursor(route, rows[-1]["run_id"]) if (more or truncated) and rows else None
        return respond(
            request,
            items=rows,
            old_key="runs",
            next_cursor=nxt,
            extra={"truncated": truncated, "scanned": scanned},
            native=True,
        )

    @app.get("/v1/runs/{run_id}")
    def run_detail(run_id: str, org: Org, who: Who) -> dict[str, Any]:
        """One run: status, tasks with their gates and verdicts, attempts, spend, budgets. A run
        that is only a start request (a runner has not begun it yet, or refused it) shows the
        request: `state` `requested` or `refused` with a `refusal_code`."""
        requested = request_view(org, run_id) if who.kind != "agent" else None
        if requested is not None:
            return requested
        if who.kind == "agent" and who.agent_id is not None:
            return views.agent_run_detail(store, run_id, run_events(org, run_id), who.agent_id)
        return views.run_detail(
            store,
            run_id,
            run_events(org, run_id),
            config,
            unowned=org == LOCAL_INSPECTOR,
            show_warning=can_act_of(who),
        )

    def request_view(org: str | None, run_id: str) -> dict[str, Any] | None:
        """The caller's own start request for a run that has no ledger stream yet, else None."""
        if org is None or not is_org(org) or not _SLUG.fullmatch(run_id):
            return None
        if list(store.read_stream(f"run:{run_id}", after_seq=0, limit=1)):
            return None
        with store.transaction() as uow:
            row = uow.get_start_request(org, run_id)
        if row is None:
            return None
        refused = row.state == "refused"
        return {
            "run_id": run_id,
            "state": "refused" if refused else "requested",
            "refusal_code": row.refusal_code if refused else None,
            "engine": row.engine,
            "data_class": row.data_class,
            "requested_at": row.created_at.isoformat(),
        }

    @app.get("/v1/runs/{run_id}/ledger")
    def run_ledger(
        request: Request,
        run_id: str,
        org: Org,
        after_seq: Annotated[int | None, Query(ge=0, le=MAX_SEQ)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
        type: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]*$")] = None,
        task_id: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Cursor = None,
    ) -> Response:
        """The run's ledger stream, paged; `type` keeps entries whose type starts with it
        (for example `gate.` or `budget.`)."""
        need_run(org, run_id)
        route = "GET /v1/runs/{run_id}/ledger"
        refuse_old_parameters(cursor, after_seq=after_seq)
        start = after_seq or 0
        if cursor is not None:
            start = seq_position(decode_position(cursor, route, run_id))
        page = views.ledger_page(
            store,
            f"run:{run_id}",
            after_seq=start,
            limit=limit,
            type_prefix=type,
            task_id=task_id,
        )
        if cursor is not None and start > page["head_seq"]:
            raise invalid()
        return respond(
            request,
            items=page["events"],
            old_key="events",
            next_cursor=(
                encode_cursor(route, position(run_id, str(page["next_after_seq"])))
                if page["has_more"]
                else None
            ),
            extra={k: page[k] for k in ("stream", "head_seq", "head")},
            old_extra={k: page[k] for k in ("next_after_seq", "has_more")},
            native=True,
        )

    @app.get("/v1/runs/{run_id}/ledger/verify")
    def run_verify(request: Request, run_id: str, org: Org) -> dict[str, Any]:
        throttle(verify_limit, request, verify_caller_limit)  # before the lookup (TM-10)
        need_run(org, run_id)
        return verification([f"run:{run_id}"])

    @app.post("/v1/runs/{run_id}/stop")
    def stop_run(
        run_id: str, body: StopRequestBody, actor_id: Actor_, org: Org, who: Who, idem: Idem
    ) -> Response:
        """A person, a `run` token or an agent credential asks the run to stop
        (`run.stop_requested`, the actor from the credential only). Idempotent: asking again
        returns the first request and writes nothing. The engine ends the run cleanly at its next
        step. An agent may stop only a run it takes part in (it is the assignee of a task of the
        run); any other run of the organization is answered exactly as a run that does not exist,
        before the finished check (E2 spec 6.2.2), and the same is checked again inside the write
        transaction."""
        need_run(org, run_id)  # another organization's run is the missing answer, not a 409
        recheck = None
        if who.kind == "agent" and who.agent_id is not None and agent_service is not None:
            agent_id, credential_id, svc = who.agent_id, who.credential_id, agent_service
            if not takes_part(store.tasks_for_run(run_id), agent_id):
                raise no_run()

            def recheck(uow: UnitOfWork) -> None:
                found = (
                    None
                    if credential_id is None
                    else uow.get_agent_credential(svc.org_id, credential_id)
                )
                if found is None or not found.active(svc.now()) or svc.current(found) is None:
                    raise invalid_credential()
                if svc.suspended(uow, agent_id):
                    raise AuthProblem(
                        403,
                        "agent_suspended",
                        "the organization is halted or this agent's role is paused",
                    )
                if not takes_part(uow.tasks_for_run(run_id), agent_id):
                    raise no_run()

        reason = (body.reason or "").strip() or None
        # the run is visible to the caller, so its organization is the caller's (or, for the
        # local inspector of an app with no organization, the run's own): that is where a
        # reason is kept. The local inspector is a marker, never an organization id.
        first = list(store.read_stream(f"run:{run_id}", after_seq=0, limit=1))
        run_org = first[0].payload.get("org") if first and first[0].type == "run.started" else None
        org_id = org if is_org(org) else (run_org if is_org(run_org) else None)
        if reason is not None:
            _content()  # a reason needs the content store; refuse before anything is written
            if org_id is None:
                raise Problem(
                    409, "no_config", "the run's organization is unknown; a reason can't be kept"
                )
        state = stop_state(store, run_id)  # the cheap pre-read stays outside the transaction
        try:
            with idem.effect() as uow:
                outcome = request_stop_in(
                    uow,
                    run_id,
                    state,
                    actor=actor_id,
                    via="api",
                    reason=reason,
                    content=content,
                    org_id=org_id,
                    recheck=recheck,
                )
                req = outcome.request
                answer: dict[str, Any] = {
                    "run_id": run_id,
                    "status": "stopping",
                    "already_requested": outcome.already_requested,
                    "requested_by": shown_actor(who, req.by),
                    "requested_at": req.at.isoformat(),
                    "ledger_seq": req.seq,
                    "via": req.via,
                }
                if req.reason_withheld is not None:  # Stop always works; reason not kept (K6)
                    answer["reason_withheld"] = req.reason_withheld
                    answer["stop_reason_withheld"] = req.withheld_text
                # the answer names who asked first: erasing that person purges this row (M1)
                response = idem.respond(
                    uow, 200, answer, subject=req.by if req.by.startswith("human:") else None
                )
        except NotFoundError:
            raise no_run() from None
        except RunFinished:
            raise Problem(
                409, "run_finished", f"run '{run_id}' has already ended; there is nothing to stop"
            ) from None
        except ContentError as exc:
            raise _content_problem(exc, org_id) from None
        if content is not None:
            content.flush_alerts()  # the capture_failed row, now that the transaction has ended
        return response

    @app.get("/v1/runs/{run_id}/stop")
    def stop_status(run_id: str, org: Org, who: Who) -> dict[str, Any]:
        need_run(org, run_id)
        state = stop_state(store, run_id)
        if not state.exists:
            raise no_run()
        req = state.request
        return {
            "run_id": run_id,
            "requested": req is not None,
            "requested_by": shown_actor(who, req.by) if req else None,
            "requested_at": req.at.isoformat() if req else None,
            "ledger_seq": req.seq if req else None,
            "ended": state.ended,
            "stopped": state.ended_status == "stopped",
            **(
                {"reason_withheld": req.reason_withheld, "stop_reason_withheld": req.withheld_text}
                if req is not None and req.reason_withheld is not None
                else {}
            ),
        }

    start_settings = run_start or RunStartSettings()
    start_rate = RateLimiter(start_settings.rate_per_minute, 60.0, auth_settings.monotonic)

    def run_body_cap(request: Request) -> None:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > RUN_BODY_BYTES:
            raise Problem(413, "too_large", "the plan is over the 64 KiB limit")

    def start_refusal(org_id: str, run_id: str, actor_id: str, code: str) -> None:
        """The one `run_start_refused` entry of a synchronous refusal (steps 8 and 10)."""
        with store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=org_stream(org_id),
                    type=EventType.DECISION_RECORDED,
                    actor=actor_id,
                    payload={"kind": "run_start_refused", "run_id": run_id, "code": code},
                )
            )

    def active_for(org_id: str) -> int:
        with store.transaction() as uow:
            return uow.count_active_runs(org_id)

    @app.post("/v1/runs", status_code=202, dependencies=[Depends(run_body_cap)])
    def start_run_route(
        body: StartRunBody, actor_id: Actor_, org: Org, who: Who, idem: Idem
    ) -> Response:
        """A person starts a run (R1.20, ND347 section 4). Asynchronous: the plan's text goes to
        the content store (never the ledger), a start request is stored and the answer is 202; a
        runner checks the plan against the team's code and begins it, or refuses it visibly in
        `GET /v1/runs/{id}`. The run id is the server's: with an `Idempotency-Key` it is derived
        from (organization, actor, route, key), so a retry is the same run."""
        # step 5: the caller's own organization, never a body field (extra fields are 422)
        if config is None or org is None or not is_org(org) or org != config.org.id:
            raise Problem(409, "no_config", "this server has no organization to start a run in")
        org_id = org
        if body.data_class == "customer" and not start_settings.customer_data_enabled:
            raise Problem(
                422,
                "customer_data_not_enabled",
                "customer data is not enabled on this deployment",
            )
        # step 6: rate and cap before the organization state, so every later refusal is bounded
        wait = start_rate.allow(org_id)
        if wait > 0:
            raise AuthProblem(
                429,
                "run_rate_limited",
                "too many run starts; slow down",
                {"Retry-After": str(max(1, math.ceil(wait)))},
            )
        if active_for(org_id) >= start_settings.max_active:
            raise AuthProblem(
                429,
                "run_cap_reached",
                "too many unfinished runs and pending starts for this organization",
                {"Retry-After": "30"},
            )
        # step 7: organization state
        denial = deny_checks(store, org_id, content=content)
        if denial is not None:
            if denial.is_halt:
                record_halt_refusal(
                    store, org_id, "run-start", episode=denial.halt_seq, content=content
                )
                raise Problem(409, "org_halted", "the organization is halted")
            if denial.kind == "key_lost":
                if isinstance(denial.error, OrphanedContentError):
                    raise Problem(409, "org_key_missing", denial.text)
                raise Problem(409, "org_key_destroyed", denial.text)
            raise Problem(409, "org_erased", denial.text)
        service = _content()
        idem.require_alive()
        run_id = derive_run_id(org_id, actor_id, idem.key_hash)
        # step 8: shape, no team code
        try:
            validate_shape(body.tasks)
            with store.transaction() as uow:
                for item in body.tasks:
                    if item.template is not None:
                        if not service.has_kind(
                            uow, org_id, item.template, ContentKind.TASK_TEMPLATE
                        ):
                            raise PlanProblem("template_unknown")
        except PlanProblem as problem:
            start_refusal(org_id, run_id, actor_id, problem.code)
            refused_plan = "the plan is refused"
            if problem.code == "plan_cycle":
                raise Problem(422, "plan_cycle", refused_plan) from None
            if problem.code == "plan_too_many_tasks":
                raise Problem(422, "plan_too_many_tasks", refused_plan) from None
            if problem.code == "template_unknown":
                raise Problem(422, "template_unknown", refused_plan) from None
            raise Problem(422, "plan_invalid", refused_plan) from None
        # step 9: the engine
        if starter is None or not starter.health().ok:
            raise Problem(503, "engine_unavailable", "no engine can take a run start now")
        # step 10: budget headroom (read only)
        try:
            check_start_headroom(store, config.org, run_id)
        except RunBudgetRefused as refused:
            start_refusal(org_id, run_id, actor_id, refused.code)
            no_budget = "the organization has no budget to start a run"
            if refused.code == "run_budget_missing":
                raise Problem(409, "run_budget_missing", no_budget) from None
            if refused.code == "run_budget_blocked":
                raise Problem(409, "run_budget_blocked", no_budget) from None
            raise Problem(409, "run_budget_exhausted", no_budget) from None
        # step 11: record the request and the plan text
        try:
            with idem.effect() as uow:
                if uow.count_active_runs(org_id) >= start_settings.max_active:
                    raise AuthProblem(  # inside the insert transaction: no overshoot
                        429,
                        "run_cap_reached",
                        "too many unfinished runs and pending starts for this organization",
                        {"Retry-After": "30"},
                    )
                outcome = record_start_request(
                    uow,
                    service,
                    org_id=org_id,
                    run_id=run_id,
                    actor_id=actor_id,
                    tasks=body.tasks,
                    data_class=body.data_class,
                    engine=starter.name,
                    idempotency_key_hash=idem.key_hash,
                    plan_text_days=start_settings.plan_text_days,
                )
                if outcome == "conflict":
                    raise RunIdConflict(run_id)
                answer = {
                    "run_id": run_id,
                    "state": "accepted" if outcome == "created" else "already_requested",
                    "engine": starter.name,
                }
                response = idem.respond(
                    uow,
                    202,
                    answer,
                    subject=actor_id if actor_id.startswith("human:") else None,
                    headers={"Location": f"/v1/runs/{run_id}"},
                )
        except RunIdConflict:
            raise Problem(
                409, "run_id_conflict", "this run id was used with another plan"
            ) from None
        except ContentError as exc:
            raise _content_problem(exc, org_id) from None
        service.flush_alerts()
        start_stored_request(store, starter, org_id, run_id)
        return response

    def acting_org(cfg: NuraMeshConfig, org: str | None) -> str:
        """The organization a caller acts on: their own."""
        return org if org is not None and is_org(org) else cfg.org.id

    def role_ids(cfg: NuraMeshConfig, org_id: str) -> list[str]:
        """The roles the routes name: those of the server's organization, never another's."""
        return [r.id for r in cfg.org.roles] if org_id == cfg.org.id else []

    def can_act_of(who: Caller) -> bool:
        """Whether this caller may press the buttons: a signed-in person whose role allows
        decide, or (authentication off) the in-process `dev_human` harness. The server enforces
        the same rule on every POST regardless."""
        if require_auth:
            return who.human and Permission.DECIDE in who.permissions
        return dev_human is not None

    def is_owner(who: Caller) -> bool:
        """An owner in a human session (the `ADMIN` permission); the dev person when
        authentication is off. The config-expiry warning (ND194) is for owners only."""
        if require_auth:
            return who.human and Permission.ADMIN in who.permissions
        return dev_human is not None

    def halt_body(
        org_id: str, cfg: NuraMeshConfig, who: Caller, events: Sequence[Any] | None = None
    ) -> dict[str, Any]:
        if events is None:
            body = org_views.halt_view(store, org_id, role_ids(cfg, org_id), can_act_of(who))
        else:
            body = org_views.halt_from_events(events, role_ids(cfg, org_id), can_act_of(who))
        warning = (
            config_expiry_warning(store, content, org_id)
            if content is not None and is_owner(who)
            else None
        )
        return {**body, "warnings": [] if warning is None else [warning]}

    @app.get("/v1/org/status")
    @app.get("/v1/org/halt", deprecated=True)
    def halt_status(cfg: Config, org: Org, who: Who) -> dict[str, Any]:
        """Whether the organization is halted, who and why, and every role's state (E19), and
        `solo_approval`: whether one person alone can approve now, and how many of the last 30
        days' approved requests were solo (spec/rbac/solo-approval.md SA-18)."""
        org_id = acting_org(cfg, org)
        events = read_org_events(store, org_id)  # one read of the stream for both views
        solo = solo_status_view(
            store,
            org_id,
            cfg.policies.solo_limit,
            lambda run: bool(_SLUG.fullmatch(run)) and scope.visible(org, f"run:{run}"),
            store.now(),
            events,
        )
        return {**halt_body(org_id, cfg, who, events), "solo_approval": solo}

    @app.post("/v1/org/halt")
    def halt_organization(who_: Human, cfg: Config, org: Org, who: Who) -> dict[str, Any]:
        """A person halts every run of the organization and refuses new work (E19). Running
        steps finish (Stop is cooperative); nothing new starts. Halting a halted org is 409."""
        org_id = acting_org(cfg, org)
        outcome = halt_org(store, org_id, actor=who_, via="api")
        if outcome.already_halted:
            raise Problem(409, "already_halted", "the organization is already halted")
        return {**halt_body(org_id, cfg, who), "stopped_runs": list(outcome.stopped_runs)}

    @app.put("/v1/org/prompt-cache")
    def set_prompt_cache(
        body: PromptCacheBody, who_: Human, cfg: Config, org: Org, idem: Idem
    ) -> Response:
        """An organization administrator turns provider-side prompt caching on or off (off by
        default; R1.34, ND320). One `decision.recorded` entry per change; asking for the state it
        already has writes nothing. Refused in hosted mode until the privacy notes ship."""
        org_id = acting_org(cfg, org)  # the organization is the caller's own, never in the path
        if org_id != cfg.org.id:
            raise Problem(404, "not_found", "no such organization")  # never says which exist
        try:
            refuse_if_hosted()
        except PromptCacheHosted as exc:
            raise Problem(403, "hosted_refused", str(exc)) from None
        idem.require_alive()
        try:
            with idem.effect() as uow:
                if uow.key_tombstone(org_id) is not None or (
                    content is not None and content.retired(org_id) is not None
                ):
                    raise OrgErasedError("the organization was erased")
                change = set_prompt_cache_in(uow, org_id, body.enabled, actor=who_, via="api")
                return idem.respond(
                    uow,
                    200,
                    {
                        "organization": org_id,
                        "prompt_cache": {
                            "enabled": change.enabled,
                            "changed": change.changed,
                            "ledger_seq": change.seq,
                            "provider_retention": RETENTION_NOTE,
                        },
                    },
                    subject=who_,
                )
        except OrgErasedError as exc:
            raise Problem(409, "org_erased", str(exc)) from None

    @app.post("/v1/org/resume")
    def resume_organization(who_: Human, cfg: Config, org: Org, who: Who, idem: Idem) -> Response:
        """A person lifts the halt (E19). Stopped runs are not restarted; roles stay paused and
        breakers stay tripped."""
        org_id = acting_org(cfg, org)
        idem.require_alive()
        try:
            with idem.effect() as uow:
                outcome = resume_org_in(uow, org_id, actor=who_, via="api", content=content)
                if outcome.already_running:
                    raise Problem(409, "already_running", "the organization is not halted")
                response = idem.respond(
                    uow,
                    200,
                    org_views.halt_from_events(
                        outcome.events, role_ids(cfg, org_id), can_act_of(who)
                    ),
                )
        except OrgErasedError as exc:
            raise Problem(409, "org_erased", str(exc)) from None
        delivery_pass("resume")  # never raises: a failed pass never fails the resume
        return response

    def missing_role() -> Problem:
        return Problem(404, "no_such_role", "no such role")

    def missing_breaker() -> Problem:
        return Problem(404, "no_such_breaker", "no such breaker")

    def foreign(cfg: NuraMeshConfig, org: str | None) -> bool:
        """A caller of another organization: the routes' missing answer (spec 11)."""
        return org_of_config_mismatch(cfg, org)

    def org_of_config_mismatch(cfg: NuraMeshConfig, org: str | None) -> bool:
        return org is not None and is_org(org) and org != cfg.org.id

    @app.post("/v1/org/roles/{role}/pause")
    def pause_org_role(
        role: str, who_: Human, cfg: Config, org: Org, who: Who, body: EmptyBody | None = None
    ) -> dict[str, Any]:
        """A person pauses one role: its tasks and model calls stop, other roles keep working."""
        if foreign(cfg, org) or not _SLUG.fullmatch(role):
            raise missing_role()
        try:
            outcome = pause_role(store, cfg.org, role, actor=who_, via="api")
        except NoSuchRole:
            raise missing_role() from None
        if outcome.already_paused:
            raise Problem(409, "already_paused", "the role is already paused")
        return {
            **halt_body(cfg.org.id, cfg, who),
            "stopped_tasks": list(outcome.stopped_tasks),
        }

    @app.post("/v1/org/roles/{role}/resume")
    def resume_org_role(
        role: str,
        who_: Human,
        cfg: Config,
        org: Org,
        who: Who,
        idem: Idem,
        body: EmptyBody | None = None,
    ) -> Response:
        """A person lifts a role pause (the role may have been removed from the config)."""
        if foreign(cfg, org) or not _SLUG.fullmatch(role):
            raise missing_role()
        idem.require_alive()
        try:
            with idem.effect() as uow:
                outcome = resume_role_in(uow, cfg.org, role, actor=who_, via="api")
                if outcome.not_paused:
                    raise Problem(409, "not_paused", "the role is not paused")
                return idem.respond(
                    uow,
                    200,
                    org_views.halt_from_events(
                        outcome.events, role_ids(cfg, cfg.org.id), can_act_of(who)
                    ),
                )
        except NoSuchRole:
            raise missing_role() from None

    def evaluation_view(cfg: NuraMeshConfig, org_id: str) -> dict[str, Any]:
        if org_id != cfg.org.id or not breaker_rules:
            status = {
                "state": "not_configured",
                "tick_seconds": cfg.breakers.tick_seconds,
                "halt_after_failed_ticks": cfg.breakers.halt_after_failed_ticks,
                "last_evaluated_at": None,
                "last_error_at": None,
                "last_error_phase": None,
                "last_error_class": None,
                "last_failing_rule": None,
                "consecutive_failures": 0,
            }
            return status
        if ticker is None:
            return {
                "state": "stopped",
                "tick_seconds": cfg.breakers.tick_seconds,
                "halt_after_failed_ticks": cfg.breakers.halt_after_failed_ticks,
                "last_evaluated_at": None,
                "last_error_at": None,
                "last_error_phase": None,
                "last_error_class": None,
                "last_failing_rule": None,
                "consecutive_failures": 0,
            }
        return ticker.status()

    @app.get("/v1/org/breakers")
    def breakers_status(cfg: Config, org: Org, who: Who) -> dict[str, Any]:
        """Every circuit breaker: its live numbers (or those recorded at a trip), its state, the
        halt state and how the server's checks are doing. Writes nothing."""
        org_id = acting_org(cfg, org)
        rules = breaker_rules if org_id == cfg.org.id else ()
        return org_views.breakers_view(
            store,
            org_id,
            role_ids(cfg, org_id),
            rules,
            evaluation_view(cfg, org_id),
            can_act_of(who),
            store.now(),
        )

    def known_breaker(cfg: NuraMeshConfig, name: str) -> bool:
        return any(r.id == name for r in breaker_rules)

    @app.post("/v1/org/breakers/{name}/reset")
    def reset_org_breaker(
        name: str, who_: Human, cfg: Config, org: Org, idem: Idem, body: EmptyBody | None = None
    ) -> Response:
        """A person clears a tripped breaker; its count starts afresh. Does not resume the org."""
        if foreign(cfg, org) or not _SLUG.fullmatch(name):
            raise missing_breaker()
        if not known_breaker(cfg, name) and not org_views.ever_tripped(store, cfg.org.id, name):
            raise missing_breaker()
        idem.require_alive()
        with idem.effect() as uow:
            done = reset_breaker_in(uow, cfg.org.id, name, actor=who_, via="api")
            if done is None:
                raise Problem(409, "not_tripped", "the breaker is not tripped")
            return idem.respond(
                uow,
                200,
                {
                    "name": name,
                    "state": "ok",
                    "reset_at": done.at.isoformat(),
                    "ledger_seq": done.seq,
                    "org_halted": done.halted,
                },
            )

    @app.post("/v1/org/breakers/{name}/disable")
    def disable_org_breaker(
        name: str, body: DisableBody, who_: Human, cfg: Config, org: Org, idem: Idem
    ) -> Response:
        """A person switches a breaker off (it is not evaluated until a person enables it)."""
        if foreign(cfg, org) or not _SLUG.fullmatch(name) or not known_breaker(cfg, name):
            raise missing_breaker()
        idem.require_alive()
        try:
            with idem.effect() as uow:
                done = disable_breaker_in(
                    uow, cfg.org.id, name, DisableReason(body.reason).value, actor=who_, via="api"
                )
                return idem.respond(
                    uow,
                    200,
                    {
                        "name": name,
                        "state": "disabled",
                        "disabled_at": done.at.isoformat(),
                        "reason": body.reason,
                        "ledger_seq": done.seq,
                    },
                )
        except StateConflict as exc:
            raise Problem(409, exc.code, _CONFLICTS[exc.code]) from None

    @app.post("/v1/org/breakers/{name}/enable")
    def enable_org_breaker(
        name: str, who_: Human, cfg: Config, org: Org, idem: Idem, body: EmptyBody | None = None
    ) -> Response:
        """A person switches a disabled breaker back on; its count starts afresh."""
        if foreign(cfg, org) or not _SLUG.fullmatch(name):
            raise missing_breaker()
        disabled = org_views.rule_state(store, cfg.org.id, name).disabled
        if not known_breaker(cfg, name) and not disabled:
            raise missing_breaker()
        idem.require_alive()
        try:
            with idem.effect() as uow:
                done = enable_breaker_in(uow, cfg.org.id, name, actor=who_, via="api")
                return idem.respond(
                    uow,
                    200,
                    {
                        "name": name,
                        "state": "ok",
                        "enabled_at": done.at.isoformat(),
                        "ledger_seq": done.seq,
                    },
                )
        except StateConflict as exc:
            raise Problem(409, exc.code, _CONFLICTS[exc.code]) from None

    @app.get("/v1/runs/{run_id}/report")
    def report(run_id: str, cfg: Config, org: Org) -> dict[str, Any]:
        need_run(org, run_id)
        if not store.tasks_for_run(run_id):
            raise no_run()
        return build_report(store, cfg.org, run_id, redact_reasons=True).to_dict()

    @app.get("/v1/runs/{run_id}/tasks")
    def tasks(
        request: Request,
        run_id: str,
        org: Org,
        who: Who,
        limit: Limit = None,
        cursor: Cursor = None,
    ) -> Response:
        need_run(org, run_id)
        if who.kind == "agent" and who.agent_id is not None:
            rows = views.agent_tasks(store, run_id, who.agent_id)  # narrowed before paging
        else:
            rows = [
                t.task.model_dump(mode="json") | {"version": t.version}
                for t in store.tasks_for_run(run_id)
            ]
        return paginate(
            request, rows, route="GET /v1/runs/{run_id}/tasks", old_key=None,
            limit=limit, cursor=cursor, scope=run_id,
        )  # fmt: skip

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/timeline")
    def timeline(
        request: Request,
        run_id: str,
        task_id: str,
        org: Org,
        limit: Limit = None,
        cursor: Cursor = None,
    ) -> Response:
        need_run(org, run_id)
        known = store.get_task(task_id)
        if known is None or known.task.run_id != run_id:
            raise Problem(404, "not_found", "no such task")
        return paginate(
            request,
            [views.event_json(e) for e in store.task_events(run_id, task_id)],
            route="GET /v1/runs/{run_id}/tasks/{task_id}/timeline", old_key=None,
            limit=limit, cursor=cursor, scope=f"{run_id}/{task_id}", key=lambda e: str(e["seq"]),
        )  # fmt: skip

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/audit")
    def task_audit(run_id: str, task_id: str, org: Org) -> dict[str, Any]:
        """Everything that happened to one task, in order, with evidence (R1.27); read only."""
        need_run(org, run_id)
        known = store.get_task(task_id)
        if known is None or known.task.run_id != run_id:
            raise Problem(404, "not_found", "no such task")
        return views.task_audit(store, run_id, known, store.approvals(run_id=run_id))

    # -- ledger ------------------------------------------------------------------------------

    def verification(streams: list[str]) -> dict[str, Any]:
        """`verify_streams` inside one of `MAX_VERIFY_IN_FLIGHT` slots; when every slot is taken
        the caller gets 429 `busy` at once, before any stream is read (TM-10): the check never
        waits for a slot, so a flood of verify calls cannot hold the thread pool."""
        if not verify_slots.acquire(blocking=False):
            raise AuthProblem(429, "busy", "the server is busy; try again", {"Retry-After": "1"})
        try:
            return verify_streams(streams)
        finally:
            verify_slots.release()

    def verify_streams(streams: list[str]) -> dict[str, Any]:
        """Chain check per stream (against the database's own head) and, when the server was
        started with `--anchors` and `--pubkey`, the signed anchors in Git (ND22)."""
        anchor_reports: dict[str, Any] = {}
        anchors_state: dict[str, Any] = {"configured": anchors is not None}
        if anchors is not None:
            try:
                from nuramesh.engine.anchors import (
                    AnchorError,
                    load_public_key,
                    verify_anchor_repository,
                )

                try:
                    verification = verify_anchor_repository(
                        store,
                        anchors.repo,
                        [load_public_key(k) for k in (anchors.pubkey, *anchors.extra_pubkeys)],
                        streams=streams,
                        ref=anchors.ref,
                        tail_valid_until=anchors.tail_valid_until,
                    )
                    for r in verification.streams:
                        anchor_reports[r.stream] = r
                    anchors_state["checked"] = True
                    anchors_state["repository"] = {
                        "ok": not verification.repository_problems,
                        "problems": [
                            {"code": p.code, "detail": p.detail}
                            for p in verification.repository_problems
                        ],
                    }
                    for problem in verification.repository_problems:
                        logging.getLogger("nuramesh.server").warning(
                            "anchor repository problem %s: %s", problem.code, problem.detail
                        )
                except (AnchorError, OSError) as exc:
                    anchors_state.update(
                        checked=False,
                        error="the anchors could not be checked (repository, branch, key or "
                        "signatures); see the server log",
                    )
                    logging.getLogger("nuramesh.server").warning("anchor check failed: %s", exc)
            except ImportError:
                anchors_state.update(checked=False, error="anchors need nuramesh[anchors]")
        results = []
        for stream in streams:
            check = store.verify_stream(stream)
            try:
                head = store.head(stream)
            except StoreError:  # a head with a time that is not real: the check above says so
                head = None
            report = anchor_reports.get(stream)
            entry: dict[str, Any] = {
                "stream": stream,
                "ok": check.ok,
                "checked": check.checked,
                "problem": check.problem.value if check.problem else None,
                "at_seq": check.at_seq,
                "head": head.hash if head else None,
                "anchors": None
                if report is None
                else {
                    "status": "problems"
                    if report.problems
                    else ("anchored" if report.anchored else "unanchored"),
                    "anchored": report.anchored,
                    "count": report.anchors,
                    "unanchored_events": report.unanchored_events,
                    "problems": [{"code": p.code, "detail": p.detail} for p in report.problems],
                },
            }
            if report is not None and not report.ok:
                entry["ok"] = False
            results.append(entry)
        repository_ok = anchors_state.get("repository", {"ok": True})["ok"]
        verified = (
            repository_ok
            and bool(results)
            and all(
                r["problem"] is None
                and r["anchors"] is not None
                and r["anchors"]["status"] == "anchored"
                for r in results
            )
        )
        return {
            "ok": all(r["ok"] for r in results)
            and anchors_state.get("checked", True)
            and repository_ok,
            "chain_ok": all(r["problem"] is None for r in results),
            # true only when every stream's chain is intact AND has signed anchors that check out;
            # an unanchored stream is `ok` (nothing wrong found) but never `verified`
            "verified": verified,
            "streams": results,
            "anchors": anchors_state,
            "note": "chains are checked against the database's own head; anchors prove history",
        }

    @app.get("/v1/ledger")
    def ledger_streams(
        request: Request, org: Org, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        """The caller's organization's streams and their heads (the ledger's table of contents).
        `truncated`: more streams exist than one request may look up (spec R9)."""
        heads = store.heads()
        damaged = set(store.damaged_streams())
        names, truncated = scope.filter_streams(
            org, sorted(set(heads) | damaged), LookupBudget(MAX_NEW_LOOKUPS)
        )
        rows = [
            {"stream": s, "seq": None, "head": None, "updated": None, "problem": "bad_time"}
            if s in damaged
            else {
                "stream": s,
                "seq": heads[s].seq,
                "head": heads[s].hash,
                "updated": heads[s].at.isoformat(),
            }
            for s in names
        ]
        return paginate(
            request, rows, route="GET /v1/ledger", old_key="streams", limit=limit, cursor=cursor,
            key=lambda r: str(r["stream"]), extra={"truncated": truncated},
        )  # fmt: skip

    @app.get("/v1/ledger/verify")
    def verify_all(
        request: Request,
        org: Org,
        limit: Annotated[int, Query(ge=1, le=MAX_VERIFY_STREAMS)] = 50,
        after: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Cursor = None,
    ) -> Response:
        """Verify the caller's organization's streams in name order, at most `limit` (100) per
        request: continue with `after=<next_after>` while `truncated` is true (spec R9a).
        `next_after` is the last stream **shown**, or the `after` sent when the page shows none,
        so it never names another organization's stream; the next request goes on because the
        streams already looked up are remembered and cost nothing."""
        throttle(verify_limit, request, verify_caller_limit)
        route = "GET /v1/ledger/verify"
        refuse_old_parameters(cursor, after=after)
        start = after or ""
        if cursor is not None:  # a position is a stream this caller may see (or the start)
            start = decode_position(cursor, route)
            if start and not scope.visible(org, start):
                raise invalid()
        after = start
        names = sorted(s for s in {*store.heads(), *store.damaged_streams()} if s > after)
        found, cut = scope.filter_streams(
            org, names, LookupBudget(MAX_NEW_LOOKUPS), stop_after=limit + 1
        )
        chosen = found[:limit]
        result = verification(chosen)
        truncated = len(found) > limit or cut
        last = (chosen[-1] if chosen else after) if truncated else None
        return respond(
            request,
            items=result["streams"],
            old_key="streams",
            next_cursor=None if last is None else encode_cursor(route, last),
            extra={**{k: v for k, v in result.items() if k != "streams"}, "truncated": truncated},
            old_extra={"next_after": last},
            native=True,
        )

    @app.get("/v1/ledger/{stream}")
    def ledger(
        request: Request,
        stream: str,
        org: Org,
        who: Who,
        after_seq: Annotated[int | None, Query(ge=0, le=MAX_SEQ)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
        cursor: Cursor = None,
    ) -> Response:
        need_stream(org, stream)
        route = "GET /v1/ledger/{stream}"
        refuse_old_parameters(cursor, after_seq=after_seq)
        start = after_seq or 0
        if cursor is not None:
            start = seq_position(decode_position(cursor, route, stream))
        read = list(store.read_stream(stream, after_seq=start, limit=limit))
        events = [views.event_json(e) for e in read if shown_to(who, stream, e.type)]
        head = store.head(stream)
        if head is None:
            raise Problem(404, "not_found", "no such stream")
        if cursor is not None and start > head.seq:
            raise invalid()
        # the last entry READ, not the last shown: paging never stalls on hidden entries
        last = read[-1].seq if read else start
        return respond(
            request,
            items=events,
            old_key="events",
            next_cursor=(
                encode_cursor(route, position(stream, str(last))) if last < head.seq else None
            ),
            extra={"stream": stream, "head_seq": head.seq, "head": head.hash},
            old_extra={"next_after_seq": last},
            native=True,
        )

    @app.get("/v1/ledger/{stream}/verify")
    def verify(request: Request, stream: str, org: Org) -> dict[str, Any]:
        throttle(verify_limit, request, verify_caller_limit)  # before the lookup (TM-10)
        need_stream(org, stream)
        result = verification([stream])
        return {
            **result["streams"][0],
            "ok": result["ok"],  # also false when configured anchors could not be checked
            "verified": result["verified"],
            "chain_ok": result["chain_ok"],
            "anchor_status": result["anchors"],
            "note": result["note"],
        }

    # -- live events -------------------------------------------------------------------------

    live = {"n": 0}  # open live connections (all on the one event loop thread)

    async def take_slot(websocket: WebSocket) -> bool:
        """Count a live connection from the moment it is accepted (before it has said anything);
        refuse with close code 1013 when the cap is reached."""
        if live["n"] >= max_live_connections:
            await websocket.close(code=1013, reason="too many live connections; try again later")
            return False
        live["n"] += 1
        return True

    async def serve_events(
        websocket: WebSocket, wanted: set[str] | None, cursor: dict[str, int]
    ) -> None:
        """Replay after `cursor`, then push new events of `wanted` streams (None = all)."""

        recheck = websocket.scope.get("state", {}).get("recheck")
        who = websocket.scope.get("state", {}).get("caller")
        org = who.org_id if isinstance(who, Caller) else None

        def read_new() -> list[tuple[str, Any]]:
            """One head query, then only the streams that moved; at most MAX_PAGE per tick.
            With authentication on, the caller is checked again first, in this same worker call
            (a revoked token, a logged-out session or an erased user ends the stream, M1)."""
            if recheck is not None:
                recheck()
            found: list[tuple[str, Any]] = []
            lookups = LookupBudget(MAX_NEW_LOOKUPS_PER_TICK)
            for stream, head in store.heads(wanted).items():
                if len(found) >= MAX_PAGE:
                    break
                if (wanted is None or stream in wanted) and head.seq > cursor.get(stream, 0):
                    # a stream of another organization is never read; one not yet looked up
                    # waits for the next tick when this tick's lookups are spent
                    if not scope.visible_within(org, stream, lookups):
                        continue
                    for event in store.read_stream(
                        stream, after_seq=cursor.get(stream, 0), limit=MAX_PAGE - len(found)
                    ):
                        found.append((stream, event))  # hidden ones advance the cursor, unsent
            return found

        async def watch_disconnect() -> None:
            # without a reader a closed connection is only noticed on the next send
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        async def push() -> None:
            announced = False
            while True:
                try:
                    batch = await anyio.to_thread.run_sync(read_new)
                except AuthProblem:
                    await websocket.close(code=CLOSE_CREDENTIAL_ENDED)  # refresh, then reconnect
                    return
                except StoreBusyError:
                    await asyncio.sleep(events_poll_seconds)  # nothing is sent unchecked
                    continue
                for stream, event in batch:
                    if shown_to(who, stream, event.type):
                        await websocket.send_json(
                            {"stream": stream, "event": views.event_json(event)}
                        )
                    cursor[stream] = event.seq
                if not batch:
                    if not announced:
                        announced = True
                        await websocket.send_json({"type": "caught_up", "cursor": dict(cursor)})
                    await asyncio.sleep(events_poll_seconds)

        pusher = asyncio.ensure_future(push())
        watcher = asyncio.ensure_future(watch_disconnect())
        try:
            await asyncio.wait({pusher, watcher}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (pusher, watcher):
                task.cancel()
        failure = pusher.exception() if pusher.done() and not pusher.cancelled() else None
        if failure is not None and not isinstance(failure, WebSocketDisconnect | RuntimeError):
            # a closed socket is the only expected way to end; anything else is logged, not hidden
            logging.getLogger("nuramesh.server").error("live events failed", exc_info=failure)
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1011)

    @app.websocket("/v1/events")
    async def events(websocket: WebSocket) -> None:
        """Stream ledger events as they are committed (R1.21; docs/architecture-platform.md §6).

        The client must send one JSON message first (within 5 s): `{"cursor": {"run:r1": 12},
        "streams": ["run:r1"]}` (`streams` omitted or `["*"]` = every stream, including new
        ones). Events
        after each cursor are replayed, then new ones are pushed as `{"stream", "event"}` (the
        ledger event itself: hashes and verdicts, never content, A7). A client that reconnects with
        its last cursor misses nothing and sees nothing twice. `{"type": "caught_up"}` marks the end
        of a replay. Events are read from the store every `events_poll_seconds` (default 0.5 s), so
        events appended by **another process** (the CLI writing the same database) arrive too, and
        a missed notification (R1.21's LISTEN/NOTIFY layer) can only delay an update.
        """
        await websocket.accept()
        if not isinstance(websocket.scope.get("state", {}).get("caller"), Caller):
            await websocket.close(code=1008)  # the authorizer always sets one; never guess
            return
        if not await take_slot(websocket):
            return
        try:
            await events_session(websocket)
        finally:
            live["n"] -= 1

    async def events_session(websocket: WebSocket) -> None:
        try:
            first = await asyncio.wait_for(
                websocket.receive(), timeout=EVENTS_FIRST_MESSAGE_SECONDS
            )
        except TimeoutError:
            await websocket.close(code=1008)  # subscribe first; a late message is never ignored
            return
        if first["type"] == "websocket.disconnect":
            return
        text = first.get("text")
        if text is None:
            await websocket.close(code=1003)  # binary frames aren't part of the protocol
            return
        try:
            request = json.loads(text) if text.strip() else {}
        except (ValueError, RecursionError):  # bad JSON, a 5000-digit number, absurd nesting
            await websocket.close(code=1003)
            return
        parsed = _parse_subscription(request)
        if parsed is None:
            await websocket.close(code=1003)
            return
        wanted, cursor = parsed
        await serve_events(websocket, wanted, cursor)

    @app.websocket("/v1/runs/{run_id}/events")
    async def run_events_socket(websocket: WebSocket, run_id: str, after_seq: int = 0) -> None:
        """One run's live ledger: no subscribe message needed. `?after_seq=N` resumes after entry
        N (the `seq` of the last event the client saw); the messages are those of `/v1/events`.
        Closes with 4404 if the run has no ledger stream."""
        await websocket.accept()
        who = websocket.scope.get("state", {}).get("caller")
        if not isinstance(who, Caller):
            await websocket.close(code=1008)  # the authorizer always sets one; never guess
            return
        if not await take_slot(websocket):
            return
        try:
            stream = f"run:{run_id}"
            known = False
            if _SLUG.fullmatch(run_id):  # a store call may block: off the event loop
                known = await anyio.to_thread.run_sync(scope.visible, who.org_id, stream)
            if not known:
                await websocket.close(code=4404)
                return
            if not 0 <= after_seq <= MAX_SEQ:
                await websocket.close(code=1003)
                return
            await serve_events(websocket, {stream}, {stream: after_seq})
        finally:
            live["n"] -= 1

    # -- approvals and budgets ---------------------------------------------------------------

    @app.get("/v1/approvals")
    def approvals(
        request: Request,
        org: Org,
        status: ApprovalStatus | None = None,
        run_id: str | None = None,
        approval_mode: ApprovalMode | None = None,
        limit: Limit = None,
        cursor: Cursor = None,
    ) -> Response:
        """The caller's organization's approvals (those of runs it may see). `approval_mode`
        keeps the decided rows of that mode; a row decided before modes has none."""
        rows = [
            views.approval_json(a.approval)
            for a in store.approvals(run_id=run_id, status=status)
            if _SLUG.fullmatch(a.approval.run_id)
            and scope.visible(org, f"run:{a.approval.run_id}")
            and (approval_mode is None or a.approval.approval_mode is approval_mode)
        ]
        return paginate(
            request, rows, route="GET /v1/approvals", old_key=None, limit=limit, cursor=cursor
        )

    @app.get("/v1/approvals/{approval_id}")
    def approval(approval_id: str, org: Org, who: Who, cfg: Config) -> dict[str, Any]:
        """One approval request, with the ledger entries that name it, and `caller_terms`: what
        an approve by this caller would be asked for now (null unless the request is pending and
        the caller is a person in a cookie session; computed, never stored)."""
        stored = need_approval(org, approval_id)
        run_events = views.read_all(store, f"run:{stored.approval.run_id}")
        terms = (
            terms_view(
                store, who, auth_service, cfg.org.id, cfg.policies.solo_limit, stored, store.now()
            )
            if require_auth
            else None
        )
        return {**views.approval_view(stored, run_events), "caller_terms": terms}

    def _answer_approval(
        approval_id: str,
        approve: bool,
        reason: str,
        who: Caller,
        cfg: NuraMeshConfig,
        org: str | None,
        idem: IdemContext,
        fields: _SoloFields | None = None,
    ) -> Response:
        """SA-12 from step 4 on. The solo terms go to the engine only for a person in a cookie
        session of a server with authentication on (SA-2); the `dev_human` harness gets none."""
        assert who.actor is not None
        need_approval(org, approval_id)  # another organization's approval: the missing answer
        idem.require_alive()
        terms = (
            solo_terms(
                who,
                auth_service,
                cfg.policies.solo_limit,
                acknowledge=fields is not None and fields.acknowledge_solo,
                action_digest=None if fields is None else fields.digest,
            )
            if require_auth
            else None
        )
        if require_auth and terms is None:
            # None means "never solo" to the engine: with authentication on that would skip
            # SA-5 to SA-9 silently, so a caller without terms is refused.
            raise AuthProblem(403, "human_required", "this needs a signed-in person")
        try:
            service = _content()
            service.ledger_hashing.reset_cache()  # PH-KEY R5: the cache never outlives a request
            with idem.effect() as uow:
                answered = answer_approval_in(
                    uow,
                    approval_id,
                    approver=who.actor,
                    approve=approve,
                    reason=reason,
                    policies=cfg.effective_policies,
                    org=cfg.org,
                    content=service,
                    hashing=service.ledger_hashing,
                    solo=terms,
                )
                return idem.respond(uow, 200, views.approval_json(answered))
        except SelfApproval as exc:
            raise Problem(403, "self_approval", str(exc)) from None
        except SoloRefused as exc:
            # nothing was written: the first failing step of SA-12 step 7 answers
            refused = solo_problem(exc)
            if exc.retry_after is not None:  # SA-9, spelled out for the OpenAPI status walker
                raise AuthProblem(
                    429, "solo_rate_limited", refused.detail, refused.headers
                ) from None
            raise refused from None
        except ApprovalError as exc:
            raise Problem(409, "approval_refused", str(exc)) from None
        except ContentError as exc:
            raise _content_problem(exc, cfg.org.id) from None

    @app.post("/v1/approvals/{approval_id}/decision", deprecated=True)
    def decide(
        approval_id: str,
        body: ApprovalDecision,
        who: HumanCaller,
        cfg: Config,
        org: Org,
        idem: Idem,
    ) -> Response:
        fields = body if body.approve else None  # read only for an approve (SA-16)
        return _answer_approval(approval_id, body.approve, body.reason, who, cfg, org, idem, fields)

    @app.post("/v1/approvals/{approval_id}/approve")
    def approve_approval(
        approval_id: str,
        body: ApprovalApprove,
        who: HumanCaller,
        cfg: Config,
        org: Org,
        idem: Idem,
    ) -> Response:
        """A person approves the request (the answer `/decision` took with `approve: true`). A
        sole decide-holder also sends `acknowledge_solo: true` and the `action_digest` the
        approval view showed; a high-risk kind then needs a fresh proof."""
        return _answer_approval(approval_id, True, body.reason, who, cfg, org, idem, body)

    @app.post("/v1/approvals/{approval_id}/reject")
    def reject_approval(
        approval_id: str,
        body: ApprovalReason,
        who: HumanCaller,
        cfg: Config,
        org: Org,
        idem: Idem,
    ) -> Response:
        """A person rejects the request (stored as `denied`, as `approve: false` was)."""
        return _answer_approval(approval_id, False, body.reason, who, cfg, org, idem)

    _DECISION_STATUS = {
        "not_found": 404,
        "no_human_gate": 409,
        "decider_not_allowed": 403,
        "invalid_decision": 422,
        "already_decided": 409,
        "stale_round": 409,
        "run_ended": 409,
        "run_stopping": 409,
        "not_waiting": 409,
        "human_decision_refused": 409,
        "content_store_unavailable": 503,
    }

    _TOP = frozenset({"attempt", "submission", "findings", "acknowledge_solo"})
    # fixed text; a gate decision has no digest (SA-19)
    _SOLO_GATE_TEXT = "you are the only person who can decide this: acknowledge the solo decision"
    _FINDING = frozenset({"approved", "reason"})
    waker_pool = (
        None
        if on_human_decision is None
        else ThreadPoolExecutor(max_workers=4, thread_name_prefix="nuramesh-waker")
    )

    async def decision_body(who: Human, request: Request) -> GateDecisionBody:
        """The decision body, read only after a human is known (`who` is resolved first): with no
        credentials even a body that is not JSON answers 401 and nothing of it is read. The answer
        never repeats anything the client sent: a field it invented is "unknown field" at "body",
        a criterion name is written `<criterion>`."""

        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            keys = [k for k, _ in pairs]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate key")  # never last-wins: which one was meant?
            return dict(pairs)

        raw = await request.body()
        try:
            parsed = json.loads(raw, object_pairs_hook=no_duplicates)
        except (ValueError, RecursionError) as exc:  # not JSON, not UTF-8, a repeated key or absurd
            # nesting: nothing is quoted
            what = "duplicate key" if str(exc) == "duplicate key" else "not valid JSON"
            raise Problem(
                422,
                "invalid_request",
                "the request is invalid",
                problems=[{"where": "body", "message": what}],
            ) from None
        try:
            return GateDecisionBody.model_validate(parsed, strict=True)
        except ValidationError as exc:
            problems = []
            for e in exc.errors():
                # by position, never by value: loc is (field), or (findings, <criterion>, field)
                loc = e["loc"]
                where = ["body"]
                if loc and loc[0] in _TOP:
                    where.append(str(loc[0]))
                    if loc[0] == "findings" and len(loc) > 1:
                        where.append("<criterion>")
                        if len(loc) > 2 and loc[2] in _FINDING:
                            where.append(str(loc[2]))
                message = "unknown field" if e["type"] == "extra_forbidden" else e["msg"]
                problems.append({"where": ".".join(where), "message": message})
            raise Problem(
                422, "invalid_request", "the request is invalid", problems=problems
            ) from None

    # the body is read by `decision_body` (after the human check), so say what it is here
    _gate_body = {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inline_schema(GateDecisionBody)}},
        }
    }

    @app.post(
        "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions",
        status_code=201,
        responses=ALREADY_200,
        openapi_extra=_gate_body,
    )
    @app.post(
        "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision",
        deprecated=True,
        status_code=201,
        responses=ALREADY_200,
        openapi_extra=_gate_body,
    )
    async def decide_gate(
        run_id: str,
        task_id: str,
        gate_id: str,
        body: Annotated[GateDecisionBody, Depends(decision_body)],
        who: HumanCaller,
        org: Org,
        idem: Idem,
    ) -> JSONResponse:
        """A person decides a human gate that a durable run waits on (spec/steps/README.md,
        "Human decisions in a durable run"). The answer never contains the person's text. The
        record is written on a worker thread; waking the workflow (a network call, a few seconds
        at most) happens on its own small pool, so a slow Temporal cannot hold up the threads
        that authorize every other request.

        The organization's only person who can decide sends `acknowledge_solo: true` with a
        decision that passes the gate (403 `acknowledge_required` otherwise; a failing decision
        needs nothing more)."""
        made = await anyio.to_thread.run_sync(
            lambda: _record_gate_decision(run_id, task_id, gate_id, body, who, org, idem)
        )
        woken: bool | None = None
        held, held_reason = False, ""
        if waker_pool is not None and on_human_decision is not None:
            woken, held, held_reason = await anyio.to_thread.run_sync(
                lambda: _wake_gate(run_id, made, org)
            )
        payload = {
            "run_id": run_id,
            "task_id": task_id,
            "gate": made.gate,
            "attempt": made.attempt,
            "submission": made.submission,
            "outcome": made.outcome,
            "decision_hash": made.decision_hash,
            "already_recorded": made.already_recorded,
            "woken": woken,
            "held": held,
        }
        if held:
            payload["held_reason"] = held_reason
        return JSONResponse(payload, status_code=200 if made.already_recorded else 201)

    def _wake_gate(
        run_id: str, made: RecordedDecision, org: str | None
    ) -> tuple[bool | None, bool, str]:
        """The wake gate (deny-checks spec 4, step 6): `(woken, held, held_reason)`; the logic is
        `decision_wake.wake_gate`, shared with the Temporal client's `decide()`. The names it
        calls are passed in from here so a test can hook them."""
        cfg = need_config()
        if wake_fold is None:
            return None, False, ""
        return wake_gate(
            store,
            cfg.org.id,
            content,
            wake_fold,
            wake_with_limit,
            run_id,
            made,
            deny=deny_checks,
            marker=place_marker,
            deliver=deliver_held,
        )

    def _record_gate_decision(
        run_id: str,
        task_id: str,
        gate_id: str,
        body: GateDecisionBody,
        who: Caller,
        org: str | None,
        idem: IdemContext,
    ) -> RecordedDecision:
        if who.actor is None:  # `human_caller` admits only a person: refused, never assumed
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        # one answer for a malformed, a missing and another organization's run, never the id
        if not _SLUG.fullmatch(run_id):
            raise no_run()
        if not (_ITEM.fullmatch(task_id) and _ITEM.fullmatch(gate_id)):
            raise Problem(404, "not_found", "no such task or gate")
        if not scope.visible(org, f"run:{run_id}"):
            raise no_run()
        cfg = need_config()  # 409 no_config only for a run that exists and is visible
        stored = store.get_task(task_id)
        if stored is None or stored.task.run_id != run_id:  # the task check precedes the 503
            raise Problem(404, "not_found", "no such task or gate")
        idem.require_alive()
        findings = {c: (f.approved, f.reason) for c, f in body.findings.items()}
        # SA-2: the solo terms go to the engine only for a person in a cookie session of a
        # server with authentication on; the `dev_human` harness gets none ("never solo")
        terms = (
            solo_terms(
                who, auth_service, cfg.policies.solo_limit, acknowledge=body.acknowledge_solo
            )
            if require_auth
            else None
        )
        if require_auth and terms is None:
            # None means "never solo" to the engine: with authentication on that would skip the
            # acknowledgement silently, so a caller without terms is refused
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        try:
            made = record_decision(
                store,
                cfg.org,
                content,
                run_id,
                task_id,
                gate_id,
                decided_by=who.actor,
                findings=findings,
                round=(body.attempt, body.submission),
                solo=terms,
            )
        except DecisionRefused as exc:
            extra = {"why": exc.why} if exc.why else {}
            raise Problem(
                _DECISION_STATUS.get(exc.code, 409), exc.code, str(exc), **extra
            ) from None
        except SelfApproval as exc:
            raise Problem(403, "self_approval", str(exc)) from None
        except SoloRefused:
            # nothing was written: a solo passing decision without the acknowledgement, the
            # only solo term of a gate (SA-19: no digest, no fresh proof, no rate limit)
            raise AuthProblem(403, "acknowledge_required", _SOLO_GATE_TEXT) from None
        except DeciderNotAllowed as exc:
            raise Problem(403, "decider_not_allowed", str(exc)) from None
        except GateError as exc:
            raise Problem(422, "invalid_decision", str(exc)) from None
        except ContentError as exc:
            raise _content_problem(exc, cfg.org.id) from None
        return made

    def _content_problem(exc: ContentError, org_id: str | None) -> Problem:
        """The answer for a refused key use: 409 `org_key_destroyed` / `org_key_missing` (and one
        coalesced security-log row per organization, day and reason, written after the rollback;
        best effort), else the 503 for a key ring that can't be used. Also writes any queued
        `content.capture_failed` alert (R1.37 section 8, 8.5)."""
        service = content
        if service is not None:
            service.flush_alerts()
        if isinstance(exc, KeyDestroyedError | OrphanedContentError):
            if service is not None and org_id is not None:
                reason = "tombstone" if isinstance(exc, KeyDestroyedError) else "orphaned_content"
                service.record_security_count(org_id, "content.capture_refused", reason)
            return Problem(409, str(exc.code), str(exc))
        return _content_unavailable()

    def _content_unavailable() -> Problem:
        return Problem(
            503,
            "content_store_unavailable",
            "the reason couldn't be stored (check the key directory); nothing was changed",
        )

    def _content() -> ContentService:
        if content is None:
            raise Problem(
                503,
                "content_store_unavailable",
                "reasons are stored in the content store, which isn't configured "
                "(install nuramesh[content] and set NURAMESH_KEY_DIR)",
            )
        return content

    @app.get("/v1/content/{org_id}/{content_hash}")
    def reveal_content(org_id: str, content_hash: str, who: Human, cfg: Config) -> dict[str, Any]:
        """The text behind a keyed hash in the ledger (a person's reason, this server's own
        organization only). **Policy (default deny):** only a person whose role has `reveal`
        (approver or owner) in a human session; never an API token, never a viewer. A person
        reading is itself ledgered (`content.read`: who and which hash, never the text)."""
        if not _HASH.fullmatch(content_hash) or not _SLUG.fullmatch(org_id):
            raise Problem(422, "invalid_hash", "expected an organization id and a 64-digit hash")
        service = _content()
        info = service.describe(org_id, content_hash) if org_id == cfg.org.id else None
        if info is None or info.kind != ContentKind.REASON:
            raise Problem(404, "not_found", "no such content (never stored, expired or erased)")
        try:
            text = service.reveal(org_id, content_hash, actor=who, why="api")
        except ContentError as exc:
            if "integrity check" in str(exc):  # tampered bytes; a key-ring fault is not that
                raise Problem(
                    500, "content_unreadable", "the stored content failed its check"
                ) from None
            raise _content_unavailable() from None  # no server paths in the answer
        if text is None:
            raise Problem(404, "not_found", "no such content (never stored, expired or erased)")
        return {"hash": content_hash, "text": text}

    def need_budget(org: str | None, budget_id: str, *, unique: bool = False) -> Any:
        """The caller's budget with this id, or the missing answer when it has none (spec R8,
        R8a, O8): another organization's budget of the same id is a different budget, so the
        answer is the same 404 either way, never the id. The local inspector (no organization)
        sees every organization's budgets and, for an id several organizations hold, gets the first
        one by organization id when reading; `unique=True` (a change) refuses it as ambiguous
        (409) instead, so a change never lands on another organization's budget."""
        found = None
        if _SLUG.fullmatch(budget_id):
            if org == LOCAL_INSPECTOR:
                same = [b for b in store.list_balances(None) if b.balance.budget.id == budget_id]
                if unique and len(same) > 1:
                    raise Problem(
                        409,
                        "ambiguous_budget",
                        "several organizations have a budget with this id; "
                        "sign in as one organization to change it",
                    )
                found = same[0] if same else None
            elif is_org(org):
                found = store.get_balance(str(org), budget_id)
        if found is None:
            raise Problem(404, "not_found", "no such budget")
        return found

    @app.get("/v1/budgets")
    def budgets(request: Request, org: Org, limit: Limit = None, cursor: Cursor = None) -> Response:
        """The caller's budgets, each with its balance in its current window (T56): the first-cut
        keys plus `available`, `window`, `on_breach`, `window_start` and the stored
        `definition`. Another organization's budgets are not listed (spec R8a)."""
        now = store.now()
        if org == LOCAL_INSPECTOR:
            mine = store.list_balances(None)
        elif is_org(org):
            mine = store.list_balances(str(org))
        else:
            mine = []  # default deny: a caller with no organization sees nothing
        return paginate(
            request, [views.budget_view(b, now) for b in mine], route="GET /v1/budgets",
            old_key=None, limit=limit, cursor=cursor,
        )  # fmt: skip

    @app.get("/v1/budgets/{budget_id}")
    def budget(budget_id: str, org: Org) -> dict[str, Any]:
        """One budget, with its ledger history (defined, adjusted, breached, unblocked)."""
        stored = need_budget(org, budget_id)
        history = []
        budget_left = MAX_BUDGET_HISTORY_EVENTS
        truncated = False
        # the budget's own organization stream only (spec R8, O7); an unattributed legacy budget
        # (local inspector only) has no stream of its own, so every one is read
        owner = stored.balance.budget.org_id
        own = None if owner is None else {f"org:{owner}"}
        for stream, head in sorted(store.heads(own).items()):
            if not stream.startswith("org:"):
                continue
            if head.seq > budget_left:
                truncated = True  # more history than one request reads
                break
            budget_left -= head.seq
            for e in views.read_all(store, stream):
                if e.type.startswith("budget.") and e.payload.get("budget") == budget_id:
                    shown, hidden = redact_payload(e.type, e.actor, views.thaw(e.payload))
                    history.append(
                        {
                            "stream": stream,
                            "ledger_seq": e.seq,
                            "type": e.type,
                            "actor": e.actor,
                            "at": e.at.isoformat(),
                            "payload": shown,
                            **({"redacted": hidden} if hidden else {}),
                        }
                    )
        return {
            **views.budget_view(stored, store.now()),
            "history": history,
            "history_truncated": truncated,
        }

    @app.post("/v1/budgets/{budget_id}/unblock")
    def unblock(
        budget_id: str, body: Unblock, who: Human, cfg: Config, org: Org, idem: Idem
    ) -> Response:
        stored = need_budget(org, budget_id, unique=True)  # before anything that depends on it
        idem.require_alive()
        if body.new_limit is not None and not _AMOUNT.match(body.new_limit):
            raise Problem(
                422, "invalid_amount", "new_limit must be a plain decimal like 20 or 20.50"
            )
        if stored.balance.budget.org_id is None:  # after the amount check: the old order (R8a)
            raise Problem(
                409, "unblock_refused", "this budget has no organization (it predates ND90)"
            )
        new_limit = (
            None
            if body.new_limit is None
            else Money.of(body.new_limit, stored.balance.budget.limit.currency)
        )
        try:
            with idem.effect() as uow:
                lifted = unblock_budget_in(
                    uow,
                    budget_id,
                    actor=who,
                    reason=body.reason,
                    org_id=stored.balance.budget.org_id,
                    content=_content(),
                    new_limit=new_limit,
                )
                return idem.respond(
                    uow,
                    200,
                    {
                        "id": budget_id,
                        "blocked": lifted.blocked,
                        "limit": lifted.budget.limit.to_json(),
                    },
                )
        except ContentError as exc:
            raise _content_problem(exc, stored.balance.budget.org_id) from None
        except (ValueError, BudgetExceededError) as exc:
            raise Problem(409, "unblock_refused", str(exc).splitlines()[0][:300]) from None

    # -- reports -----------------------------------------------------------------------------

    @app.get("/v1/reports/downgrade")
    def downgrade_report(
        cfg: Config,
        min_sample: Annotated[int, Query(ge=1, le=10_000)] = downgrade.MIN_SAMPLE,
        from_: Annotated[str | None, Query(alias="from", max_length=64)] = None,
        to: Annotated[str | None, Query(max_length=64)] = None,
        as_of: Annotated[str | None, Query(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Per role: the evidence for each model next to its cost, and one recommendation for a
        cheaper model where the evidence supports it (ND63, R1.34). Read-only and scoped to the
        server's organization; it never switches a model. `min_results` is retired and ignored."""

        def instant(text: str, name: str) -> datetime:
            try:
                moment = datetime.fromisoformat(text)
                if moment.tzinfo is None:
                    raise ValueError(name)
                return moment.astimezone(UTC)
            except (ValueError, OverflowError):
                raise Problem(
                    422, "invalid_request", f"{name} must be an instant with a UTC offset"
                ) from None

        now = store.now()
        month = datetime(now.year, now.month, 1, tzinfo=UTC)
        following = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
        start = instant(from_, "from") if from_ else month
        end = instant(to, "to") if to else following
        if not start < end:
            raise Problem(422, "invalid_request", "from must be before to")
        fold = OrgLedger(store, cfg.org.id, cfg.prices).view().catalog
        return downgrade.build_report(
            store,
            cfg.org,
            min_sample=min_sample,
            start=start,
            end=end,
            as_of=instant(as_of, "as_of") if as_of else now,
            inputs=downgrade.recommendation_inputs(cfg.org, fold),
        )

    # -- agents ------------------------------------------------------------------------------

    @app.get("/v1/agents/{agent_id}/eligibility")
    def eligibility(agent_id: str, cfg: Config, who: Who) -> dict[str, Any]:
        if who.kind == "agent" and agent_id != who.agent_id:
            # before anything is read: a mismatch reveals nothing about other agents (B3)
            raise AuthProblem(403, "agent_mismatch", "this credential belongs to a different agent")
        try:
            cfg.org.agent(agent_id)
        except KeyError:
            raise Problem(404, "not_found", f"no agent '{agent_id}'") from None
        role = cfg.org.role_of(agent_id).id
        if cfg.evals is None and not effective_suites(None, cfg.presets, role):
            return {"agent": agent_id, "eligible": True, "reasons": [], "evals": False}
        result = agent_eligibility(
            store, cfg.evals, cfg.org, agent_id=agent_id, presets=cfg.presets
        )
        recent = [
            r.model_dump(mode="json", exclude={"outcomes"})
            for r in load_eval_records(store, cfg.org.id)
            if r.subject_id == agent_id
        ][-5:]
        return {
            "agent": agent_id,
            "eligible": result.eligible,
            "reasons": list(result.reasons),
            "evals": True,
            "recent_results": recent,
        }

    @app.get("/v1/verifiers/{agent_id}/calibration")
    def verifier_calibration(agent_id: str, cfg: Config, who: Who) -> dict[str, Any]:
        """Whether a verifier is calibrated for each suite its organization defines, with the
        latest result, the trend and the last five results (R2.08). Read-only: ids, counts and
        rates only; it never edits a verifier."""
        # Human-only (ND299, ND115): an agent credential never gets here (`agent_forbidden`).
        try:
            cfg.org.agent(agent_id)
        except KeyError:
            raise Problem(404, "not_found", f"no agent '{agent_id}'") from None
        suites = [s.id for s in cfg.calibration.suites] if cfg.calibration is not None else []
        try:
            found = calibration_standings(
                store,
                cfg.calibration,
                suites,
                verifier=agent_id,
                model=effective_ref(
                    cfg.org,
                    OrgLedger(store, cfg.org.id, cfg.prices).view().catalog.assignments,
                    agent_id,
                ).ref,
                org_id=cfg.org.id,
                now=store.now(),
            )
        except CalibrationLedgerError:
            raise Problem(
                503, "calibration_unavailable", "a stored calibration result is unreadable"
            ) from None
        return {
            "agent": agent_id,
            "calibrated": bool(found) and all(s.calibrated for s in found),
            "suites": [
                {
                    "suite": s.suite,
                    "calibrated": s.calibrated,
                    "reason": s.reason,
                    "latest": s.latest.model_dump(mode="json") if s.latest else None,
                    "trend": str(s.trend) if s.trend is not None else None,
                    "recent_results": [r.model_dump(mode="json") for r in s.recent_results],
                }
                for s in found
            ],
        }

    install_costs(app, store, need_config, Problem)
    install_savings(app, store, need_config, Problem)
    install_dashboards(app, store, lambda: config, caller, Problem)

    # -- routes of installed plugins (ND72, T161): added after every open route, so none can shadow
    # one (and `check_route_table` refuses a path that could); each sees only its own records ----

    for install_routes in plugins.route_installers if plugins is not None else ():
        assert plugins is not None
        install_routes(
            RouteKit(
                app=app,
                store=bound_store(store, plugins.namespaces_of(install_routes)),
                content=content,
                settings=plugin_settings or {},
                caller=caller,
                org_of=org_of,
                human=human,
                problem=Problem,
                need_run=need_run,
                is_org=is_org,
            )
        )

    # every app, whatever its host configuration (spec/auth/org-scope.md section 6, G3): the routes
    # it serves must be exactly the declared ones, and the declared tables the written matrix; an
    # unreadable or undeclared route stops it here
    check_route_table(app, extra_rules, extra_scopes)

    base_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = document_problems(document_renames(document_idempotency(base_openapi())))
            # the published text is a public contract: no task ids or decision numbers, and the
            # contract version a client may ask for by header (contract freeze, #894)
            schema["info"]["x-nuramesh-contract"] = CURRENT
            scrub_schema(schema)
        return app.openapi_schema or base_openapi()

    app.openapi = openapi  # type: ignore[method-assign]

    return app
