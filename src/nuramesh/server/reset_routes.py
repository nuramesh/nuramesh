"""Password reset routes (R1.41; section 2).

Two public routes (request and redeem), one owner route (`admin` rule) and one public route that
revokes an API token by its emailed link. The request answer is the same bytes and headers for
every address; no route ever returns or prints a token or a link.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.core.ledger import format_time
from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.limits import client_address
from nuramesh.server.password_reset import ResetService
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem

_NO_STORE = {"Cache-Control": "no-store"}
ACCEPTED = b'{"status":"accepted"}'


class RequestBody(Body):
    email: str = Field(max_length=320)


class RedeemBody(Body):
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(max_length=4096)


class LinkBody(Body):
    token: str = Field(min_length=1, max_length=512)


def install_password_reset(
    app: FastAPI,
    service: ResetService | None,
    caller: Callable[..., Caller],
    *,
    trusted_proxy: TrustedProxies | bool = False,
) -> None:
    """Add the four password-reset routes."""

    def need() -> ResetService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(owner)]

    def seen(request: Request) -> tuple[str, str | None]:
        agent = request.headers.get("user-agent")
        return (
            client_address(request, trusted_proxy=trusted_proxy),
            None if agent is None else agent[:256],
        )

    @app.post("/v1/auth/password-reset/request", status_code=202)
    def request_reset(body: RequestBody, request: Request) -> Response:
        who, agent = seen(request)
        need().request(body.email, who, user_agent=agent)
        return Response(ACCEPTED, status_code=202, media_type="application/json", headers=_NO_STORE)

    @app.post("/v1/auth/password-reset/redeem")
    def redeem_reset(body: RedeemBody, request: Request) -> Response:
        who, agent = seen(request)
        need().redeem(body.token, body.password, who, user_agent=agent)
        return JSONResponse({"status": "password_changed"}, headers=_NO_STORE)

    @app.post("/v1/auth/users/{user_id}/password-reset", status_code=202)
    def owner_reset(user_id: str, body: EmptyBody, by: By) -> Response:
        expires = need().owner_issue(by, user_id)
        return JSONResponse(
            {"user": user_id, "expires_at": format_time(expires)},
            status_code=202,
            headers=_NO_STORE,
        )

    @app.post("/v1/auth/tokens/revoke-by-link")
    def revoke_by_link(body: LinkBody, request: Request) -> Response:
        who, agent = seen(request)
        need().revoke_by_link(body.token, who, user_agent=agent)
        return JSONResponse({"status": "revoked"}, headers=_NO_STORE)
