"""Email change routes (R1.40; `spec/identity/email-verification.md` section 3).

`POST /v1/auth/me/email` (rule R plus a person check) starts a change and always answers 202 once
the password is right, whether or not the new address is taken. `POST /v1/auth/email/verify`
(public) finishes it with the link token. Neither returns a token or a link.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.server.auth import Body
from nuramesh.server.authz import Caller
from nuramesh.server.email_change import EmailChangeService
from nuramesh.server.limits import client_address
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem

_NO_STORE = {"Cache-Control": "no-store"}


class EmailChangeStartBody(Body):
    new_email: str = Field(max_length=320)
    password: str = Field(max_length=4096)


class EmailChangeVerifyBody(Body):
    token: str = Field(min_length=1, max_length=512)


def install_email_change(
    app: FastAPI,
    service: EmailChangeService | None,
    caller: Callable[..., Caller],
    *,
    trusted_proxy: TrustedProxies | bool = False,
) -> None:
    """Add the two routes."""

    def need() -> EmailChangeService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def person(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # an API token, agent or auth-off mode
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(person)]

    def agent(request: Request) -> str | None:
        value = request.headers.get("user-agent")
        return None if value is None else value[:256]

    @app.post("/v1/auth/me/email", status_code=202)
    def start_email_change(body: EmailChangeStartBody, request: Request, by: By) -> Response:
        who = client_address(request, trusted_proxy=trusted_proxy)
        need().start(by, body.new_email, body.password, who, user_agent=agent(request))
        return JSONResponse({"status": "verification_sent"}, status_code=202, headers=_NO_STORE)

    @app.post("/v1/auth/email/verify")
    def verify_email_change(body: EmailChangeVerifyBody, request: Request) -> Response:
        who = client_address(request, trusted_proxy=trusted_proxy)
        need().verify(body.token, who, user_agent=agent(request))
        return JSONResponse({"status": "email_changed"}, headers=_NO_STORE)
