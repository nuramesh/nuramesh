"""Invite routes (R1.37; `spec/identity/invites.md` section 2).

Four owner routes (`admin` rule: an owner in a human session) and two public ones (inspect and
accept, each checking its own link token). No route returns or prints a token or a link: the
answers of create and resend are the same `view` as list. With no mail sender the create and
resend routes answer 503 `email_not_configured` and write nothing.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.invites import InviteService, invite_view
from nuramesh.server.limits import client_address
from nuramesh.server.pagination import Cursor, Limit, paginate
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem

_NO_STORE = {"Cache-Control": "no-store"}


class CreateBody(Body):
    email: str = Field(max_length=320)
    role: str = Field(max_length=64)


class TokenBody(Body):
    token: str = Field(min_length=1, max_length=512)


class AcceptBody(TokenBody):
    password: str = Field(max_length=4096)
    display_name: str | None = Field(default=None, max_length=200)


def install_invites(
    app: FastAPI,
    service: InviteService | None,
    caller: Callable[..., Caller],
    idem_dep: Callable[..., IdemContext],
    *,
    trusted_proxy: TrustedProxies | bool = False,
) -> None:
    """Add the six `/v1/auth/invites` routes."""

    def need() -> InviteService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(owner)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    def view(svc: InviteService, invite: object) -> dict[str, object]:
        return invite_view(invite, svc.auth.now())  # type: ignore[arg-type]

    @app.get("/v1/auth/invites")
    def list_invites(
        by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        return paginate(
            request, need().list_invites(), route="GET /v1/auth/invites", old_key="invites",
            limit=limit, cursor=cursor, headers=_NO_STORE,
        )  # fmt: skip

    @app.post("/v1/auth/invites", status_code=201)
    def create_invite(body: CreateBody, by: By) -> Response:
        svc = need()
        issued = svc.create(by, body.email, body.role)
        return JSONResponse(
            {"invite": view(svc, issued.invite)}, status_code=201, headers=_NO_STORE
        )

    @app.post("/v1/auth/invites/inspect")
    def inspect_invite(body: TokenBody, request: Request) -> Response:
        who = client_address(request, trusted_proxy=trusted_proxy)
        agent = request.headers.get("user-agent")
        answer = need().inspect(body.token, who, user_agent=None if agent is None else agent[:256])
        return JSONResponse(answer, headers=_NO_STORE)

    @app.post("/v1/auth/invites/accept", status_code=201)
    def accept_invite(body: AcceptBody, request: Request) -> Response:
        who = client_address(request, trusted_proxy=trusted_proxy)
        agent = request.headers.get("user-agent")
        user = need().accept(
            body.token,
            body.password,
            body.display_name,
            who,
            user_agent=None if agent is None else agent[:256],
        )
        return JSONResponse(
            {
                "user": {
                    "id": user.id,
                    "role": user.role.value,
                    "email": user.email,
                    "email_verified": True,
                }
            },
            status_code=201,
            headers=_NO_STORE,
        )

    @app.post("/v1/auth/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        if not idem.keyed:
            invite, changed = svc.revoke(by, invite_id)
            return JSONResponse(
                {"invite": view(svc, invite), "changed": changed}, headers=_NO_STORE
            )
        with idem.effect() as uow:
            invite, changed = svc.revoke_in(uow, by, invite_id)
            return idem.respond(
                uow, 200, {"invite": view(svc, invite), "changed": changed}, headers=_NO_STORE
            )

    @app.post("/v1/auth/invites/{invite_id}/resend")
    def resend_invite(invite_id: str, body: EmptyBody, by: By) -> Response:
        svc = need()
        issued = svc.resend(by, invite_id)
        return JSONResponse({"invite": view(svc, issued.invite)}, headers=_NO_STORE)
