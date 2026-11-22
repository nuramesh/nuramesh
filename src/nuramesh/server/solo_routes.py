"""Solo approval at the HTTP edge (spec/rbac/solo-approval.md, #942 PR 1).

Three things live here, so `app.py` only wires them:

- `solo_terms`: the terms the approve routes pass to the engine. Only a person in a cookie session
  of a server with authentication on gets terms (SA-2); for everyone else the engine keeps its
  default, "never solo".
- `terms_view`: `caller_terms` of the single approval view (SA-16), computed for the caller from
  what an approve sent now would read, and never stored.
- `install_password_confirm`: `POST /v1/auth/password/confirm` (SA-8). The password is checked by
  the existing `AuthService.confirm_password` (same limiter, hashing budget, counters and lockout
  as a sign-in; success clears no counter); a success stamps only the calling session and writes
  one `auth.password_confirmed` entry. Nothing portable is issued.

A refusal's detail is fixed text: never a digest, a time, a session id or what was sent.
"""

import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator

from nuramesh.core.auth import MAX_PASSWORD_LENGTH
from nuramesh.core.solo import FRESH_SECONDS, PROOF_TEXTS
from nuramesh.engine.originator import originator_of
from nuramesh.engine.solo import SoloRefused, SoloTerms, caller_terms
from nuramesh.engine.solo_status import StoreReads
from nuramesh.hosted import HostedConfigError, hosted_from_env
from nuramesh.server.auth import Body
from nuramesh.server.authz import Caller
from nuramesh.server.limits import client_address
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.sessions import AuthProblem, AuthService
from nuramesh.stores.base import Store, StoredApproval

__all__ = [
    "PASSWORD_BODY_BYTES",
    "PasswordConfirmBody",
    "install_password_confirm",
    "solo_problem",
    "solo_terms",
    "terms_view",
]

PASSWORD_BODY_BYTES = 4096
"""The cheap pre-bound of a confirm password, in UTF-8 bytes, checked before NFKC (SA-8)."""

_NO_STORE = {"Cache-Control": "no-store"}


def _hosted() -> bool:
    try:
        return hosted_from_env()
    except HostedConfigError:
        return True  # an unreadable flag fails closed (section 1, "Hosted")


def solo_problem(refused: SoloRefused) -> AuthProblem:
    """The answer of a refused solo approve (table 3.3): fixed code and text, `Retry-After` for
    the limit. Each code is spelled out (the error catalogue reads literals); a code this table
    does not know answers 403 `step_up_required`, never a pass."""
    code, detail = refused.code, str(refused)
    if code == "acknowledge_required":
        return AuthProblem(403, "acknowledge_required", detail)
    if code == "action_changed":
        return AuthProblem(409, "action_changed", detail)
    if code == "mfa_unavailable":
        return AuthProblem(403, "mfa_unavailable", detail)
    if code == "mfa_enrollment_required":
        return AuthProblem(403, "mfa_enrollment_required", detail)
    if code == "mfa_required":
        return AuthProblem(403, "mfa_required", detail)
    if code == "solo_rate_limited":
        wait = 1 if refused.retry_after is None else refused.retry_after
        return AuthProblem(429, "solo_rate_limited", detail, {"Retry-After": str(wait)})
    return AuthProblem(403, "step_up_required", PROOF_TEXTS["step_up_required"])


def solo_terms(
    who: Caller,
    auth: AuthService | None,
    limit_per_hour: int,
    *,
    acknowledge: bool = False,
    action_digest: str | None = None,
) -> SoloTerms | None:
    """The terms for this caller, or None ("never solo") unless they are a person in a cookie
    session of a server with authentication on (SA-2). The `dev_human` harness, a token and an
    agent credential never get terms."""
    if auth is None or who.kind != "session" or not who.human or who.session_id is None:
        return None
    return SoloTerms(
        session_id=who.session_id,
        hosted=_hosted(),
        mfa_available=auth.mfa_available(),
        limit_per_hour=limit_per_hour,
        acknowledge=acknowledge,
        action_digest=action_digest,
    )


def terms_view(
    store: Store,
    who: Caller,
    auth: AuthService | None,
    org_id: str,
    limit_per_hour: int,
    stored: StoredApproval,
    now: datetime,
) -> dict[str, Any] | None:
    """`caller_terms` for this caller: None when the approval is not pending or the caller is
    not a person in a cookie session (SA-16)."""
    terms = solo_terms(who, auth, limit_per_hour)
    if terms is None or who.actor is None:
        return None
    reads = StoreReads(store)
    approval = stored.approval
    originator = originator_of(reads, org_id, approval.run_id)
    return caller_terms(reads, org_id, approval, who.actor, originator, terms, now)


class PasswordConfirmBody(Body):
    """`{password}` and nothing else. 1 to 1024 characters after NFKC (main's own limit), with a
    cheap bound of 4096 UTF-8 bytes first. Errors never carry the input back."""

    password: str = Field(strict=True)

    @field_validator("password")
    @classmethod
    def _bounded(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("the password is not valid text") from None
        if not 1 <= size <= PASSWORD_BODY_BYTES:
            raise ValueError("the password is empty or too long")
        if len(unicodedata.normalize("NFKC", value)) > MAX_PASSWORD_LENGTH:
            raise ValueError("the password is too long")
        return value


def install_password_confirm(
    app: FastAPI,
    service: AuthService | None,
    caller: Callable[..., Caller],
    *,
    trusted_proxy: TrustedProxies | bool = False,
) -> None:
    """Add `POST /v1/auth/password/confirm` (rows in `authz.ROUTE_RULES`, `routes.ROUTE_SCOPES`
    and the tenancy table). The route takes no `Idempotency-Key`; its body is never stored,
    hashed for replay or logged."""

    def person(who: Annotated[Caller, Depends(caller)]) -> Caller:
        # the authorizer already refused tokens, agent credentials and authentication off
        # (`authz.PERSON_SESSION_ONLY`); checked again so a wrong table row lets nobody through
        if who.kind != "session" or not who.human or who.actor is None or who.session_id is None:
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who

    @app.post("/v1/auth/password/confirm")
    def confirm(
        body: PasswordConfirmBody, who: Annotated[Caller, Depends(person)], request: Request
    ) -> Response:
        """A signed-in person enters the password again; a success is a fresh proof for a solo
        approval of a high-risk kind for the next 600 s, on this session only."""
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        assert who.actor is not None and who.session_id is not None
        agent = request.headers.get("user-agent")
        service.confirm_session_password(
            who.actor,
            who.session_id,
            body.password,
            client_address(request, trusted_proxy=trusted_proxy),
            user_agent=None if agent is None else agent[:256],
        )
        return JSONResponse({"expires_in": FRESH_SECONDS}, headers=_NO_STORE)
