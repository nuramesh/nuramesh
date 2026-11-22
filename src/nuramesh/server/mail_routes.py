"""`GET /v1/auth/email/status` (R1.39): whether email is on, and what is missing. An owner in a
human session only (the `admin` rule). Names and counts, never a value."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from nuramesh.server.authz import Caller
from nuramesh.server.mail import MailService
from nuramesh.server.sessions import AuthProblem


def install_mail(app: FastAPI, service: MailService, caller: Callable[..., Caller]) -> None:
    """Add the status route."""

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    @app.get("/v1/auth/email/status")
    def email_status(by: Annotated[str, Depends(owner)]) -> Response:
        return JSONResponse(service.status(), headers={"Cache-Control": "no-store"})
