"""The auth endpoints (R1.22 PR 2): login, refresh, logout, who am I, and
redeeming the first-owner setup token.

These routes are `PUBLIC` in the authorization table (`nuramesh.server.authz`): each checks its
own credentials. Every other route follows that table (R1.22 PR 3), and with `--auth` a session
made here is what makes a caller the person `human:u_...`.

Cookie sessions: three cookies, all `SameSite=Strict`, `Path=/`, and `Secure` with the `__Host-`
name prefix when the request arrived over https (or through a trusted TLS-terminating proxy):

- `nm_session`: the access token (15 minutes), `HttpOnly`;
- `nm_refresh`: the refresh token (8 hours idle, 7 days absolute), `HttpOnly`;
- `nm_csrf`: the CSRF token, readable by the page's script, which sends it as `X-CSRF-Token`.
"""

import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import HTTPConnection

from nuramesh.core.auth import ACCESS_LIFETIME, csrf_token
from nuramesh.server.headers import secure_transport
from nuramesh.server.limits import client_address
from nuramesh.server.problems import problem_response as render_problem
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem, AuthService, MfaPending, Started
from nuramesh.server.setup_page import SETUP_PATH, setup_page

COOKIE_ACCESS = "nm_session"
COOKIE_REFRESH = "nm_refresh"
COOKIE_CSRF = "nm_csrf"
HEADER_CSRF = "x-csrf-token"
HOST_PREFIX = "__Host-"


class Body(BaseModel):
    # an auth payload holds passwords and tokens: errors never carry the input back
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _no_invisible_characters(value: str) -> str:
    """Refuse format characters (Unicode category Cf: zero-width, bidi overrides) that the
    address and name checks in core let through and that make look-alike accounts."""
    if any(unicodedata.category(ch) == "Cf" for ch in value):
        raise ValueError("contains invisible formatting characters")
    return value


class LoginBody(Body):
    """Email and password. Limits keep the body small; wrong shapes are still a plain 401."""

    # no length limits here: an over-long value is an ordinary failed login (generic 401, counted),
    # not a 422 that tells an attacker what the limit is; the guard caps the whole body at 1 MB
    email: str
    password: str


class EmptyBody(Body):
    """Mutations carry an empty JSON object (the guard requires a JSON body)."""


class SetupBody(Body):
    """The one-time setup token with the first owner's chosen details."""

    token: str = Field(min_length=1, max_length=512)
    email: str = Field(max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    password: str = Field(max_length=4096)

    @field_validator("email", "display_name")
    @classmethod
    def _visible(cls, value: str | None) -> str | None:
        return None if value is None else _no_invisible_characters(value)


def problem_response(exc: AuthProblem) -> JSONResponse:
    """An auth error as `application/problem+json`, with `Retry-After` and the like."""
    return render_problem(exc.status, exc.code, exc.detail, headers=exc.headers)


def is_secure(request: HTTPConnection, *, trusted_proxy: TrustedProxies | bool) -> bool:
    """Whether the browser reached us over https: the request's scheme, or, only when the
    operator says the proxy is trusted, the proxy's `X-Forwarded-Proto`: its **last** value
    over all header lines and comma lists (the rule of the headers and `X-Forwarded-For`)."""
    return secure_transport(request.scope, trusted_proxy)


def cookie_names(secure: bool) -> tuple[str, str, str]:
    """(access, refresh, csrf) cookie names; `__Host-` prefixed when Secure."""
    prefix = HOST_PREFIX if secure else ""
    return prefix + COOKIE_ACCESS, prefix + COOKIE_REFRESH, prefix + COOKIE_CSRF


def _set_cookies(response: Response, started: Started, secure: bool) -> None:
    access, refresh, csrf = cookie_names(secure)
    issued = started.issued
    now = issued.access.created_at
    common: dict[str, Any] = {"path": "/", "secure": secure, "samesite": "strict"}
    idle = max(1, int((issued.refresh.expires_at - now).total_seconds()))
    response.set_cookie(
        access,
        issued.access_token,
        max_age=max(1, int((issued.access.expires_at - now).total_seconds())),
        httponly=True,
        **common,
    )
    response.set_cookie(refresh, issued.refresh_token, max_age=idle, httponly=True, **common)
    response.set_cookie(
        csrf, csrf_token(issued.refresh_token), max_age=idle, httponly=False, **common
    )


def _clear_cookies(response: Response, secure: bool) -> None:
    access, refresh, csrf = cookie_names(secure)
    for name, http_only in ((access, True), (refresh, True), (csrf, False)):
        response.delete_cookie(name, path="/", secure=secure, httponly=http_only, samesite="strict")


def _body(started: Started) -> dict[str, Any]:
    user, session, issued = started.user, started.session, started.issued
    return {
        "user": {
            "id": user.id,
            "org_id": user.org_id,
            "role": user.role.value,
            "email": user.email,
            "display_name": user.display_name,
        },
        "session": {
            "id": session.id,
            "access_expires_at": issued.access.expires_at.isoformat(),
            "idle_expires_at": issued.refresh.expires_at.isoformat(),
            "absolute_expires_at": session.absolute_expires_at.isoformat(),
        },
        "csrf_token": csrf_token(issued.refresh_token),
        "access_lifetime_seconds": int(ACCESS_LIFETIME.total_seconds()),
    }


def pending_body(pending: MfaPending) -> dict[str, Any]:
    """The answer to a password that needs a second step."""
    body: dict[str, Any] = {"status": pending.status, "challenge": pending.challenge}
    if pending.methods is not None:
        body["methods"] = list(pending.methods)
    body["expires_in"] = pending.expires_in
    return body


def install_auth(app: FastAPI, service: AuthService | None, *, required: bool = False) -> None:
    """Add the endpoints under `/v1/auth` and the first-owner page `GET /setup`. `service` is
    None for a server started without an organization: every `/v1/auth` route then answers 409
    `no_config`. `required` says whether authentication is on: the setup page exists only then
    (SA-0.10)."""
    trusted = service.settings.trusted_proxy if service is not None else False

    def need() -> AuthService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def secure_of(request: Request) -> bool:
        return is_secure(request, trusted_proxy=trusted)

    def cookies(request: Request, secure: bool) -> tuple[str | None, str | None]:
        access, refresh, _ = cookie_names(secure)
        return request.cookies.get(access), request.cookies.get(refresh)

    def client(request: Request) -> str:
        return client_address(request, trusted_proxy=trusted)

    def agent(request: Request) -> str | None:
        # the security log only (ND60), never the ledger; bounded
        value = request.headers.get("user-agent")
        return None if value is None else value[:256]

    @app.post("/v1/auth/login")
    def login(body: LoginBody, request: Request) -> Response:
        svc, secure = need(), secure_of(request)
        _, previous = cookies(request, secure)  # never adopted: it is revoked, a new id is made
        started = svc.begin_login(
            body.email, body.password, client(request), previous, user_agent=agent(request)
        )
        if isinstance(started, MfaPending):
            return JSONResponse(pending_body(started), headers={"Cache-Control": "no-store"})
        response = JSONResponse(_body(started), headers={"Cache-Control": "no-store"})
        _set_cookies(response, started, secure)
        return response

    @app.post("/v1/auth/refresh")
    def refresh(body: EmptyBody, request: Request) -> Response:
        svc, secure = need(), secure_of(request)
        _, token = cookies(request, secure)
        try:
            svc.limit_client(client(request))  # before anything else
            if token is None:
                raise AuthProblem(401, "session_invalid", "sign in again")
            svc.require_csrf(token, request.headers.get(HEADER_CSRF))
            started = svc.refresh(token, client=client(request), user_agent=agent(request))
        except AuthProblem as exc:
            response = problem_response(exc)
            if exc.code == "session_invalid":
                _clear_cookies(response, secure)
            return response
        response = JSONResponse(_body(started), headers={"Cache-Control": "no-store"})
        _set_cookies(response, started, secure)
        return response

    @app.post("/v1/auth/logout")
    def logout(body: EmptyBody, request: Request) -> Response:
        svc, secure = need(), secure_of(request)
        _, token = cookies(request, secure)
        if token is None:
            response = problem_response(
                AuthProblem(401, "session_invalid", "you are not signed in")
            )
            _clear_cookies(response, secure)
            return response
        svc.require_csrf(token, request.headers.get(HEADER_CSRF))
        svc.logout(token, client=client(request), user_agent=agent(request))
        done = Response(status_code=204, headers={"Cache-Control": "no-store"})
        _clear_cookies(done, secure)
        return done

    @app.get("/v1/auth/session")
    def whoami(request: Request) -> Response:
        svc, secure = need(), secure_of(request)
        access, refresh_token = cookies(request, secure)
        principal = svc.authenticate(access)
        user = principal.user
        return JSONResponse(
            {
                "user": {
                    "id": user.id,
                    "org_id": user.org_id,
                    "role": user.role.value,
                    "email": user.email,
                    "display_name": user.display_name,
                    "email_verified": user.email_verified_at is not None,
                },
                "session": {
                    "id": principal.session.id,
                    "absolute_expires_at": principal.session.absolute_expires_at.isoformat(),
                },
                "csrf_token": svc.csrf_for(principal, refresh_token),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/auth/setup/status")
    def setup_status(request: Request) -> Response:
        """Boolean only: has the first owner not been made yet? Read-only, rate limited, public."""
        svc = need()
        try:
            svc.limit_client(client(request))
        except AuthProblem as exc:
            return problem_response(exc)
        return JSONResponse(
            {"owner_missing": not svc.owner_ever_existed()}, headers={"Cache-Control": "no-store"}
        )

    def first_owner_page(request: Request) -> Response:
        """The API's own setup page (SA-0.10). 404 while authentication is off, without an
        organization, and for ever once an owner record has existed (an erased owner included:
        a closed organization is not "no owner"). It reads one boolean of the server's own
        organization and writes nothing; the token never reaches this route (it is in the
        URL's fragment)."""
        gone = render_problem(404, "not_found", "there is no setup page here")
        if service is None or not required:
            return gone
        try:
            service.limit_client(client(request))
        except AuthProblem as exc:
            return problem_response(exc)
        return gone if service.owner_ever_existed() else setup_page()

    # a plain route, not an API operation: it is HTML and stays out of the API description
    app.add_route(SETUP_PATH, first_owner_page, methods=["GET"], include_in_schema=False)

    @app.post("/v1/auth/setup/redeem", status_code=201)
    def redeem(body: SetupBody, request: Request) -> Response:
        svc = need()
        user = svc.redeem_setup(
            body.token,
            body.email,
            body.display_name,
            body.password,
            client(request),
            user_agent=agent(request),
        )
        return JSONResponse(
            {"user": {"id": user.id, "role": user.role.value, "email": user.email}},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
