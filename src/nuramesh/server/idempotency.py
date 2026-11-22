"""`Idempotency-Key` for the mutating routes (R1.35, `spec/idempotency/README.md`).

`IdempotencyMiddleware` is the innermost ASGI middleware (it replaces the old blanket refusal):
authentication, authorization and the guard have answered before it. For a mutating route with
the header it matches the route table (`routes.IDEMPOTENCY_ROUTES`), checks the header, finds the
caller, checks the organization's key ring, fingerprints the request, calls `begin` and acts on
the outcome: replay the stored response, refuse (409, 422, 503) or run the route with an
`IdemContext` on `request.state`. Every store call runs on a worker thread.

`IdemContext` is what a handler uses (one code path, with or without a key):

    with idem.effect() as uow:                 # a transaction; with a key it claims the row first
        result = engine.thing_in(uow, ...)
        return idem.respond(uow, 200, view(result))   # sealed and stored in this transaction
"""

import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qsl

import anyio.to_thread
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

from nuramesh.core.auth import SecurityLogEntry
from nuramesh.core.idempotency import (
    IDEMPOTENCY_TTL,
    LEASE_SECONDS,
    LOCAL_RING_NAME,
    REPLAY_HEADER,
    Delivery,
    IdempotencyContractError,
    IdempotencyFenced,
    InProgress,
    InvalidKey,
    Mismatch,
    Mode,
    Replay,
    Scope,
    Started,
    fingerprint,
    human_subjects,
    key_hash,
    parse_key,
    storable,
    stored_headers,
    sweep_expired,
)
from nuramesh.engine.content import DESTROYED_MESSAGE, ContentService
from nuramesh.engine.idempotency import ResponseSealer, ResponseUnavailable, SealUnavailable
from nuramesh.server.authz import AUTH_FIRST, PREFLIGHT_KEY, Caller
from nuramesh.server.error_statuses import ERROR_503_CODES
from nuramesh.server.problems import PROBLEM
from nuramesh.server.problems import problem_response as render_problem
from nuramesh.server.routes import IDEMPOTENCY_ROUTES, MUTATING
from nuramesh.server.scope import LOCAL_INSPECTOR
from nuramesh.stores.base import Store, StoreError, UnitOfWork

_LOG = logging.getLogger(__name__)

MAX_BODY: Final = 1_000_000
HEADER: Final = b"idempotency-key"
STATE_KEY: Final = "idem"
SECURITY_EVENT: Final = "idempotency.key_reused"
"""The security-log type. The spec's `idempotency_key_reused` has no dot, which the log's type
pattern (`core.auth.SecurityLogEntry`) requires; the dotted form is the convention here."""

UNSUPPORTED_NO_ROW: Final = "Idempotency-Key not supported on this route yet"
UNSUPPORTED_READONLY: Final = (
    "This route changes nothing, so it takes no Idempotency-Key. Repeat it freely."
)
UNSUPPORTED_SECRET: Final = (
    "This route cannot be repeated safely with an Idempotency-Key. "
    "Send the request without the header."
)
UNAVAILABLE_RING: Final = (
    "Idempotency keys are unavailable on this server: the content key ring is missing or cannot be "
    "read. The request was not run. Send it again later, or without an Idempotency-Key header, and "
    "ask the operator to install the content extra and configure the key ring."
)
UNAVAILABLE_STORE: Final = (
    "Idempotency keys are unavailable on this server: the idempotency store cannot be reached. The "
    "request was not run. Send it again later, or without an Idempotency-Key header."
)

SWEEP_EVERY_SECONDS: Final = 3600.0
"""How often the server's own timer sweeps expired rows, whatever login activity there is."""

UNAVAILABLE_AFTER_RUN: Final = (
    "Idempotency keys are unavailable on this server: the answer to this request could not be "
    "stored. The request may have been carried out. Check the current state before you send it "
    "again; do not assume it did not run."
)
"""The at-least-once 503 (ND139): the effect already ran, so it never says "not run"."""

REVOKE_ROUTES: Final = (
    ("POST", "/v1/auth/tokens/{token_id}/revoke"),
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/revoke"),
    ("POST", "/v1/agents/{agent_id}/credentials/revoke-all"),
)
"""The revoke routes (ND163): besides a destroyed key they also run unkeyed when the key ring
cannot be read, and a shred that lands after the effect never rolls the revoke back."""

ALWAYS_WORKS: Final = frozenset(
    {
        ("POST", "/v1/runs/{run_id}/stop"),
        ("POST", "/v1/org/halt"),
        ("POST", "/v1/org/roles/{role}/pause"),
        # ND163: revoking only reduces access, so it never waits on a key
        *REVOKE_ROUTES,
        # ND210: disabling a person also only reduces access
        ("POST", "/v1/auth/users/{user_id}/disable"),
    }
)
"""Routes that run as if no key was sent on an organization whose key was destroyed (R1.37)."""


class IdemRefusal(Exception):
    """An idempotency answer (a problem+json) raised from inside a handler's `effect`."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        headers: Mapping[str, str] | None = None,
        after: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
        self.headers = dict(headers or {})
        self.after = after
        """Work to run once the problem has been sent (the security-log write of a 422)."""


def subjects_of(
    actor: str, body: bytes, headers: Mapping[str, str], extra: str | None = None
) -> frozenset[str]:
    """Every person a stored answer names (ND148): the ids in the body and the headers, computed
    from what is stored so no route can leave one out, plus the actor and `extra` if a person."""
    found = human_subjects(body.decode("utf-8", errors="replace"), *headers.values())
    people = {actor, extra} if extra else {actor}
    return found | {p for p in people if p.startswith("human:")}


def problem(
    status: int, code: str, detail: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """A problem+json response like every other error of the API."""
    return render_problem(status, code, detail, headers=headers)


def _unavailable(detail: str) -> IdemRefusal:
    return IdemRefusal(503, "idempotency_unavailable", detail, {"Retry-After": "5"})


def _in_progress() -> IdemRefusal:
    return IdemRefusal(
        409,
        "idempotency_in_progress",
        "another request with this Idempotency-Key is still running; try again in a moment",
        {"Retry-After": "1"},
    )


@dataclass
class IdemService:
    """What the middleware and the contexts share: the store, the sealer (None without a key
    ring) and the content service (for the tombstone log row)."""

    store: Store
    content: ContentService | None

    def __post_init__(self) -> None:
        self.sealer = None if self.content is None else ResponseSealer(self.content)

    def now(self) -> datetime:
        return self.store.now()

    def sweep(self) -> int:
        """Delete expired rows: batches of 1,000 while a batch is full, at most
        `SWEEP_MAX_BATCHES` batches a call (ND148). A failure is logged, never raised."""
        try:
            return sweep_expired(
                lambda limit: self.store.purge_idempotency(self.now(), limit=limit)
            )
        except Exception:
            _LOG.warning("the idempotency sweep failed", exc_info=True)
            return 0

    async def sweep_forever(self) -> None:
        """The server's own timer: sweep now, then every `SWEEP_EVERY_SECONDS`."""
        while True:
            await anyio.to_thread.run_sync(self.sweep)
            await anyio.sleep(SWEEP_EVERY_SECONDS)


class IdemContext:
    """The handler's view of the key (a no-op object when the request has none)."""

    def __init__(
        self,
        service: IdemService,
        *,
        scope: Scope | None = None,
        key: str | None = None,
        fingerprint_: str | None = None,
        fence: str | None = None,
        ring_org: str | None = None,
        tombstoned: bool = False,
        unsealed_ok: bool = False,
    ) -> None:
        self.service = service
        self.scope, self.key_hash, self.fingerprint, self.fence = scope, key, fingerprint_, fence
        self.ring_org, self.tombstoned = ring_org, tombstoned
        self.responded = False
        self.committed = False
        self.unsealed_ok = unsealed_ok  # a revoke whose sealing fails still stands (ND163)
        self.unsealed = False

    @property
    def keyed(self) -> bool:
        """Whether this request carries a key that is being honored."""
        return self.fence is not None

    def require_alive(self) -> None:
        """Raise 409 `org_key_destroyed` for a noted tombstone (call it right after the path
        lookup and before any business refusal). Writes one coalesced security-log row."""
        if not self.tombstoned:
            return
        content, org = self.service.content, self.ring_org
        if content is not None and org is not None:
            content.record_security_count(org, "content.capture_refused", "tombstone")
        raise IdemRefusal(409, "org_key_destroyed", DESTROYED_MESSAGE)

    @contextmanager
    def effect(self) -> Iterator[UnitOfWork]:
        """A transaction for the route's effect. With a key it first claims the row (a stale
        holder is `IdemRefusal` 409 before any write) and must end in `respond`."""
        with self.service.store.transaction() as uow:
            if self.keyed:
                assert self.scope is not None and self.key_hash is not None
                assert self.fence is not None
                try:
                    uow.idempotency_claim(self.scope, self.key_hash, self.fence)
                except IdempotencyFenced:
                    raise _in_progress() from None
            yield uow
            if self.keyed and not self.responded:
                raise IdempotencyContractError("a keyed effect ended without respond")
        self.committed = not self.unsealed  # unsealed: the middleware releases the claimed row

    def respond(
        self,
        uow: UnitOfWork,
        status: int,
        payload: Any,
        *,
        subject: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """The response, serialized once. With a key and a 2xx status it is sealed and stored in
        `uow` (the effect's transaction)."""
        response = JSONResponse(jsonable_encoder(payload), status_code=status, headers=headers)
        if not self.keyed:
            return response
        assert self.scope is not None and self.key_hash is not None
        assert self.fence is not None and self.fingerprint is not None
        assert self.ring_org is not None
        sent = dict(response.headers.items())
        if not storable(status, sent, bytes(response.body)):
            raise IdempotencyContractError("a keyed response must be a storable 2xx")
        try:
            sealed = self._sealer().seal(
                uow,
                self.ring_org,
                self.scope,
                self.key_hash,
                self.fingerprint,
                status,
                sent,
                bytes(response.body),
            )
        except SealUnavailable:
            if self.unsealed_ok:  # a shred landed after the check: the revoke stands, unstored
                _LOG.warning("idempotency: revoke answered unsealed, the key ring was destroyed")
                self.responded = self.unsealed = True
                return response
            raise _unavailable(UNAVAILABLE_RING) from None
        except StoreError:
            _LOG.warning("idempotency seal failed", exc_info=True)
            raise _unavailable(UNAVAILABLE_STORE) from None
        try:
            uow.idempotency_complete(
                self.scope,
                self.key_hash,
                self.fence,
                status=status,
                sealed=sealed,
                now=self.service.now(),
                ttl=IDEMPOTENCY_TTL,
                subjects=subjects_of(self.scope.actor, bytes(response.body), sent, subject),
            )
        except IdempotencyFenced:
            raise _in_progress() from None
        except StoreError:  # D30: the effect rolls back with the answer
            _LOG.warning("idempotency complete failed", exc_info=True)
            raise _unavailable(UNAVAILABLE_STORE) from None
        self.responded = True
        return response

    def _sealer(self) -> ResponseSealer:
        sealer = self.service.sealer
        if sealer is None:
            raise _unavailable(UNAVAILABLE_RING)
        return sealer


def idem_of(request: Request, service: IdemService) -> IdemContext:
    """The request's context: the middleware's, or a no-op one when no key is honored."""
    found = getattr(request.state, STATE_KEY, None)
    return found if isinstance(found, IdemContext) else IdemContext(service)


def require_org_key_alive(request: Request) -> None:
    """Refuse 409 `org_key_destroyed` when the middleware noted a destroyed organization key."""
    found = getattr(request.state, STATE_KEY, None)
    if isinstance(found, IdemContext):
        found.require_alive()


def ring_name(caller: Caller) -> str:
    """The key-ring name of the caller's organization (a fixed name without an organization)."""
    return LOCAL_RING_NAME if caller.org_id == LOCAL_INSPECTOR else str(caller.org_id)


Message = MutableMapping[str, Any]
Send = Callable[[Message], Awaitable[None]]
Receive = Callable[[], Awaitable[Message]]


class IdempotencyMiddleware:
    """ASGI, innermost. See the module docstring and spec section 2."""

    def __init__(self, app: Any, routes: Callable[[], Any], service: IdemService) -> None:
        self.app, self.routes, self.service = app, routes, service

    def _match(self, scope: Any) -> str | None:
        for route in self.routes():
            match, _child = route.matches(scope)
            if match == Match.FULL:
                path = getattr(route, "path", None)
                return path if isinstance(path, str) else None
        return None

    async def __call__(self, scope: Any, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            await self.app(scope, receive, send)
            return
        lines = [v for k, v in scope["headers"] if k == HEADER]
        template = self._match(scope) if lines else None
        if template is None:
            await self.app(scope, receive, send)
            return
        try:
            await self._keyed(scope, receive, send, template, lines)
        except IdemRefusal as exc:
            try:
                await problem(exc.status, exc.code, exc.detail, exc.headers)(scope, receive, send)
            finally:  # the log row is written once, even if the client left before the 422
                if exc.after is not None:  # the client has its answer; the write never delays it
                    with anyio.CancelScope(shield=True):
                        await anyio.to_thread.run_sync(exc.after)

    async def _keyed(
        self, scope: Any, receive: Receive, send: Send, template: str, lines: list[bytes]
    ) -> None:
        method = scope["method"]
        mode = IDEMPOTENCY_ROUTES.get((method, template))
        if mode is not None and mode.mode is Mode.IGNORED:
            await self.app(scope, receive, send)  # as if no key was sent
            return
        if mode is None or mode.mode is not Mode.HONORED:
            detail = {
                None: UNSUPPORTED_NO_ROW,
                Mode.EXCLUDED_READONLY: UNSUPPORTED_READONLY,
                Mode.EXCLUDED_SECRET: UNSUPPORTED_SECRET,
            }[None if mode is None else mode.mode]
            raise IdemRefusal(400, "idempotency_key_unsupported", detail)
        try:
            key = parse_key([v.decode("latin-1") for v in lines])
        except InvalidKey as exc:
            raise IdemRefusal(400, "idempotency_key_invalid", str(exc)) from None
        state = scope.setdefault("state", {})
        caller = scope.get(PREFLIGHT_KEY) if (method, template) in AUTH_FIRST else None
        caller = caller or state.get("caller")
        if not isinstance(caller, Caller) or not caller.actor or not caller.org_id:
            raise _unavailable(UNAVAILABLE_STORE)  # a programming error: fail closed
        ring_org = ring_name(caller)
        revoke = (method, template) in REVOKE_ROUTES
        sealer = self.service.sealer
        if sealer is None:
            if revoke:  # ND163: revoking never waits on the ring
                _LOG.warning("idempotency: no sealer, revoke runs unkeyed")
                await self.app(scope, receive, send)
                return
            raise _unavailable(UNAVAILABLE_RING)
        try:
            await anyio.to_thread.run_sync(sealer.check_ring, ring_org)
            dead = await anyio.to_thread.run_sync(sealer.tombstoned, ring_org)
        except Exception as exc:
            if revoke:
                _LOG.warning("idempotency: key ring unreadable, revoke runs unkeyed", exc_info=True)
                await self.app(scope, receive, send)  # as if no key was sent
                return
            if not isinstance(exc, SealUnavailable):
                _LOG.warning("the key ring could not be read", exc_info=True)
            raise _unavailable(UNAVAILABLE_RING) from None
        service = self.service
        if dead:
            if (method, template) in ALWAYS_WORKS:
                _LOG.warning("idempotency: organization key destroyed, route runs unkeyed")
                await self.app(scope, receive, send)  # as if no key was sent
                return
            state[STATE_KEY] = IdemContext(service, ring_org=ring_org, tombstoned=True)
            await self.app(scope, receive, send)
            return
        body = await self._read_body(scope, receive, send)
        if body is None:
            return
        headers = dict(scope["headers"])
        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        fp = fingerprint(
            method,
            scope["path"],
            query,
            headers.get(b"content-type", b"").decode("latin-1") or None,
            body,
        )
        idem_scope = Scope(str(caller.org_id), caller.actor, method, template)
        hashed = key_hash(key)
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: service.store.idempotency_begin(
                    idem_scope,
                    hashed,
                    fp,
                    now=service.now(),
                    lease=LEASE_SECONDS,
                    ttl=IDEMPOTENCY_TTL,
                )
            )
        except Exception:
            _LOG.warning("idempotency begin failed", exc_info=True)
            raise _unavailable(UNAVAILABLE_STORE) from None
        if isinstance(outcome, Mismatch):
            raise IdemRefusal(
                422,
                "idempotency_key_reused",
                "this Idempotency-Key was already used with a different request",
                after=lambda: self._log_mismatch(idem_scope, caller),
            )
        if isinstance(outcome, InProgress):
            raise _in_progress()
        if isinstance(outcome, Replay):
            await self._replay(scope, receive, send, outcome, idem_scope, hashed, fp, ring_org)
            return
        assert isinstance(outcome, Started)
        ctx = IdemContext(
            service,
            scope=idem_scope,
            key=hashed,
            fingerprint_=fp,
            fence=outcome.fence,
            ring_org=ring_org,
            unsealed_ok=revoke,
        )
        state[STATE_KEY] = ctx
        delivery = mode.delivery
        sent = [body]

        async def replay_receive() -> Message:
            if sent:
                return {"type": "http.request", "body": sent.pop(), "more_body": False}
            return await receive()

        buffered: list[Message] = []

        async def capture(message: Message) -> None:
            buffered.append(message)

        try:
            if delivery is Delivery.AT_LEAST_ONCE:
                await self.app(scope, replay_receive, capture)
            else:
                await self.app(scope, replay_receive, send)
        except BaseException:
            await self._release(ctx)
            raise
        if delivery is Delivery.AT_LEAST_ONCE:
            outcome_ = await anyio.to_thread.run_sync(self._complete, ctx, buffered)
            if outcome_ == "unavailable":  # a sealing failure (spec 6.3): nothing is delivered
                await self._release(ctx)
                raise _unavailable(UNAVAILABLE_AFTER_RUN)  # ND139: the effect already ran
            if outcome_ == "release":
                await self._release(ctx)
            # "kept" (X04): the real response, the row stays in_progress until its lease lapses
            for message in buffered:
                await send(message)
        elif not ctx.committed:
            await self._release(ctx)

    async def _release(self, ctx: IdemContext) -> None:
        assert ctx.scope is not None and ctx.key_hash is not None and ctx.fence is not None
        scope, hashed, fence = ctx.scope, ctx.key_hash, ctx.fence
        try:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    self.service.store.idempotency_release, scope, hashed, fence
                )
        except Exception:
            _LOG.warning("idempotency release failed", exc_info=True)

    def _complete(self, ctx: IdemContext, messages: list[Message]) -> str:
        """Seal and store a captured 2xx of an at-least-once route. Returns "stored";
        "release" (not storable or fenced: deliver, release); "unavailable" (sealing failed:
        503, release, spec 6.3); "kept" (the row write failed: deliver, row stays, X04)."""
        start = next((m for m in messages if m["type"] == "http.response.start"), None)
        if start is None:
            return "release"
        status = int(start["status"])
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        if not storable(status, headers, body) or any(
            k.decode("latin-1").lower() == "set-cookie" for k, _ in start["headers"]
        ):
            return "release"
        assert ctx.scope is not None and ctx.key_hash is not None and ctx.fence is not None
        assert ctx.fingerprint is not None and ctx.ring_org is not None
        stage = "unavailable"
        try:
            with self.service.store.transaction() as uow:
                sealed = ctx._sealer().seal(
                    uow,
                    ctx.ring_org,
                    ctx.scope,
                    ctx.key_hash,
                    ctx.fingerprint,
                    status,
                    headers,
                    body,
                )
                stage = "kept"
                uow.idempotency_complete(
                    ctx.scope,
                    ctx.key_hash,
                    ctx.fence,
                    status=status,
                    sealed=sealed,
                    now=self.service.now(),
                    ttl=IDEMPOTENCY_TTL,
                    subjects=subjects_of(ctx.scope.actor, body, headers),
                )
        except IdempotencyFenced:
            return "release"
        except Exception:
            _LOG.warning("idempotency complete failed", exc_info=True)
            return stage
        return "stored"

    def _log_mismatch(self, scope: Scope, caller: Caller) -> None:
        """One security-log row for a reused key; never the key, hash, fingerprint or body."""
        try:
            subject = self._subject(scope, caller)
            entry = SecurityLogEntry.new(
                scope.org_id,
                self.service.now(),
                SECURITY_EVENT,
                actor=scope.actor,
                subject=subject,
                details={"via": scope.method, "scope": scope.route},
            )
            with self.service.store.transaction() as uow:
                uow.append_security_log(entry)
        except Exception:
            _LOG.warning("could not write the idempotency security-log row", exc_info=True)

    def _subject(self, scope: Scope, caller: Caller) -> str | None:
        if scope.actor.startswith("human:"):
            return scope.actor
        if caller.token_id is not None:
            token = self.service.store.get_api_token(scope.org_id, caller.token_id)
            if token is not None and token.created_by.startswith("human:"):
                return token.created_by
        return None

    async def _replay(
        self,
        scope: Any,
        receive: Receive,
        send: Send,
        outcome: Replay,
        idem_scope: Scope,
        hashed: str,
        fp: str,
        ring_org: str,
    ) -> None:
        sealer = self.service.sealer
        assert sealer is not None
        try:
            headers, body = await anyio.to_thread.run_sync(
                lambda: sealer.open(
                    ring_org, idem_scope, hashed, fp, outcome.status, outcome.sealed
                )
            )
        except ResponseUnavailable:
            raise IdemRefusal(
                409,
                "idempotency_response_unavailable",
                "the original request was carried out but its response can no longer be shown; "
                "check the current state and use a new Idempotency-Key",
            ) from None
        except SealUnavailable:
            raise _unavailable(UNAVAILABLE_RING) from None
        out = {k: v for k, v in stored_headers(headers).items()}
        out[REPLAY_HEADER] = "true"
        content_type = out.pop("content-type", "application/json")
        response = Response(
            content=body, status_code=outcome.status, headers=out, media_type=content_type
        )
        await response(scope, receive, send)

    async def _read_body(self, scope: Any, receive: Receive, send: Send) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return None  # the client went away
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY:
                raise IdemRefusal(413, "too_large", "the request body is too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)


def sweep_due(last: float | None, now: float, interval: int) -> bool:
    """Whether the hourly sweep runs (`now - last >= interval`; the first one runs at once)."""
    return last is None or now - last >= interval


def monotonic() -> float:
    """The clock the sweep uses (a function so tests can replace it)."""
    return time.monotonic()


__all__ = [
    "ALWAYS_WORKS",
    "IdemContext",
    "IdemRefusal",
    "IdemService",
    "IdempotencyMiddleware",
    "idem_of",
    "require_org_key_alive",
]


_HEADER_PARAMETER: Final = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": False,
    "description": (
        "A random 16 to 128 character key (letters, digits, '.', '_', '-') made once per user "
        "intent and reused only to retry that request. A repeat within 24 hours returns the "
        "first answer with Idempotent-Replayed: true."
    ),
    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
}
_PROBLEM_RESPONSES: Final = {
    "400": "idempotency_key_invalid",
    "409": "idempotency_in_progress, idempotency_response_unavailable or org_key_destroyed",
    "422": "idempotency_key_reused",
    "503": "idempotency_unavailable",
}
_REVOKE_503: Final = (
    "idempotency_unavailable (the idempotency store is unreachable; an unreadable key ring "
    "runs the revoke as if no key was sent, ND163)"
)
_AT_LEAST_503: Final = (
    "idempotency_unavailable (no key ring, store unreachable, or the answer could not be stored: "
    "the request may have been carried out)"
)


def _idempotency_statuses(method: str, template: str, delivery: Delivery | None) -> dict[str, str]:
    """The idempotency answers of one honored route, from the documented table (#1121 option B):
    400, 409, 422 and 503 always. A revoke route (ND163) runs unkeyed only when the key RING is
    unreadable; an unreachable idempotency store is still 503. An at-least-once route's 503
    also covers an unstorable answer."""
    out = dict(_PROBLEM_RESPONSES)
    if (method, template) in REVOKE_ROUTES:
        out["503"] = _REVOKE_503
    elif delivery is Delivery.AT_LEAST_ONCE:
        out["503"] = _AT_LEAST_503
    return out


def document_idempotency(schema: dict[str, Any]) -> dict[str, Any]:
    """Add the header parameter and the idempotency answers to every honored operation (the
    document is generated from `IDEMPOTENCY_ROUTES`, spec section 13). A status the route
    already answers keeps its own text and gains the idempotency code."""
    for (method, template), mode in IDEMPOTENCY_ROUTES.items():
        if mode.mode not in (Mode.HONORED, Mode.EXCLUDED_SECRET):
            continue
        operation = schema.get("paths", {}).get(template, {}).get(method.lower())
        if operation is None:
            continue
        responses = operation.setdefault("responses", {})
        own_503 = ERROR_503_CODES.get(f"{method} {template}", [])
        if own_503:  # the codes the route raises itself, from the source walker
            _merge_response(responses, "503", ", ".join(own_503))
        if mode.mode is Mode.EXCLUDED_SECRET:  # a sent key is refused (the secret is in the answer)
            _merge_response(responses, "400", "idempotency_key_unsupported")
            continue
        params = operation.setdefault("parameters", [])
        if not any(p.get("name") == "Idempotency-Key" for p in params):
            params.append(dict(_HEADER_PARAMETER))
        for status, text in _idempotency_statuses(method, template, mode.delivery).items():
            _merge_response(responses, status, text)
    return schema


def _merge_response(responses: dict[str, Any], status: str, text: str) -> None:
    """Add `status` with `text`, or append `text` to the route's own description."""
    existing = responses.get(status)
    if existing is None:
        responses[status] = {
            "description": text,
            "content": {PROBLEM: {"schema": {"type": "object"}}},
        }
        return
    own = existing.get("description") or ""
    if text not in own:
        existing["description"] = f"{own}; {text}" if own else text
