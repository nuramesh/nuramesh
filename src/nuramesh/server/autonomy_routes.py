"""Earned autonomy and agent scorecards over HTTP (E13 + E14, `spec/autonomy/README.md`).

- Reads: `GET /v1/scorecards` (read), `GET /v1/autonomy/grants` (read),
  `GET /v1/autonomy/proposals` (decide: owner and approver, a viewer is 403).
- Human decisions: `POST /v1/autonomy/grants` (admin: an owner), `POST .../revoke` and
  `PUT /v1/autonomy/ceilings/{role}/{kind}` (decide; raising a ceiling is the owner's, checked in
  the engine), `PUT /v1/autonomy/settings/{name}` (admin) and
  `PUT /v1/autonomy/model-change-floor/{role}` (admin, MC6).

The engine (`nuramesh.engine.autonomy`) holds every rule; this module only turns requests into
calls and `AutonomyError` / `ScorecardError` into problem responses. The organization is the
caller's; a token is never a person (`human_required`). A refusal appends nothing.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.core.auth import Permission
from nuramesh.core.autonomy import DEFAULT_EXPIRY_DAYS
from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.model_epoch import FLOOR_KINDS
from nuramesh.core.scorecard import DEFAULT_MIN_RESULTS, ScorecardError
from nuramesh.engine import autonomy
from nuramesh.engine.autonomy import AutonomyError
from nuramesh.engine.content import ContentService
from nuramesh.server.auth import Body
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.pagination import Cursor, Limit, paginate
from nuramesh.server.problems import problem_response as render_problem
from nuramesh.server.sessions import AuthProblem
from nuramesh.stores.base import Store

NO_STORE = {"Cache-Control": "no-store"}
REASON_MAX = 5_000
"""A bound on the request text only; the engine refuses a blank reason."""


class SubjectBody(Body):
    kind: Literal["agent", "role"]
    id: str = Field(min_length=1, max_length=200)


class GrantBody(Body):
    """A grant request: what the person read (`as_of`, `ledger_position`, `evidence_hash`)."""

    subject: SubjectBody
    rule_key: int = Field(ge=0, le=10_000)
    as_of: datetime
    ledger_position: int = Field(ge=0)
    evidence_hash: str = Field(min_length=1, max_length=200)
    expires_in_days: int = Field(default=DEFAULT_EXPIRY_DAYS, ge=-1_000, le=1_000)
    reason: str = Field(max_length=REASON_MAX)


class ReasonBody(Body):
    reason: str = Field(max_length=REASON_MAX)


class CeilingBody(ReasonBody):
    value: str = Field(max_length=20)


class FloorBody(ReasonBody):
    kinds: list[str] = Field(max_length=20)


class SettingBody(ReasonBody):
    value: int | str = Field(union_mode="left_to_right")


def _problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return render_problem(status, code, detail, headers=NO_STORE, **extra)


def _aware(value: datetime | None, default: datetime) -> datetime:
    if value is None:
        return default
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def install_autonomy(
    app: FastAPI,
    config: NuraMeshConfig | None,
    store: Store,
    content: ContentService | None,
    caller: Callable[..., Caller],
    now: Callable[[], datetime],
    idem_dep: Callable[..., IdemContext],
) -> None:
    """Add the autonomy routes (each has a row in `authz.ROUTE_RULES` and `routes.ROUTE_SCOPES`)."""

    @app.exception_handler(AutonomyError)
    async def _autonomy_error(_: Request, exc: AutonomyError) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(ScorecardError)
    async def _scorecard_error(_: Request, exc: ScorecardError) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.detail)

    def need() -> NuraMeshConfig:
        if config is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return config

    def need_content() -> ContentService:
        if content is None:
            raise AuthProblem(409, "no_content_store", "reasons need the content store")
        return content

    Cfg = Annotated[NuraMeshConfig, Depends(need)]
    Content = Annotated[ContentService, Depends(need_content)]
    Who = Annotated[Caller, Depends(caller)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    def role_of(who: Caller) -> str:
        """The organization role the caller acts as: from the permissions of a session (the
        engine checks again that the actor is a person)."""
        if Permission.ADMIN in who.permissions:
            return "owner"
        if Permission.DECIDE in who.permissions:
            return "approver"
        return "viewer"

    def actor_of(who: Caller) -> str:
        if who.actor is None:
            raise AuthProblem(403, "authentication_required", "this needs a signed-in person")
        return who.actor

    # -- scorecards ----------------------------------------------------------------------------

    @app.get("/v1/scorecards")
    def get_scorecards(
        cfg: Cfg,
        kind: Annotated[Literal["agent", "role"], Query()],
        id: Annotated[str | None, Query(max_length=200)] = None,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: Annotated[datetime | None, Query()] = None,
        as_of: Annotated[datetime | None, Query()] = None,
        min_results: Annotated[int, Query()] = DEFAULT_MIN_RESULTS,
    ) -> Response:
        when = _aware(as_of, now())
        start = None if from_ is None else _aware(from_, when)
        end = None if to is None else _aware(to, when)
        known = [a.id for a in cfg.org.agents] if kind == "agent" else [r.id for r in cfg.org.roles]
        if id is not None and id not in known:
            return _problem(404, "not_found", f"{kind} '{id}' is not in the organization")

        def one(subject: str) -> dict[str, Any]:
            return autonomy.read_scorecard(
                store,
                cfg.org.id,
                kind=kind,
                subject=subject,
                as_of=when,
                start=start,
                end=end,
                min_results=min_results,
                evals=cfg.evals,
            )

        if id is not None:
            return JSONResponse(one(id), headers=NO_STORE)
        return JSONResponse({"items": [one(s) for s in known]}, headers=NO_STORE)

    # -- proposals and grants (reads) ------------------------------------------------------------

    @app.get("/v1/autonomy/proposals")
    def get_proposals(
        cfg: Cfg,
        role: Annotated[str, Query(max_length=200)],
        agent: Annotated[str | None, Query(max_length=200)] = None,
        as_of: Annotated[datetime | None, Query()] = None,
    ) -> Response:
        when = _aware(as_of, now())
        if role not in [r.id for r in cfg.org.roles]:
            return _problem(404, "not_found", f"role '{role}' is not in the organization")
        if agent is not None and agent not in [a.id for a in cfg.org.agents]:
            return _problem(404, "not_found", f"agent '{agent}' is not in the organization")
        items: list[dict[str, Any]] = []
        position = 0
        for kind, subject in [("role", role), *([("agent", agent)] if agent else [])]:
            found, position = autonomy.read_proposals(
                store, cfg.org.id, kind=kind, subject=subject, as_of=when, evals=cfg.evals
            )
            items += [p.to_json() for p in found]
        return JSONResponse(
            {"items": items, "ledger_position": position, "as_of": when.isoformat()},
            headers=NO_STORE,
        )

    @app.get("/v1/autonomy/grants")
    def get_grants(
        request: Request,
        cfg: Cfg,
        status: Annotated[Literal["active", "all"], Query()] = "active",
        as_of: Annotated[datetime | None, Query()] = None,
        limit: Limit = None,
        cursor: Cursor = None,
    ) -> Response:
        when = _aware(as_of, now())
        items = autonomy.list_grants(
            store, cfg.org.id, as_of=when, evals=cfg.evals, active_only=status == "active"
        )
        return paginate(
            request, items, route="GET /v1/autonomy/grants", old_key="items", limit=limit,
            cursor=cursor, headers=NO_STORE, key=lambda g: str(g["grant_id"]),
            extra={"as_of": when.isoformat()}, renamed=False,
        )  # fmt: skip

    # -- human decisions -----------------------------------------------------------------------

    @app.post("/v1/autonomy/grants", status_code=201)
    def post_grant(body: GrantBody, who: Who, cfg: Cfg, service: Content, idem: Idem) -> Response:
        actor = actor_of(who)
        with idem.effect() as uow:
            result = autonomy.grant_in(
                uow,
                cfg.org.id,
                actor=actor,
                actor_role=role_of(who),
                kind=body.subject.kind,
                subject=body.subject.id,
                rule_key=body.rule_key,
                as_of=_aware(body.as_of, now()),
                ledger_position=body.ledger_position,
                evidence_hash=body.evidence_hash,
                expires_in_days=body.expires_in_days,
                reason=body.reason,
                evals=cfg.evals,
                content=service,
                grant_id=uuid.uuid4().hex,
            )
            return idem.respond(uow, 201, result, subject=actor, headers=NO_STORE)

    @app.post("/v1/autonomy/grants/{grant_id}/revoke")
    def post_revoke(
        grant_id: str, body: ReasonBody, who: Who, cfg: Cfg, service: Content, idem: Idem
    ) -> Response:
        actor = actor_of(who)
        with idem.effect() as uow:
            result = autonomy.revoke_in(
                uow,
                cfg.org.id,
                grant_id,
                actor=actor,
                actor_role=role_of(who),
                reason=body.reason,
                evals=cfg.evals,
                content=service,
            )
            return idem.respond(uow, 200, result, subject=actor, headers=NO_STORE)

    @app.put("/v1/autonomy/ceilings/{role}/{kind}")
    def put_ceiling(
        role: str, kind: str, body: CeilingBody, who: Who, cfg: Cfg, service: Content, idem: Idem
    ) -> Response:
        if role not in [r.id for r in cfg.org.roles]:
            return _problem(404, "not_found", f"role '{role}' is not in the organization")
        actor = actor_of(who)
        with idem.effect() as uow:
            result = autonomy.set_ceiling_in(
                uow,
                cfg.org.id,
                role,
                kind,
                body.value,
                actor=actor,
                actor_role=role_of(who),
                reason=body.reason,
                content=service,
            )
            return idem.respond(uow, 200, result, subject=actor, headers=NO_STORE)

    @app.put("/v1/autonomy/settings/{name}")
    def put_setting(
        name: str, body: SettingBody, who: Who, cfg: Cfg, service: Content, idem: Idem
    ) -> Response:
        value: int | Decimal
        try:
            value = body.value if isinstance(body.value, int) else Decimal(body.value)
        except InvalidOperation:
            return _problem(400, "invalid_request", "value is a whole number or a decimal text")
        actor = actor_of(who)
        with idem.effect() as uow:
            result = autonomy.set_setting_in(
                uow,
                cfg.org.id,
                name,
                value,
                actor=actor,
                actor_role=role_of(who),
                reason=body.reason,
                content=service,
            )
            return idem.respond(uow, 200, result, subject=actor, headers=NO_STORE)

    @app.put("/v1/autonomy/model-change-floor/{role}")
    def put_floor(
        role: str, body: FloorBody, who: Who, cfg: Cfg, service: Content, idem: Idem
    ) -> Response:
        """The one human-owner write path of the model-change floor."""
        if role not in [r.id for r in cfg.org.roles]:
            return _problem(404, "not_found", f"role '{role}' is not in the organization")
        if not all(k in FLOOR_KINDS for k in body.kinds):
            # the engine refuses the same way; this early answer is the one the error table sees
            return _problem(400, "not_floor_kind", "kinds must be floor kinds")
        actor = actor_of(who)
        with idem.effect() as uow:
            result = autonomy.set_floor_in(
                uow,
                cfg.org.id,
                role,
                body.kinds,
                actor=actor,
                actor_role=role_of(who),
                reason=body.reason,
                content=service,
            )
            return idem.respond(uow, 200, result, subject=actor, headers=NO_STORE)
