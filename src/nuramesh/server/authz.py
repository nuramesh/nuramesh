"""Who is calling, and may they (R1.22 PR 3).

**One table decides every route.** `ROUTE_RULES` maps each `(method, path template)` the app
serves to what it needs: `PUBLIC`, or a `core.auth.Permission`. `Authorizer` (pure ASGI, inside
the Host/Origin guard) finds the matched route, authenticates the caller and checks the rule
before the route runs. **A route with no row is refused** (403 `no_rule`): default deny, and
`tests/server/test_authz_matrix.py` fails for any route of the app without a row.

**The caller** comes only from the credentials, never from a body, a query or another header:

- a cookie session (`nm_session`) is the person `human:u_...` with their role's permissions; a
  mutation also needs the session's CSRF token in `X-CSRF-Token`;
- `Authorization: Bearer nmt_...` is an API token, the actor `service:<token id>` with its scopes'
  permissions (`read`, `run`); it never has `decide`, `reveal` or `admin`, which need a person;
- with **authentication off** (the loopback-only local mode) nobody is authenticated: `PUBLIC`
  and `read` routes are open, `admin` routes are refused, and `run`, `decide` and `reveal` are
  refused too unless the app was built with the in-process `dev_human` harness (tests and the
  app's contract scripts only; `nuramesh serve` has no such flag any more).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import anyio.to_thread
from starlette.requests import HTTPConnection
from starlette.routing import Match

from nuramesh.core.auth import (
    AGENT_CREDENTIAL_PREFIX,
    HUMAN_ONLY,
    ROLE_PERMISSIONS,
    Permission,
    scope_permissions,
)
from nuramesh.core.people import is_human_id
from nuramesh.server.agent_auth import AgentAuth
from nuramesh.server.auth import HEADER_CSRF, cookie_names, is_secure, problem_response
from nuramesh.server.limits import client_address
from nuramesh.server.proxies import TrustedProxies
from nuramesh.server.scope import LOCAL_INSPECTOR
from nuramesh.server.sessions import AuthProblem, AuthService
from nuramesh.server.tokens import TokenService
from nuramesh.stores import StoreBusyError

PUBLIC: Final = "public"
# public routes that also recognise a signed-in caller (a bad credential stays anonymous)
OPTIONAL_AUTH: Final = frozenset({("GET", "/v1/health")})
WS: Final = "WS"
"""The method name the table uses for WebSocket routes."""

Rule = Permission | str

ROUTE_RULES: dict[tuple[str, str], Rule] = {
    # service
    ("GET", "/v1/health"): PUBLIC,
    ("GET", "/v1/openapi.json"): Permission.READ,
    ("HEAD", "/v1/openapi.json"): Permission.READ,
    ("GET", "/v1/docs"): Permission.READ,
    ("HEAD", "/v1/docs"): Permission.READ,
    ("GET", "/v1/schema/config"): Permission.READ,
    ("POST", "/v1/config/validate"): Permission.READ,
    # accounts and sessions: each checks its own credentials
    ("POST", "/v1/auth/login"): PUBLIC,
    ("POST", "/v1/auth/login/mfa"): PUBLIC,
    ("POST", "/v1/auth/login/mfa/enroll"): PUBLIC,
    ("POST", "/v1/auth/login/mfa/enroll/confirm"): PUBLIC,
    ("POST", "/v1/auth/refresh"): PUBLIC,
    ("POST", "/v1/auth/logout"): PUBLIC,
    ("GET", "/v1/auth/session"): PUBLIC,
    ("GET", "/v1/auth/setup/status"): PUBLIC,
    ("POST", "/v1/auth/setup/redeem"): PUBLIC,
    # the API's own first-owner page (SA-0.10); 404 with authentication off or once an owner exists
    ("GET", "/setup"): PUBLIC,
    ("HEAD", "/setup"): PUBLIC,
    # API tokens: an owner in a human session
    ("GET", "/v1/auth/tokens"): Permission.ADMIN,
    ("POST", "/v1/auth/tokens"): Permission.ADMIN,
    ("POST", "/v1/auth/tokens/{token_id}/revoke"): Permission.ADMIN,
    ("PATCH", "/v1/auth/tokens/{token_id}"): Permission.ADMIN,
    ("DELETE", "/v1/auth/tokens/{token_id}"): Permission.ADMIN,
    # people and roles (E1): an owner in a human session
    # outgoing email (R1.39): whether it is on; an owner in a human session
    ("GET", "/v1/auth/email/status"): Permission.ADMIN,
    ("GET", "/v1/auth/users"): Permission.ADMIN,
    ("GET", "/v1/auth/users/{user_id}/agent-credentials"): Permission.ADMIN,
    ("POST", "/v1/auth/users/{user_id}/disable"): Permission.ADMIN,
    ("POST", "/v1/auth/users/{user_id}/enable"): Permission.ADMIN,
    ("PUT", "/v1/auth/users/{user_id}/role"): Permission.ADMIN,
    # MFA (R1.38): the person's own routes are rule R plus a handler check that the caller is a
    # person (an API token or agent credential is 403 human_required); the owner's are admin
    ("GET", "/v1/auth/mfa"): Permission.READ,
    ("POST", "/v1/auth/mfa/enroll"): Permission.READ,
    ("POST", "/v1/auth/mfa/enroll/confirm"): Permission.READ,
    ("POST", "/v1/auth/mfa/recovery-codes"): Permission.READ,
    ("POST", "/v1/auth/mfa/disable"): Permission.READ,
    # solo approval (SA-8): any signed-in person of any role, and only a person in a cookie
    # session (`PERSON_SESSION_ONLY`)
    ("POST", "/v1/auth/password/confirm"): Permission.READ,
    ("POST", "/v1/auth/users/{user_id}/mfa/reset"): Permission.ADMIN,
    ("GET", "/v1/auth/settings"): Permission.ADMIN,
    ("PUT", "/v1/auth/settings/mfa"): Permission.ADMIN,
    # invites (R1.37): four for an owner in a human session, two public (each checks its link)
    ("GET", "/v1/auth/invites"): Permission.ADMIN,
    ("POST", "/v1/auth/invites"): Permission.ADMIN,
    ("POST", "/v1/auth/invites/{invite_id}/revoke"): Permission.ADMIN,
    ("POST", "/v1/auth/invites/{invite_id}/resend"): Permission.ADMIN,
    ("POST", "/v1/auth/invites/inspect"): PUBLIC,
    ("POST", "/v1/auth/invites/accept"): PUBLIC,
    # email change (R1.40): start is a person (READ plus a handler check), verify is public
    ("POST", "/v1/auth/me/email"): Permission.READ,
    ("POST", "/v1/auth/email/verify"): PUBLIC,
    # password reset (R1.41): two public, the owner's reset, and the public revoke-by-link
    ("POST", "/v1/auth/password-reset/request"): PUBLIC,
    ("POST", "/v1/auth/password-reset/redeem"): PUBLIC,
    ("POST", "/v1/auth/users/{user_id}/password-reset"): Permission.ADMIN,
    ("POST", "/v1/auth/tokens/revoke-by-link"): PUBLIC,
    # runs and tasks
    ("GET", "/v1/runs"): Permission.READ,
    ("GET", "/v1/runs/{run_id}"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/ledger"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/ledger/verify"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/stop"): Permission.READ,
    ("POST", "/v1/runs"): Permission.RUN,
    ("POST", "/v1/runs/{run_id}/stop"): Permission.RUN,
    ("GET", "/v1/org/halt"): Permission.READ,
    ("GET", "/v1/org/status"): Permission.READ,
    ("POST", "/v1/org/halt"): Permission.DECIDE,
    ("PUT", "/v1/org/prompt-cache"): Permission.ADMIN,
    ("POST", "/v1/org/resume"): Permission.DECIDE,
    ("POST", "/v1/org/roles/{role}/pause"): Permission.DECIDE,
    ("POST", "/v1/org/roles/{role}/resume"): Permission.DECIDE,
    ("GET", "/v1/org/breakers"): Permission.READ,
    ("POST", "/v1/org/breakers/{name}/reset"): Permission.DECIDE,
    ("POST", "/v1/org/breakers/{name}/disable"): Permission.DECIDE,
    ("POST", "/v1/org/breakers/{name}/enable"): Permission.DECIDE,
    ("GET", "/v1/runs/{run_id}/report"): Permission.READ,
    ("GET", "/v1/reports/downgrade"): Permission.READ,
    # cost views (ND61, ND62): read-only; a quote is advisory and writes nothing
    ("GET", "/v1/costs/calls"): Permission.READ,
    ("GET", "/v1/costs/per-result"): Permission.READ,
    ("GET", "/v1/costs/spend"): Permission.READ,
    ("GET", "/v1/savings/tokens"): Permission.READ,
    ("POST", "/v1/costs/quote"): Permission.READ,
    ("GET", "/v1/dashboards/verification"): Permission.READ,
    ("GET", "/v1/dashboards/verifiers"): Permission.READ,
    ("GET", "/v1/dashboards/cost-latency"): Permission.READ,
    ("GET", "/v1/dashboards/approvals"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/tasks"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/timeline"): Permission.READ,
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/audit"): Permission.READ,
    # ledger and live events
    ("GET", "/v1/ledger"): Permission.READ,
    ("GET", "/v1/ledger/verify"): Permission.READ,
    ("GET", "/v1/ledger/{stream}"): Permission.READ,
    ("GET", "/v1/ledger/{stream}/verify"): Permission.READ,
    (WS, "/v1/events"): Permission.READ,
    (WS, "/v1/runs/{run_id}/events"): Permission.READ,
    # approvals, content, budgets, agents
    ("GET", "/v1/approvals"): Permission.READ,
    ("GET", "/v1/approvals/{approval_id}"): Permission.READ,
    ("POST", "/v1/approvals/{approval_id}/decision"): Permission.DECIDE,
    ("POST", "/v1/approvals/{approval_id}/approve"): Permission.DECIDE,
    ("POST", "/v1/approvals/{approval_id}/reject"): Permission.DECIDE,
    # a person's decision on a human gate a durable run waits on (T81): a human session only
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision"): Permission.DECIDE,
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions"): Permission.DECIDE,
    ("GET", "/v1/content/{org_id}/{content_hash}"): Permission.REVEAL,
    ("GET", "/v1/budgets"): Permission.READ,
    ("GET", "/v1/budgets/{budget_id}"): Permission.READ,
    ("POST", "/v1/budgets/{budget_id}/unblock"): Permission.DECIDE,
    ("GET", "/v1/agents/{agent_id}/eligibility"): Permission.READ,
    ("GET", "/v1/verifiers/{agent_id}/calibration"): Permission.READ,
    # agent credentials (E2): an owner in a human session, never an agent or a token
    ("POST", "/v1/agents/{agent_id}/credentials"): Permission.ADMIN,
    ("GET", "/v1/agents/{agent_id}/credentials"): Permission.ADMIN,
    ("GET", "/v1/agents"): Permission.ADMIN,
    ("GET", "/v1/agent-credentials"): Permission.ADMIN,
    ("POST", "/v1/agents/{agent_id}/credentials/revoke-all"): Permission.ADMIN,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/revoke"): Permission.ADMIN,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/rotate"): Permission.ADMIN,
    # model catalog and assignments (read), their admin writes, and the keyless connections: an
    # owner in a human session (ND80 Q6, ND92)
    ("GET", "/v1/models/catalog"): Permission.READ,
    ("GET", "/v1/models/catalog/events"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/edit"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries/edit"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/approve"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries/approve"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/pause"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries/pause"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/retire"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries/retire"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/remove"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/entries/remove"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/prices"): Permission.ADMIN,
    ("POST", "/v1/models/catalog/regions"): Permission.ADMIN,
    ("PUT", "/v1/models/catalog/regions"): Permission.ADMIN,
    ("GET", "/v1/models/assignments"): Permission.READ,
    ("POST", "/v1/models/assignments"): Permission.ADMIN,
    ("PUT", "/v1/models/assignments/{scope}/{subject}"): Permission.ADMIN,
    ("GET", "/v1/models/resolution"): Permission.ADMIN,
    ("GET", "/v1/models/connections"): Permission.ADMIN,
    ("GET", "/v1/models/connections/events"): Permission.ADMIN,
    ("PUT", "/v1/models/connections/{provider}"): Permission.ADMIN,
    ("POST", "/v1/models/connections/{provider}/remove"): Permission.ADMIN,
    ("POST", "/v1/models/connections/{provider}/test"): Permission.ADMIN,
    ("PUT", "/v1/models/connections/{provider}/key"): Permission.ADMIN,
    ("DELETE", "/v1/models/connections/{provider}/key"): Permission.ADMIN,
    ("GET", "/v1/models/connections/{provider}/key"): Permission.READ,
    ("GET", "/v1/models/keys/health"): Permission.ADMIN,
    ("GET", "/v1/scorecards"): Permission.READ,
    ("GET", "/v1/autonomy/proposals"): Permission.DECIDE,
    ("GET", "/v1/autonomy/grants"): Permission.READ,
    ("POST", "/v1/autonomy/grants"): Permission.ADMIN,
    ("POST", "/v1/autonomy/grants/{grant_id}/revoke"): Permission.DECIDE,
    ("PUT", "/v1/autonomy/ceilings/{role}/{kind}"): Permission.DECIDE,
    ("PUT", "/v1/autonomy/settings/{name}"): Permission.ADMIN,
    ("PUT", "/v1/autonomy/model-change-floor/{role}"): Permission.ADMIN,
}
"""Every route's rule. The written table in the platform architecture guide is the same."""

AUTH_FIRST: Final = frozenset(
    (method, path)
    for (method, path), rule in ROUTE_RULES.items()
    if method in ("POST", "PUT", "PATCH", "DELETE") and rule != PUBLIC
)
"""Every non-public mutating route (R1.20b G1, derived from `ROUTE_RULES`): its caller is decided
before the body or its content type is looked at; the guard asks
`Authorizer.preflight` after the Host, TLS and Origin checks, so no credentials is 401 and a
non-human token 403 whatever the body (`{not json`, `text/plain`) is."""

DenialHook = Callable[[str, str, str, int, Mapping[str, str]], None]
"""A note of a refusal (ND72): `(org, actor, code, http, path_params)`, called for a
refusal of the route it was registered for, decided here before the route runs. Only for a signed-in
caller of a real organization refused with 403; it may write to the security log but never changes
the refusal, and a failure in it is ignored."""

PREFLIGHT_KEY: Final = "nuramesh.preflight"
"""The scope key `preflight` leaves its decision under. Only honoured for a route in `AUTH_FIRST`,
and not a key of `scope["state"]` (which an ASGI server's lifespan state can fill)."""

AGENT_ROUTES: Final = frozenset(
    {
        ("GET", "/v1/health"),
        ("GET", "/v1/agents/{agent_id}/eligibility"),
        ("GET", "/v1/runs"),
        ("GET", "/v1/runs/{run_id}"),
        ("GET", "/v1/runs/{run_id}/tasks"),
        ("GET", "/v1/runs/{run_id}/stop"),
        ("POST", "/v1/runs/{run_id}/stop"),
    }
)
"""The only routes an agent credential may call (E2 spec 6.2). Default deny: a route not here is
403 `agent_forbidden`. `route_matrix.AGENT_MATRIX` pins the same seven rows by hand and
`routes.check_tables` compares them; a new route is closed to agents until a row is added to both
in the same change."""

HUMAN_SESSION_ONLY: Final = frozenset({("POST", "/v1/runs")})
"""Routes whose permission an API token could hold (`run` scope) but that only a person signed in
may call (OD-2, R1.20): an API token of any scope gets 403 `human_required`, an agent credential
is refused by `AGENT_ROUTES`. The flag is per route, so no permission letter is invented."""

PERSON_SESSION_ONLY: Final = frozenset({("POST", "/v1/auth/password/confirm")})
"""Routes of rule `read` that only a person in a cookie session may call, whatever their role
(solo-approval rule SA-8): an API token and an agent credential get 403 `human_required`,
and with authentication off the answer is 403 `authentication_required` (there is no session to
act on, and the `dev_human` harness is not one). The flag is per route, as `HUMAN_SESSION_ONLY`."""

MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Caller:
    """Who is calling. `kind` is `session` (a person), `token` (a program), `dev` (the in-process
    harness person, authentication off) or `anonymous`."""

    kind: str
    actor: str | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    role: str | None = None
    token_id: str | None = None
    agent_id: str | None = None
    """For kind `agent`: the agent's configuration slug (the actor is `agent:<agent_id>`)."""
    credential_id: str | None = None
    session_id: str | None = None
    """For kind `session`: the id of the session the request came in on."""
    org_id: str | None = None
    """The caller's organization: the one their credential belongs to.
    `LOCAL_INSPECTOR` for an app with no organization (authentication off); None sees nothing."""

    @property
    def human(self) -> bool:
        """A person: a session or the dev harness, and a `human:` actor (checked again here)."""
        return (
            self.kind in ("session", "dev") and self.actor is not None and is_human_id(self.actor)
        )


ANONYMOUS = Caller("anonymous")


def rule_for(route_path: str, method: str) -> Rule | None:
    """The rule of a route, or None if the table has no row (refused)."""
    return ROUTE_RULES.get((method, route_path))


class Authorizer:
    """ASGI middleware: authenticate, then apply `ROUTE_RULES` to the matched route."""

    def __init__(
        self,
        app: Any,
        *,
        routes: Callable[[], list[Any]],
        required: bool,
        sessions: AuthService | None,
        tokens: TokenService | None,
        trusted_proxy: TrustedProxies | bool,
        dev_human: str | None = None,
        local_org: str | None = None,
        extra_rules: Mapping[tuple[str, str], Rule] | None = None,
        denial_hooks: Mapping[tuple[str, str], DenialHook] | None = None,
        agents: AgentAuth | None = None,
    ) -> None:
        if required and local_org is None:
            raise ValueError("authentication needs an organization")
        self.app = app
        self.local_org = local_org
        self._scope_org = LOCAL_INSPECTOR if local_org is None else local_org
        self.routes = routes
        self.required = required
        self.sessions, self.tokens, self.agents = sessions, tokens, agents
        self.trusted_proxy = trusted_proxy
        self.dev_human = dev_human
        self.rules: dict[tuple[str, str], Rule] = {**ROUTE_RULES, **(extra_rules or {})}
        self.denial_hooks = dict(denial_hooks or {})

    def _match(self, scope: Any) -> tuple[Any, str] | None:
        """The fully matched route and the table's method name, or None (404/405 follow)."""
        method = WS if scope["type"] == "websocket" else scope["method"]
        for route in self.routes():
            found, _ = route.matches(scope)
            if found is Match.FULL:
                return route, method
        return None

    async def preflight(self, scope: Any) -> AuthProblem | None:
        """For a route in `AUTH_FIRST`: decide the caller now (remembered on the scope so the
        authorizer does not decide twice) and return the refusal, or None to go on."""
        matched = self._match(scope)
        if matched is None or (matched[1], matched[0].path) not in AUTH_FIRST:
            return None
        route, method = matched
        try:
            caller = await anyio.to_thread.run_sync(
                self._decide, scope, method, self.rules.get((method, route.path))
            )
        except AuthProblem as exc:
            return exc
        except StoreBusyError:
            return AuthProblem(503, "store_busy", "the database is busy; retry")
        scope[PREFLIGHT_KEY] = caller  # a key of the scope itself, not of the shared `state`
        scope.setdefault("state", {})["caller"] = caller
        return None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        matched = self._match(scope)
        early = scope.get(PREFLIGHT_KEY)
        if (
            isinstance(early, Caller)
            and matched is not None
            and (matched[1], matched[0].path) in AUTH_FIRST
        ):
            await self.app(scope, receive, send)  # decided early by `preflight` for this route
            return
        if matched is None:
            await self.app(scope, receive, send)  # no such route: the router answers 404 or 405
            return
        route, method = matched
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            # a route that cannot be read (for example an included-router wrapper) is refused
            await self._refuse(
                scope,
                receive,
                send,
                AuthProblem(403, "no_rule", "this route has no authorization rule; refused"),
            )
            return
        rule = self.rules.get((method, path))
        try:
            caller = await anyio.to_thread.run_sync(self._decide, scope, method, rule)
        except AuthProblem as exc:
            hook = self.denial_hooks.get((method, path))
            if hook is not None:
                await anyio.to_thread.run_sync(self._note_denial, hook, route, scope, exc)
            await self._refuse(scope, receive, send, exc)
            return
        except StoreBusyError:
            await self._refuse(
                scope, receive, send, AuthProblem(503, "store_busy", "the database is busy; retry")
            )
            return
        state = scope.setdefault("state", {})
        state["caller"] = caller
        if scope["type"] == "websocket" and self.required:
            # a stream outlives its handshake: the stream asks again on every poll tick
            state["recheck"] = lambda: self._decide(scope, method, rule)
        await self.app(scope, receive, send)

    def _note_denial(self, hook: DenialHook, route: Any, scope: Any, exc: AuthProblem) -> None:
        """Tell a route's denial hook who was refused. A failure here never changes the refusal."""
        who = exc.caller
        if (
            exc.status != 403
            or exc.code not in ("forbidden", "human_required")
            or not isinstance(who, Caller)
            or who.actor is None
            or who.org_id is None
            or who.org_id == LOCAL_INSPECTOR
        ):
            return
        try:
            _, child = route.matches(scope)
            params = child.get("path_params", {})
            hook(
                who.org_id, who.actor, exc.code, exc.status, {k: str(v) for k, v in params.items()}
            )
        except Exception:  # noqa: S110 - a failed log write never changes a refusal
            pass

    async def _refuse(self, scope: Any, receive: Any, send: Any, exc: AuthProblem) -> None:
        if scope["type"] == "websocket":
            # refused before accepting; a busy store is transient (1013, retry), not a refusal
            code = 1013 if exc.code == "store_busy" else 1008
            await send({"type": "websocket.close", "code": code})
            return
        await problem_response(exc)(scope, receive, send)

    # -- the decision (runs in a worker thread: it reads the store) ---------------------------

    def _decide(self, scope: Any, method: str, rule: Rule | None) -> Caller:
        if rule is None:
            raise AuthProblem(403, "no_rule", "this route has no authorization rule; refused")
        person_only = False
        if rule is Permission.READ and method in MUTATIONS:  # only then can the flag apply
            matched = self._match(scope)
            person_only = (
                matched is not None and (method, str(matched[0].path)) in PERSON_SESSION_ONLY
            )
        if not self.required:
            if person_only:
                raise AuthProblem(
                    403,
                    "authentication_required",
                    "this needs a signed-in person: start the server with --auth (R1.22)",
                )
            return self._local(rule)
        if rule == PUBLIC:
            matched = self._match(scope)
            if matched is not None and (method, str(matched[0].path)) in OPTIONAL_AUTH:
                return self._known_or_anonymous(scope, method)
            return ANONYMOUS
        caller = self._authenticate(scope, method)
        assert isinstance(rule, Permission)
        if person_only and caller.kind != "session":
            problem = AuthProblem(
                403,
                "human_required",
                "this needs a person signed in; tokens and agent credentials can never do it",
            )
            problem.caller = caller
            raise problem
        if caller.kind == "agent":
            matched = self._match(scope)  # only an agent needs the route's path template
            path = "" if matched is None else str(matched[0].path)
            return self._decide_agent(caller, method, rule, path)
        if caller.kind == "token":
            matched = self._match(scope)
            if matched is not None and (method, str(matched[0].path)) in HUMAN_SESSION_ONLY:
                raise AuthProblem(
                    403,
                    "human_required",
                    "this needs a person signed in; API tokens can never do it",
                )
        if rule in caller.permissions:
            return caller
        problem = AuthProblem(403, "forbidden", "your role doesn't allow this")
        if caller.kind == "token":
            if rule in HUMAN_ONLY:
                problem = AuthProblem(
                    403,
                    "human_required",
                    "this needs a person signed in; API tokens can never do it",
                )
            else:
                problem = AuthProblem(403, "forbidden", "this token's scopes don't allow this")
        problem.caller = caller
        raise problem

    def _known_or_anonymous(self, scope: Any, method: str) -> Caller:
        """A public route that treats a signed-in caller differently (the health throttle): the
        caller a valid API token or session cookie names, else anonymous. A bad credential is
        anonymous, never an error, and an agent credential is not presented here (it would spend
        its call cap on a poll)."""
        conn = HTTPConnection(scope)
        authorization = conn.headers.get("authorization")
        if authorization is not None:
            if authorization.partition(" ")[2].strip().startswith(AGENT_CREDENTIAL_PREFIX):
                return ANONYMOUS
        else:
            secure = is_secure(conn, trusted_proxy=self.trusted_proxy)
            if cookie_names(secure)[0] not in conn.cookies:
                return ANONYMOUS
        try:
            return self._authenticate(scope, method)
        except AuthProblem:
            return ANONYMOUS

    def _decide_agent(self, caller: Caller, method: str, rule: Permission, path: str) -> Caller:
        """The checks of spec 6.1 for an agent credential, after authentication and the cap:
        human-only rules, scopes, the allow-list, then the kill switch for `run`."""
        if rule in HUMAN_ONLY:
            raise AuthProblem(
                403,
                "human_required",
                "this needs a person signed in; agent credentials can never do it",
            )
        if rule not in caller.permissions:
            raise AuthProblem(403, "forbidden", "this credential's scopes don't allow this")
        if (method, path) not in AGENT_ROUTES:
            raise AuthProblem(403, "agent_forbidden", "this route is not open to agent credentials")
        if rule is Permission.RUN:
            assert self.agents is not None and caller.agent_id is not None
            if self.agents.service.suspended_now(caller.agent_id):
                raise AuthProblem(
                    403,
                    "agent_suspended",
                    "the organization is halted or this agent's role is paused",
                )
        return caller

    def _local(self, rule: Rule) -> Caller:
        """Authentication off (loopback only): reads open, actions only through `dev_human`."""
        if rule == PUBLIC or rule is Permission.READ:
            return Caller("anonymous", org_id=self._scope_org)
        if isinstance(rule, Permission) and rule is not Permission.ADMIN and self.dev_human:
            return Caller(
                "dev", actor=self.dev_human, permissions=frozenset({rule}), org_id=self._scope_org
            )
        raise AuthProblem(
            403,
            "authentication_required",
            "this needs a signed-in person; sign-in is off because the server was started with "
            "--auth off: restart it without that flag",
        )

    def _agent_caller(self, presented: str, client: str) -> Caller:
        assert self.agents is not None
        found = self.agents.authenticate(presented, client)
        known = self.agents.service.agents[found.agent_id]
        return Caller(
            "agent",
            actor=found.actor,
            permissions=scope_permissions(found.scopes),
            role=known.role,
            agent_id=found.agent_id,
            credential_id=found.id,
            org_id=self.agents.service.org_id,
        )

    def _authenticate(self, scope: Any, method: str) -> Caller:
        conn = HTTPConnection(scope)
        headers = conn.headers
        authorization = headers.get("authorization")
        client = client_address(conn, trusted_proxy=self.trusted_proxy)
        if authorization is not None:
            kind, _, value = authorization.partition(" ")
            if kind.lower() != "bearer" or not value.strip() or self.tokens is None:
                raise AuthProblem(
                    401,
                    "token_invalid",
                    "send an API token as Authorization: Bearer <token>",
                    {"WWW-Authenticate": 'Bearer realm="nuramesh"'},
                )
            if value.strip().startswith(AGENT_CREDENTIAL_PREFIX) and self.agents is not None:
                return self._agent_caller(value.strip(), client)
            token, rights = self.tokens.authenticate_with_rights(value.strip(), client)
            return Caller(
                "token",
                actor=token.actor,
                permissions=rights,
                token_id=token.id,
                org_id=self.tokens.org_id,
            )
        secure = is_secure(conn, trusted_proxy=self.trusted_proxy)
        access_name, refresh_name, _ = cookie_names(secure)
        cookies = conn.cookies
        access = cookies.get(access_name)
        if access is None or self.sessions is None:
            raise AuthProblem(
                401,
                "authentication_required",
                "sign in, or send an API token",
                {"WWW-Authenticate": 'Bearer realm="nuramesh"'},
            )
        principal = self.sessions.authenticate(access)
        if method in MUTATIONS:
            self.sessions.require_csrf(
                cookies.get(refresh_name),
                headers.get(HEADER_CSRF),
                session_id=principal.session.id,
            )
        user = principal.user
        return Caller(
            "session",
            actor=user.id,
            permissions=ROLE_PERMISSIONS[user.role],
            role=user.role.value,
            session_id=principal.session.id,
            org_id=self.sessions.org_id,
        )
