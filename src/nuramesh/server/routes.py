"""The route table the API declares, and the check that the app serves exactly that table.

`ROUTE_SCOPES` gives every route of the app one **class** (`spec/auth/org-scope.md` section 4):
how its data is tied to the caller's organization. `authz.ROUTE_RULES` gives the same routes
their permission. `route_matrix.MATRIX` is a third, hand-written table (permission letter and
class per route) that `tests/server/test_authz_matrix.py` calls every route against.

`check_route_table` is the gate (section 6 of the spec), run by `create_app` for **every** app,
whatever its host configuration: it reads the routes the app really serves and refuses to go on if
one is not declared, one is declared but not served, one cannot be read at all, or the three
tables disagree on a key, a permission or a class. A route it cannot read is an error on purpose:
the authorizer and this check could not tell what such a route is.
"""

from collections.abc import Mapping
from typing import Any, Final

from nuramesh.core.auth import Permission
from nuramesh.core.idempotency import (
    EXCLUDED_READONLY,
    EXCLUDED_SECRET,
    IGNORED,
    Delivery,
    RouteMode,
    honored,
)
from nuramesh.server.authz import AGENT_ROUTES, AUTH_FIRST, ROUTE_RULES, WS
from nuramesh.server.authz import PUBLIC as PUBLIC_RULE
from nuramesh.server.operation_ids import OPERATION_IDS, is_well_formed
from nuramesh.server.route_matrix import AGENT_MATRIX, MATRIX

PUBLIC: Final = "public"
SERVER: Final = "server"
RUN: Final = "run"
TASK: Final = "task"
STREAM: Final = "stream"
APPROVAL: Final = "approval"
BUDGET: Final = "budget"
LIST: Final = "list"
LIVE: Final = "live"
AGENT: Final = "agent"
CREDENTIAL: Final = "credential"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/v1/health"): PUBLIC,
    ("GET", "/v1/openapi.json"): SERVER,
    ("HEAD", "/v1/openapi.json"): SERVER,
    ("GET", "/v1/docs"): SERVER,
    ("HEAD", "/v1/docs"): SERVER,
    ("GET", "/v1/schema/config"): SERVER,
    ("POST", "/v1/config/validate"): SERVER,
    ("POST", "/v1/auth/login"): PUBLIC,
    ("POST", "/v1/auth/login/mfa"): PUBLIC,
    ("POST", "/v1/auth/login/mfa/enroll"): PUBLIC,
    ("POST", "/v1/auth/login/mfa/enroll/confirm"): PUBLIC,
    ("POST", "/v1/auth/refresh"): PUBLIC,
    ("POST", "/v1/auth/logout"): PUBLIC,
    ("GET", "/v1/auth/session"): PUBLIC,
    ("GET", "/v1/auth/setup/status"): PUBLIC,
    ("POST", "/v1/auth/setup/redeem"): PUBLIC,
    ("GET", "/setup"): PUBLIC,
    ("HEAD", "/setup"): PUBLIC,
    ("GET", "/v1/auth/tokens"): SERVER,
    ("POST", "/v1/auth/tokens"): SERVER,
    ("POST", "/v1/auth/tokens/{token_id}/revoke"): SERVER,
    ("PATCH", "/v1/auth/tokens/{token_id}"): SERVER,
    ("DELETE", "/v1/auth/tokens/{token_id}"): SERVER,
    ("GET", "/v1/auth/mfa"): SERVER,
    ("POST", "/v1/auth/mfa/enroll"): SERVER,
    ("POST", "/v1/auth/mfa/enroll/confirm"): SERVER,
    ("POST", "/v1/auth/mfa/recovery-codes"): SERVER,
    ("POST", "/v1/auth/mfa/disable"): SERVER,
    ("POST", "/v1/auth/password/confirm"): SERVER,
    ("POST", "/v1/auth/users/{user_id}/mfa/reset"): SERVER,
    ("GET", "/v1/auth/settings"): SERVER,
    ("PUT", "/v1/auth/settings/mfa"): SERVER,
    ("GET", "/v1/auth/email/status"): SERVER,
    ("GET", "/v1/auth/users"): SERVER,
    ("GET", "/v1/auth/users/{user_id}/agent-credentials"): SERVER,
    ("POST", "/v1/auth/users/{user_id}/disable"): SERVER,
    ("POST", "/v1/auth/users/{user_id}/enable"): SERVER,
    ("PUT", "/v1/auth/users/{user_id}/role"): SERVER,
    ("GET", "/v1/auth/invites"): SERVER,
    ("POST", "/v1/auth/invites"): SERVER,
    ("POST", "/v1/auth/invites/{invite_id}/revoke"): SERVER,
    ("POST", "/v1/auth/invites/{invite_id}/resend"): SERVER,
    ("POST", "/v1/auth/invites/inspect"): PUBLIC,
    ("POST", "/v1/auth/invites/accept"): PUBLIC,
    ("POST", "/v1/auth/me/email"): SERVER,
    ("POST", "/v1/auth/email/verify"): PUBLIC,
    ("POST", "/v1/auth/password-reset/request"): PUBLIC,
    ("POST", "/v1/auth/password-reset/redeem"): PUBLIC,
    ("POST", "/v1/auth/users/{user_id}/password-reset"): SERVER,
    ("POST", "/v1/auth/tokens/revoke-by-link"): SERVER,
    ("GET", "/v1/runs"): LIST,
    ("GET", "/v1/runs/{run_id}"): RUN,
    ("GET", "/v1/runs/{run_id}/ledger"): RUN,
    ("GET", "/v1/runs/{run_id}/ledger/verify"): RUN,
    ("GET", "/v1/runs/{run_id}/stop"): RUN,
    ("POST", "/v1/runs"): SERVER,
    ("POST", "/v1/runs/{run_id}/stop"): RUN,
    ("GET", "/v1/org/halt"): SERVER,
    ("GET", "/v1/org/status"): SERVER,
    ("POST", "/v1/org/halt"): SERVER,
    ("PUT", "/v1/org/prompt-cache"): SERVER,
    ("POST", "/v1/org/resume"): SERVER,
    ("POST", "/v1/org/roles/{role}/pause"): SERVER,
    ("POST", "/v1/org/roles/{role}/resume"): SERVER,
    ("GET", "/v1/org/breakers"): SERVER,
    ("POST", "/v1/org/breakers/{name}/reset"): SERVER,
    ("POST", "/v1/org/breakers/{name}/disable"): SERVER,
    ("POST", "/v1/org/breakers/{name}/enable"): SERVER,
    ("GET", "/v1/runs/{run_id}/report"): RUN,
    ("GET", "/v1/reports/downgrade"): SERVER,
    ("GET", "/v1/costs/calls"): SERVER,
    ("GET", "/v1/costs/per-result"): SERVER,
    ("GET", "/v1/costs/spend"): SERVER,
    ("GET", "/v1/savings/tokens"): SERVER,
    ("POST", "/v1/costs/quote"): SERVER,
    ("GET", "/v1/dashboards/verification"): SERVER,
    ("GET", "/v1/dashboards/verifiers"): SERVER,
    ("GET", "/v1/dashboards/cost-latency"): SERVER,
    ("GET", "/v1/dashboards/approvals"): SERVER,
    ("GET", "/v1/runs/{run_id}/tasks"): RUN,
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/timeline"): TASK,
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/audit"): TASK,
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision"): TASK,
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions"): TASK,
    ("GET", "/v1/ledger"): LIST,
    ("GET", "/v1/ledger/verify"): LIST,
    ("GET", "/v1/ledger/{stream}"): STREAM,
    ("GET", "/v1/ledger/{stream}/verify"): STREAM,
    (WS, "/v1/events"): LIVE,
    (WS, "/v1/runs/{run_id}/events"): LIVE,
    ("GET", "/v1/approvals"): LIST,
    ("GET", "/v1/approvals/{approval_id}"): APPROVAL,
    ("POST", "/v1/approvals/{approval_id}/decision"): APPROVAL,
    ("POST", "/v1/approvals/{approval_id}/approve"): APPROVAL,
    ("POST", "/v1/approvals/{approval_id}/reject"): APPROVAL,
    ("GET", "/v1/content/{org_id}/{content_hash}"): SERVER,
    ("GET", "/v1/budgets"): LIST,
    ("GET", "/v1/budgets/{budget_id}"): BUDGET,
    ("POST", "/v1/budgets/{budget_id}/unblock"): BUDGET,
    ("GET", "/v1/agents/{agent_id}/eligibility"): SERVER,
    ("GET", "/v1/verifiers/{agent_id}/calibration"): SERVER,
    ("POST", "/v1/agents/{agent_id}/credentials"): AGENT,
    ("GET", "/v1/agents/{agent_id}/credentials"): AGENT,
    ("GET", "/v1/agents"): LIST,
    ("GET", "/v1/agent-credentials"): LIST,
    ("POST", "/v1/agents/{agent_id}/credentials/revoke-all"): AGENT,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/revoke"): CREDENTIAL,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/rotate"): CREDENTIAL,
    ("GET", "/v1/models/catalog"): SERVER,
    ("GET", "/v1/models/catalog/events"): SERVER,
    ("POST", "/v1/models/catalog/entries"): SERVER,
    ("POST", "/v1/models/catalog/edit"): SERVER,
    ("POST", "/v1/models/catalog/entries/edit"): SERVER,
    ("POST", "/v1/models/catalog/approve"): SERVER,
    ("POST", "/v1/models/catalog/entries/approve"): SERVER,
    ("POST", "/v1/models/catalog/pause"): SERVER,
    ("POST", "/v1/models/catalog/entries/pause"): SERVER,
    ("POST", "/v1/models/catalog/retire"): SERVER,
    ("POST", "/v1/models/catalog/entries/retire"): SERVER,
    ("POST", "/v1/models/catalog/remove"): SERVER,
    ("POST", "/v1/models/catalog/entries/remove"): SERVER,
    ("POST", "/v1/models/catalog/prices"): SERVER,
    ("POST", "/v1/models/catalog/regions"): SERVER,
    ("PUT", "/v1/models/catalog/regions"): SERVER,
    ("GET", "/v1/models/assignments"): SERVER,
    ("POST", "/v1/models/assignments"): SERVER,
    ("PUT", "/v1/models/assignments/{scope}/{subject}"): SERVER,
    ("GET", "/v1/models/resolution"): SERVER,
    ("GET", "/v1/models/connections"): SERVER,
    ("GET", "/v1/models/connections/events"): SERVER,
    ("PUT", "/v1/models/connections/{provider}"): SERVER,
    ("POST", "/v1/models/connections/{provider}/remove"): SERVER,
    ("POST", "/v1/models/connections/{provider}/test"): SERVER,
    ("PUT", "/v1/models/connections/{provider}/key"): SERVER,
    ("DELETE", "/v1/models/connections/{provider}/key"): SERVER,
    ("GET", "/v1/models/connections/{provider}/key"): SERVER,
    ("GET", "/v1/models/keys/health"): SERVER,
    ("GET", "/v1/scorecards"): SERVER,
    ("GET", "/v1/autonomy/proposals"): SERVER,
    ("GET", "/v1/autonomy/grants"): SERVER,
    ("POST", "/v1/autonomy/grants"): SERVER,
    ("POST", "/v1/autonomy/grants/{grant_id}/revoke"): SERVER,
    ("PUT", "/v1/autonomy/ceilings/{role}/{kind}"): SERVER,
    ("PUT", "/v1/autonomy/settings/{name}"): SERVER,
    ("PUT", "/v1/autonomy/model-change-floor/{role}"): SERVER,
}
"""Every route and how it is tied to the caller's organization (spec/auth/org-scope.md)."""

OPTIONAL: Final = frozenset({("GET", "/v1/docs"), ("HEAD", "/v1/docs")})
"""Declared routes an app may legitimately not serve: the docs page exists only where no person
can sign in (no organization, no `dev_human`, no authentication)."""


class RouteTableError(ValueError):
    """The app's routes are not the declared routes; a network server must not start."""


def served_routes(app: Any) -> set[tuple[str, str]]:
    """Every `(method, path template)` the app serves, WebSockets as `WS`.

    Raises `RouteTableError` for a route it cannot read: no string `path`, or neither a method
    set nor a WebSocket (for example the object FastAPI 0.161 makes for `include_router`)."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            raise RouteTableError(
                f"a route of the app cannot be read ({type(route).__name__}: no path); register "
                "routes on the app itself, not with include_router"
            )
        methods = getattr(route, "methods", None)
        if type(route).__name__ in ("WebSocketRoute", "APIWebSocketRoute"):
            found.add((WS, path))
        elif methods:
            found.update((method, path) for method in methods)
        else:
            raise RouteTableError(
                f"route {path!r} cannot be read ({type(route).__name__}: no methods)"
            )
    return found


def declared_routes() -> set[tuple[str, str]]:
    """The routes declared by both tables (they must have the same keys)."""
    return set(ROUTE_SCOPES) & set(ROUTE_RULES)


LETTER: Final = {
    PUBLIC_RULE: "P",
    Permission.READ: "R",
    Permission.RUN: "U",
    Permission.DECIDE: "D",
    Permission.REVEAL: "V",
    Permission.ADMIN: "A",
}
"""The letter of each rule, as `route_matrix.MATRIX` writes it."""


IDEMPOTENCY_ROUTES: dict[tuple[str, str], RouteMode] = {
    ("POST", "/v1/config/validate"): EXCLUDED_READONLY,
    ("POST", "/v1/costs/quote"): EXCLUDED_READONLY,
    ("POST", "/v1/auth/login"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/login/mfa"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/login/mfa/enroll"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/login/mfa/enroll/confirm"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/refresh"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/logout"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/setup/redeem"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/tokens"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/tokens/{token_id}/revoke"): honored(Delivery.EXACTLY_ONCE),
    ("PATCH", "/v1/auth/tokens/{token_id}"): honored(Delivery.EXACTLY_ONCE),
    ("DELETE", "/v1/auth/tokens/{token_id}"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/auth/users/{user_id}/disable"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/auth/users/{user_id}/enable"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/auth/users/{user_id}/role"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/auth/mfa/enroll"): IGNORED,
    ("POST", "/v1/auth/mfa/enroll/confirm"): IGNORED,
    ("POST", "/v1/auth/mfa/recovery-codes"): IGNORED,
    ("POST", "/v1/auth/mfa/disable"): IGNORED,
    # SA-8: the body holds a password; it is never stored or replayed
    ("POST", "/v1/auth/password/confirm"): IGNORED,
    ("POST", "/v1/auth/users/{user_id}/mfa/reset"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/auth/settings/mfa"): honored(Delivery.EXACTLY_ONCE),
    # a mail goes out and a link is made: neither can be replayed from a stored answer
    ("POST", "/v1/auth/invites"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/invites/{invite_id}/resend"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/invites/{invite_id}/revoke"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/auth/invites/inspect"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/invites/accept"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/me/email"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/email/verify"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/password-reset/request"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/password-reset/redeem"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/users/{user_id}/password-reset"): EXCLUDED_SECRET,
    ("POST", "/v1/auth/tokens/revoke-by-link"): EXCLUDED_SECRET,
    ("POST", "/v1/agents/{agent_id}/credentials"): EXCLUDED_SECRET,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/rotate"): EXCLUDED_SECRET,
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/revoke"): honored(
        Delivery.EXACTLY_ONCE
    ),
    ("POST", "/v1/agents/{agent_id}/credentials/revoke-all"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/runs"): honored(Delivery.AT_LEAST_ONCE),
    ("POST", "/v1/runs/{run_id}/stop"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/approvals/{approval_id}/decision"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/approvals/{approval_id}/approve"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/approvals/{approval_id}/reject"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/budgets/{budget_id}/unblock"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/resume"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/roles/{role}/resume"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/breakers/{name}/reset"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/breakers/{name}/disable"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/breakers/{name}/enable"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/edit"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries/edit"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/approve"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries/approve"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/pause"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries/pause"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/retire"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries/retire"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/remove"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/entries/remove"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/prices"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/catalog/regions"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/models/catalog/regions"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/assignments"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/models/assignments/{scope}/{subject}"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/models/connections/{provider}/remove"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/models/connections/{provider}"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision"): honored(
        Delivery.AT_LEAST_ONCE
    ),
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions"): honored(
        Delivery.AT_LEAST_ONCE
    ),
    ("POST", "/v1/org/halt"): honored(Delivery.AT_LEAST_ONCE),
    ("PUT", "/v1/org/prompt-cache"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/org/roles/{role}/pause"): honored(Delivery.AT_LEAST_ONCE),
    ("POST", "/v1/models/connections/{provider}/test"): honored(Delivery.AT_LEAST_ONCE),
    # the body of the write holds a key: never stored for replay; a revoke is idempotent itself
    ("PUT", "/v1/models/connections/{provider}/key"): EXCLUDED_SECRET,
    ("DELETE", "/v1/models/connections/{provider}/key"): IGNORED,
    # E13: one store transaction each, no outbound call (R1.35 section 6); the `_in` variants run
    # inside the idempotency effect
    ("POST", "/v1/autonomy/grants"): honored(Delivery.EXACTLY_ONCE),
    ("POST", "/v1/autonomy/grants/{grant_id}/revoke"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/autonomy/ceilings/{role}/{kind}"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/autonomy/settings/{name}"): honored(Delivery.EXACTLY_ONCE),
    ("PUT", "/v1/autonomy/model-change-floor/{role}"): honored(Delivery.EXACTLY_ONCE),
}
"""Every mutating route and how it treats `Idempotency-Key` (R1.35 section 8)."""

MUTATING: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def check_tables() -> None:
    """Raise `RouteTableError` unless `ROUTE_RULES`, `ROUTE_SCOPES` and the written matrix
    (`route_matrix.MATRIX`) have the same routes, and the matrix's permission letter and class
    are the ones the other two declare."""
    if ROUTE_SCOPES.keys() != ROUTE_RULES.keys():
        both = ROUTE_SCOPES.keys() ^ ROUTE_RULES.keys()
        raise RouteTableError(f"ROUTE_SCOPES and ROUTE_RULES disagree on: {sorted(both)}")
    if MATRIX.keys() != ROUTE_RULES.keys():
        both = MATRIX.keys() ^ ROUTE_RULES.keys()
        raise RouteTableError(f"the written matrix and ROUTE_RULES disagree on: {sorted(both)}")
    if AGENT_MATRIX.keys() != AGENT_ROUTES:
        both = AGENT_MATRIX.keys() ^ AGENT_ROUTES
        raise RouteTableError(f"the agent matrix and AGENT_ROUTES disagree on: {sorted(both)}")
    unknown = sorted(k for k in AGENT_MATRIX if k not in ROUTE_RULES)
    bad_letter = sorted(
        k
        for k, (letter, _) in AGENT_MATRIX.items()
        if k not in ROUTE_RULES or LETTER.get(ROUTE_RULES[k]) != letter
    )
    if unknown or bad_letter:
        raise RouteTableError(
            f"the agent matrix names routes the app does not declare ({unknown}) or a "
            f"permission that differs from ROUTE_RULES ({bad_letter})"
        )
    decided_first = {
        key
        for key, (letter, _) in MATRIX.items()
        if key[0] in ("POST", "PUT", "PATCH", "DELETE") and letter != "P"
    }
    if AUTH_FIRST != decided_first:
        gap = AUTH_FIRST ^ decided_first
        raise RouteTableError(
            f"AUTH_FIRST and the non-public mutating rows of the written matrix disagree on: "
            f"{sorted(gap)}"
        )
    mutating = {k for k in ROUTE_RULES if k[0] in MUTATING}
    if IDEMPOTENCY_ROUTES.keys() != mutating:
        both = IDEMPOTENCY_ROUTES.keys() ^ mutating
        raise RouteTableError(
            f"IDEMPOTENCY_ROUTES and the mutating ROUTE_RULES disagree on: {sorted(both)}"
        )
    wrong = sorted(
        key
        for key, (letter, cls) in MATRIX.items()
        if LETTER.get(ROUTE_RULES[key]) != letter or ROUTE_SCOPES[key] != cls
    )
    if wrong:
        raise RouteTableError(
            f"the written matrix differs from ROUTE_RULES or ROUTE_SCOPES (permission or class) "
            f"on: {wrong}"
        )


ROUTE_CLASSES: Final = frozenset(
    {PUBLIC, SERVER, RUN, TASK, STREAM, APPROVAL, BUDGET, LIST, LIVE, AGENT, CREDENTIAL}
)
"""The route classes above; a plugin's route names one of them."""


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _matches_literal(template: str, literal: str) -> bool:
    """Whether Starlette's own compiled pattern for `template` accepts the parameter-free path."""
    from starlette.routing import compile_path

    regex, _, _ = compile_path(template)
    return regex.match(literal) is not None


def paths_overlap(a: str, b: str) -> bool:
    """True if some request path can match both templates (`/v1/ledger/{x}` and
    `/v1/ledger/verify` do; `{p:path}` matches the rest of the path). Any segment with a `{` is a
    wildcard (Starlette compiles `a{x}` to `a[^/]+`, so a part of a segment is never a literal),
    and each template's compiled Starlette pattern is also tried on the other's literal path."""
    if "{" not in b and _matches_literal(a, b):
        return True
    if "{" not in a and _matches_literal(b, a):
        return True
    left, right = _segments(a), _segments(b)
    for i in range(max(len(left), len(right))):
        sa = left[i] if i < len(left) else None
        sb = right[i] if i < len(right) else None
        for mine in (sa, sb):
            if mine is not None and mine.startswith("{") and mine.endswith(":path}"):
                return True  # swallows whatever remains
        if sa is None or sb is None:
            return False
        if "{" in sa or "{" in sb:
            continue
        if sa != sb:
            return False
    return True


def check_plugin_operation_ids(app: Any, extra_scopes: Mapping[tuple[str, str], str]) -> None:
    """A plugin route names its own operation id (`operation_id=`), which skips the core table and
    its grammar check, so check it here: verb-first snake_case, not an id the core table uses, and
    not shared by two plugin routes (the SDK would get one method for two operations)."""
    core = set(OPERATION_IDS.values())
    seen: dict[str, tuple[str, str]] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None) or ()
        path = str(getattr(route, "path", ""))
        keys = [(m, path) for m in sorted(methods) if (m, path) in extra_scopes]
        if not keys:
            continue
        operation_id = getattr(route, "unique_id", None)
        if not isinstance(operation_id, str) or not is_well_formed(operation_id):
            raise RouteTableError(
                f"plugin route {keys[0]} has an operation id that is not verb-first snake_case: "
                f"{operation_id!r}; pass operation_id= when you declare the route"
            )
        if operation_id in core:
            raise RouteTableError(
                f"plugin route {keys[0]} uses the core operation id {operation_id!r}; "
                "pass a different operation_id="
            )
        if operation_id in seen:
            raise RouteTableError(
                f"operation id {operation_id!r} is used by plugin routes {seen[operation_id]} "
                f"and {keys[0]}; pass a distinct operation_id="
            )
        seen[operation_id] = keys[0]


def check_route_table(
    app: Any,
    extra_rules: Mapping[tuple[str, str], Any] | None = None,
    extra_scopes: Mapping[tuple[str, str], str] | None = None,
) -> None:
    """Raise `RouteTableError` unless the app serves exactly the declared routes and the declared
    tables are the written matrix. `extra_rules` and `extra_scopes` are the routes installed
    plugins add (ND72): the open tables stay as they are, and a plugin's two tables must name the
    same routes, none of them an open route."""
    check_tables()
    extra_rules, extra_scopes = extra_rules or {}, extra_scopes or {}
    if extra_rules.keys() != extra_scopes.keys():
        both = extra_rules.keys() ^ extra_scopes.keys()
        raise RouteTableError(f"the plugin rules and scopes disagree on: {sorted(both)}")
    if extra_scopes.keys() & ROUTE_SCOPES.keys():
        raise RouteTableError(
            f"a plugin redeclares: {sorted(extra_scopes.keys() & ROUTE_SCOPES.keys())}"
        )
    unknown_class = sorted({c for c in extra_scopes.values() if c not in ROUTE_CLASSES})
    if unknown_class:
        raise RouteTableError(f"a plugin names an unknown route class: {unknown_class}")
    open_paths = {path for _, path in ROUTE_SCOPES}
    shadows = sorted(
        (method, path)
        for method, path in extra_scopes
        if any(paths_overlap(path, other) for other in open_paths)
    )
    if shadows:
        raise RouteTableError(f"a plugin route can match an open route's path: {shadows}")
    check_plugin_operation_ids(app, extra_scopes)
    declared = ROUTE_SCOPES.keys() | extra_scopes.keys()
    served = served_routes(app)
    undeclared = served - declared
    missing = declared - served - OPTIONAL
    problems = []
    if undeclared:
        problems.append(f"served but not declared: {sorted(undeclared)}")
    if missing:
        problems.append(f"declared but not served: {sorted(missing)}")
    if problems:
        raise RouteTableError(
            "the app's routes are not the declared routes; " + "; ".join(problems)
        )
