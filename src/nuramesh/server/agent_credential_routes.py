"""The seven owner routes for agent credentials (E2, spec section 7).

An owner in a human session issues, lists, rotates and revokes the credentials of an agent of the
team configuration (rule `ADMIN`; an agent, a token and the CLI can never do it). The secret is in
the answer that creates it only (`shown_once`, `Cache-Control: no-store`); a view never holds the
secret or its hash. Bodies forbid unknown fields, so no request field can name an actor, a team or
a fingerprint.
"""

from collections.abc import Callable
from datetime import UTC
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from nuramesh.core.auth import IssuedAgentCredential
from nuramesh.server.agent_credentials import AgentCredentialService
from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.pagination import Cursor, Limit, paginate
from nuramesh.server.sessions import AuthProblem

_NO_STORE = {"Cache-Control": "no-store"}
_BOOL = Literal["true", "false"]


class IssueBody(Body):
    """A new credential: its scopes (`read`, `run`; duplicates are merged) and lifetime in days."""

    scopes: list[Any]
    expires_in_days: Any = None


class RotateBody(Body):
    """Rotate: the new lifetime in days and the overlap in seconds. Scopes cannot change."""

    expires_in_days: Any = None
    grace_seconds: Any = None


def install_agent_credentials(
    app: FastAPI,
    service: AgentCredentialService | None,
    caller: Callable[..., Caller],
    idem_dep: Callable[..., IdemContext],
) -> None:
    """Add the seven routes of spec section 7 and the agent list."""

    def need() -> AgentCredentialService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(owner)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    def created(svc: AgentCredentialService, issued: IssuedAgentCredential) -> dict[str, Any]:
        return {
            **svc.view(issued.record),
            "credential": issued.credential,
            "shown_once": True,
        }

    @app.post("/v1/agents/{agent_id}/credentials", status_code=201)
    def issue(agent_id: str, body: IssueBody, by: By) -> Response:
        svc = need()
        issued = svc.issue(agent_id, body.scopes, body.expires_in_days, by=by)
        return JSONResponse(created(svc, issued), status_code=201, headers=_NO_STORE)

    @app.get("/v1/agents/{agent_id}/credentials")
    def list_for_agent(
        agent_id: str, by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        svc = need()
        svc.check_agent(agent_id)
        return paginate(
            request, svc.list_views(agent_id), route="GET /v1/agents/{agent_id}/credentials",
            old_key="credentials", limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.get("/v1/agents")
    def list_agents(
        by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        return paginate(
            request, need().agent_views(), route="GET /v1/agents", old_key="agents",
            limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.get("/v1/agent-credentials")
    def list_all(
        by: By,
        request: Request,
        limit: Limit = None,
        cursor: Cursor = None,
        agent: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")] = None,
        active: _BOOL | None = None,
        issuer_erased: _BOOL | None = None,
    ) -> Response:
        views = need().list_views(
            agent,
            active=None if active is None else active == "true",
            issuer_erased=None if issuer_erased is None else issuer_erased == "true",
        )
        return paginate(
            request, views, route="GET /v1/agent-credentials", old_key="credentials",
            limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.post("/v1/agents/{agent_id}/credentials/revoke-all")
    def revoke_all(agent_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        if not idem.keyed:
            return JSONResponse({"revoked": svc.revoke_all(agent_id, by=by)}, headers=_NO_STORE)
        with idem.effect() as uow:
            revoked = svc.revoke_all_in(uow, agent_id, by=by)
            return idem.respond(uow, 200, {"revoked": revoked}, subject=by, headers=_NO_STORE)

    @app.post("/v1/agents/{agent_id}/credentials/{credential_id}/revoke")
    def revoke(agent_id: str, credential_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        if not idem.keyed:
            return JSONResponse(svc.revoke(agent_id, credential_id, by=by), headers=_NO_STORE)
        with idem.effect() as uow:
            view = svc.revoke_in(uow, agent_id, credential_id, by=by)
            return idem.respond(uow, 200, view, subject=by, headers=_NO_STORE)

    @app.post("/v1/agents/{agent_id}/credentials/{credential_id}/rotate", status_code=201)
    def rotate(agent_id: str, credential_id: str, body: RotateBody, by: By) -> Response:
        svc = need()
        issued, until = svc.rotate(
            agent_id, credential_id, body.expires_in_days, body.grace_seconds, by=by
        )
        answer = created(svc, issued) | {
            "replaces": {"id": credential_id, "until": until.astimezone(UTC).isoformat()}
        }
        return JSONResponse(answer, status_code=201, headers=_NO_STORE)
