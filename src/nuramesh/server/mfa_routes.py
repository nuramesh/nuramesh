"""MFA routes for a signed-in person and the owner (R1.38).

The person routes are rule R plus a handler check that the caller is a person: an API token or
an agent credential is 403 `human_required`. The routes that return a secret answer
`Cache-Control: no-store` and take no `Idempotency-Key`. The owner routes are rule A.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, model_validator

from nuramesh.server.auth import (
    Body,
    EmptyBody,
    _body,
    _set_cookies,
    cookie_names,
    is_secure,
)
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.limits import client_address
from nuramesh.server.mfa_login import MfaLogin
from nuramesh.server.mfa_notice import slot_of
from nuramesh.server.mfa_service import Asker, MfaService
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem

_NO_STORE = {"Cache-Control": "no-store"}


class EnrollBody(Body):
    """The password entered again."""

    password: str = Field(max_length=4096)


class ConfirmBody(Body):
    """The pending factor and a code from the authenticator app."""

    factor_id: str = Field(max_length=64)
    code: str = Field(max_length=32)


class PasswordCodeBody(Body):
    """The password entered again and a TOTP code."""

    password: str = Field(max_length=4096)
    code: str = Field(max_length=32)


class LoginMfaBody(Body):
    """A challenge and exactly one of a TOTP code and an MFA recovery code."""

    challenge: str = Field(max_length=256)
    code: str | None = Field(default=None, max_length=32)
    recovery_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _exactly_one(self) -> "LoginMfaBody":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("send exactly one of code and recovery_code")
        return self


class ChallengeBody(Body):
    """The challenge a password login answered."""

    challenge: str = Field(max_length=256)


class LoginConfirmBody(ChallengeBody):
    """The challenge, the pending factor and a code from the authenticator app."""

    factor_id: str = Field(max_length=64)
    code: str = Field(max_length=32)


class PolicyBody(Body):
    """Whether owners must use MFA."""

    required: bool


def install_mfa(
    app: FastAPI,
    service: MfaService | None,
    caller: Callable[..., Caller],
    idem_dep: Callable[..., IdemContext],
    *,
    trusted_proxy: TrustedProxies | bool = False,
) -> None:
    """Add the eight signed-in MFA routes (the login steps are separate public routes)."""

    def need() -> MfaService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def person(who: Annotated[Caller, Depends(caller)]) -> Caller:
        if not who.human or who.actor is None or who.kind != "session":
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who

    Who = Annotated[Caller, Depends(person)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    def asker(who: Caller, request: Request) -> Asker:
        assert who.actor is not None
        agent = request.headers.get("user-agent")
        return Asker(
            who.actor,
            who.session_id,
            client_address(request, trusted_proxy=trusted_proxy),
            None if agent is None else agent[:256],
        )

    def login_steps() -> MfaLogin:
        return MfaLogin(need())

    def agent_of(request: Request) -> str | None:
        value = request.headers.get("user-agent")
        return None if value is None else value[:256]

    def finish(request: Request, started: Any, extra: dict[str, Any] | None = None) -> Response:
        secure = is_secure(request, trusted_proxy=trusted_proxy)
        response = JSONResponse({**_body(started), **(extra or {})}, headers=_NO_STORE)
        _set_cookies(response, started, secure)
        return response

    def carried(request: Request) -> str | None:
        return request.cookies.get(cookie_names(is_secure(request, trusted_proxy=trusted_proxy))[1])

    @app.post("/v1/auth/login/mfa")
    def login_mfa(body: LoginMfaBody, request: Request) -> Response:
        started = login_steps().verify(
            body.challenge,
            body.code,
            body.recovery_code,
            client_address(request, trusted_proxy=trusted_proxy),
            agent_of(request),
            carried(request),
        )
        return finish(request, started)

    @app.post("/v1/auth/login/mfa/enroll")
    def login_mfa_enroll(body: ChallengeBody, request: Request) -> Response:
        got = login_steps().enroll(
            body.challenge, client_address(request, trusted_proxy=trusted_proxy)
        )
        return JSONResponse(got, headers=_NO_STORE)

    @app.post("/v1/auth/login/mfa/enroll/confirm")
    def login_mfa_confirm(body: LoginConfirmBody, request: Request) -> Response:
        started, codes = login_steps().confirm(
            body.challenge,
            body.factor_id,
            body.code,
            client_address(request, trusted_proxy=trusted_proxy),
            agent_of(request),
            carried(request),
        )
        return finish(request, started, {"recovery_codes": codes})

    @app.get("/v1/auth/mfa")
    def status(who: Who) -> Response:
        assert who.actor is not None
        return JSONResponse(need().status(who.actor), headers=_NO_STORE)

    @app.post("/v1/auth/mfa/enroll")
    def enroll(body: EnrollBody, who: Who, request: Request) -> Response:
        return JSONResponse(need().enroll(asker(who, request), body.password), headers=_NO_STORE)

    @app.post("/v1/auth/mfa/enroll/confirm")
    def confirm(body: ConfirmBody, who: Who, request: Request) -> Response:
        codes = need().confirm_enroll(asker(who, request), body.factor_id, body.code)
        return JSONResponse({"recovery_codes": codes}, headers=_NO_STORE)

    @app.post("/v1/auth/mfa/recovery-codes")
    def regenerate(body: PasswordCodeBody, who: Who, request: Request) -> Response:
        codes = need().regenerate(asker(who, request), body.password, body.code)
        return JSONResponse({"recovery_codes": codes}, headers=_NO_STORE)

    @app.post("/v1/auth/mfa/disable")
    def disable(body: PasswordCodeBody, who: Who, request: Request) -> Response:
        need().disable(asker(who, request), body.password, body.code)
        return JSONResponse({"enrolled": False}, headers=_NO_STORE)

    @app.post("/v1/auth/users/{user_id}/mfa/reset")
    def reset(user_id: str, body: EmptyBody, who: Who, idem: Idem) -> Response:
        svc = need()
        assert who.actor is not None
        by = who.actor
        svc.auth.require_person(user_id)  # 404 first, then the key check
        idem.require_alive()
        response: Response
        with slot_of(svc.desk, user_id, "reset") as notice:
            if not idem.keyed:
                with svc.store.transaction() as uow:
                    changed, _ = svc.reset_in(uow, by, user_id)
                response = JSONResponse({"user": user_id, "changed": changed}, headers=_NO_STORE)
            else:
                with idem.effect() as uow:
                    changed, _ = svc.reset_in(uow, by, user_id)
                    payload: dict[str, Any] = {"user": user_id, "changed": changed}
                    response = idem.respond(uow, 200, payload, subject=user_id, headers=_NO_STORE)
            if changed:
                notice.send(by)
        return response

    @app.get("/v1/auth/settings")
    def settings(who: Who) -> Response:
        return JSONResponse(need().settings_view(), headers=_NO_STORE)

    @app.put("/v1/auth/settings/mfa")
    def set_policy(body: PolicyBody, who: Who, idem: Idem) -> Response:
        svc = need()
        assert who.actor is not None
        idem.require_alive()
        answer = {"mfa_required_for_owners": body.required}
        if not idem.keyed:
            svc.set_policy(who.actor, body.required)
            return JSONResponse(answer, headers=_NO_STORE)
        with idem.effect() as uow:
            svc.set_policy_in(uow, who.actor, body.required)
            return idem.respond(uow, 200, answer, headers=_NO_STORE)
