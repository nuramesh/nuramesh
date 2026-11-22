"""People and roles (E1 PR 1; `spec/e1/README.md` sections 9.1 and 10).

An **owner in a human session** lists the organization's people, disables and enables a person
and sets a role (`viewer` or `approver`). All four routes are the `admin` rule, so an API token or
an agent credential is refused `human_required` before a handler runs; the handler checks again.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.core.auth import User
from nuramesh.server.agent_credentials import AgentCredentialService
from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.pagination import Cursor, Limit, paginate
from nuramesh.server.sessions import AuthProblem, AuthService, LastOwnerRefused

_NO_STORE = {"Cache-Control": "no-store"}


class RoleBody(Body):
    """The new role. Any string is accepted here; the service answers 422 for anything but
    `viewer` and `approver` (and `role_not_assignable` for `owner`, which comes first)."""

    role: str = Field(max_length=64)


def person_view(user: User) -> dict[str, Any]:
    """What an owner sees of one person (the email is personal data an owner may see)."""
    return {
        "id": user.id,
        "role": user.role.value,
        "email": user.email,
        "display_name": user.display_name,
        "disabled": user.disabled,
        "email_verified": user.email_verified_at is not None,
        "created_at": user.created_at.isoformat(),
    }


def install_users(
    app: FastAPI,
    service: AuthService | None,
    caller: Callable[..., Caller],
    idem_dep: Callable[..., IdemContext],
    agents: AgentCredentialService | None = None,
) -> None:
    """Add `GET /v1/auth/users`, `POST .../disable` and `.../enable`, `PUT .../role`."""

    def need() -> AuthService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(owner)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    @app.get("/v1/auth/users")
    def list_users(
        by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        views = [person_view(u) for u in need().list_people()]
        return paginate(
            request, views, route="GET /v1/auth/users", old_key="users",
            limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.get("/v1/auth/users/{user_id}/agent-credentials")
    def issued_credentials(
        user_id: str, by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        """The live agent credentials the person issued (D5), oldest first: what an owner looks
        at before or after disabling them. Credentials are revoked one at a time through their own
        route."""
        need().require_person(user_id)
        views = [] if agents is None else agents.issued_views(user_id)
        return paginate(
            request, views, route="GET /v1/auth/users/{user_id}/agent-credentials",
            old_key="credentials", limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.post("/v1/auth/users/{user_id}/disable")
    def disable_user(user_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        answer = {"user": user_id, "disabled": True}
        if not idem.keyed:
            return JSONResponse(
                {**answer, "changed": svc.disable_user(by, user_id)}, headers=_NO_STORE
            )
        try:
            with idem.effect() as uow:
                changed = svc.disable_user_in(uow, by, user_id)
                return idem.respond(
                    uow, 200, {**answer, "changed": changed}, subject=user_id, headers=_NO_STORE
                )
        except LastOwnerRefused as refused:
            svc.record_last_owner_refused(by, "disable", refused.target)
            raise

    @app.post("/v1/auth/users/{user_id}/enable")
    def enable_user(user_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        answer = {"user": user_id, "disabled": False}
        svc.require_person(user_id)  # 404 first (spec precedence), then the key check
        idem.require_alive()
        if not idem.keyed:
            return JSONResponse(
                {**answer, "changed": svc.enable_user(by, user_id)}, headers=_NO_STORE
            )
        with idem.effect() as uow:
            changed = svc.enable_user_in(uow, by, user_id)
            return idem.respond(
                uow, 200, {**answer, "changed": changed}, subject=user_id, headers=_NO_STORE
            )

    @app.put("/v1/auth/users/{user_id}/role")
    def set_role(user_id: str, body: RoleBody, by: By, idem: Idem) -> Response:
        svc = need()
        new = svc.assignable_role(body.role)  # 422 first, then 404, then the key check
        svc.require_person(user_id)
        idem.require_alive()
        if not idem.keyed:
            changed = svc.set_role(by, user_id, body.role)
            return JSONResponse(
                {"user": user_id, "role": body.role, "changed": changed}, headers=_NO_STORE
            )
        try:
            with idem.effect() as uow:
                changed = svc.set_role_in(uow, by, user_id, new)
                return idem.respond(
                    uow,
                    200,
                    {"user": user_id, "role": body.role, "changed": changed},
                    subject=user_id,
                    headers=_NO_STORE,
                )
        except LastOwnerRefused as refused:
            svc.record_last_owner_refused(by, "demote", refused.target)
            raise
