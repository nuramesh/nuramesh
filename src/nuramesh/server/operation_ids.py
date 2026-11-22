"""The operation id of every route, and so the name of every SDK method.

One naming rule across the whole API and SDK (`docs/sdk-typescript.md`, the compat note in
`docs/upgrade-notes.md`): the operation id is **verb-first snake_case**, `<verb>_<noun phrase>`,
the verb one of `VERBS`, and the TypeScript SDK method is its lowerCamelCase
(`set_org_prompt_cache` -> `setOrgPromptCache`). The noun phrase names the resource as a
developer says it (singular for one item, plural for a collection, the parent kept where it
reads naturally: `listRunTasks`, `revokeAgentCredential`); a deprecated twin of a renamed route
ends in `_deprecated` and the generator tags its method `@deprecated`.

This table is the only source: FastAPI asks `operation_id_for` for every route in the schema
(`create_app` installs it as `generate_unique_id_function`), so a route without a row cannot be
served (`UnnamedRouteError`), and `tests/test_sdk_names.py` checks the grammar, the generated
`apps/sdk-ts/src/generated.ts` and the rename table in the compat note against it.
"""

import re
from typing import Any, Final

ID_GRAMMAR: Final = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$|^[a-z][a-z0-9]*$")
"""snake_case: lower-case words joined by single underscores (a bare verb is allowed: `login`)."""

VERBS: Final[frozenset[str]] = frozenset(
    {
        # reads
        "get",  # one resource, or one computed view (a status, a report, a dashboard)
        "list",  # a collection (plural noun)
        # writes
        "create",  # POST that makes a new resource
        "set",  # PUT: the whole value, idempotent
        "update",  # change fields of an existing resource
        "delete",  # DELETE, or the POST `.../remove` actions
        # actions (the last path segment, or the route's own verb)
        "start",
        "stop",
        "halt",
        "resume",
        "pause",
        "retire",
        "enable",
        "disable",
        "reset",
        "unblock",
        "approve",
        "reject",
        "decide",
        "revoke",
        "rotate",
        "issue",
        "enroll",
        "confirm",
        "regenerate",
        "login",
        "logout",
        "refresh",
        "redeem",
        "request",
        "inspect",
        "accept",
        "resend",
        "verify",
        "validate",
        "quote",
        "test",
        "reveal",
    }
)
"""The closed verb list. A new verb is a rule change: add it here and in `docs/sdk-typescript.md`
(and the compat note)."""

DEPRECATED_SUFFIX: Final = "_deprecated"

OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    # server
    ("GET", "/v1/health"): "get_health",
    ("GET", "/v1/schema/config"): "get_config_schema",
    ("POST", "/v1/config/validate"): "validate_config",
    # sessions and setup
    ("POST", "/v1/auth/login"): "login",
    ("POST", "/v1/auth/login/mfa"): "login_with_mfa",
    ("POST", "/v1/auth/login/mfa/enroll"): "enroll_mfa_at_login",
    ("POST", "/v1/auth/login/mfa/enroll/confirm"): "confirm_mfa_enrollment_at_login",
    ("POST", "/v1/auth/logout"): "logout",
    ("POST", "/v1/auth/refresh"): "refresh_session",
    ("GET", "/v1/auth/session"): "get_session",
    ("GET", "/v1/auth/setup/status"): "get_setup_status",
    ("POST", "/v1/auth/setup/redeem"): "redeem_setup_token",
    # API tokens
    ("GET", "/v1/auth/tokens"): "list_tokens",
    ("POST", "/v1/auth/tokens"): "create_token",
    ("POST", "/v1/auth/tokens/{token_id}/revoke"): "revoke_token",
    ("PATCH", "/v1/auth/tokens/{token_id}"): "update_token",
    ("DELETE", "/v1/auth/tokens/{token_id}"): "delete_token",
    ("POST", "/v1/auth/tokens/revoke-by-link"): "revoke_tokens_by_link",
    # users
    ("GET", "/v1/auth/users"): "list_users",
    ("GET", "/v1/auth/users/{user_id}/agent-credentials"): "list_user_agent_credentials",
    ("POST", "/v1/auth/users/{user_id}/disable"): "disable_user",
    ("POST", "/v1/auth/users/{user_id}/enable"): "enable_user",
    ("PUT", "/v1/auth/users/{user_id}/role"): "set_user_role",
    ("POST", "/v1/auth/users/{user_id}/mfa/reset"): "reset_user_mfa",
    ("POST", "/v1/auth/users/{user_id}/password-reset"): "reset_user_password",
    # MFA (own account) and auth settings
    ("GET", "/v1/auth/mfa"): "get_mfa_status",
    ("POST", "/v1/auth/mfa/enroll"): "enroll_mfa",
    ("POST", "/v1/auth/mfa/enroll/confirm"): "confirm_mfa_enrollment",
    ("POST", "/v1/auth/mfa/recovery-codes"): "regenerate_mfa_recovery_codes",
    ("POST", "/v1/auth/mfa/disable"): "disable_mfa",
    ("POST", "/v1/auth/password/confirm"): "confirm_password",
    ("GET", "/v1/auth/settings"): "get_auth_settings",
    ("PUT", "/v1/auth/settings/mfa"): "set_mfa_policy",
    # invites
    ("GET", "/v1/auth/invites"): "list_invites",
    ("POST", "/v1/auth/invites"): "create_invite",
    ("POST", "/v1/auth/invites/{invite_id}/revoke"): "revoke_invite",
    ("POST", "/v1/auth/invites/{invite_id}/resend"): "resend_invite",
    ("POST", "/v1/auth/invites/inspect"): "inspect_invite",
    ("POST", "/v1/auth/invites/accept"): "accept_invite",
    # email change and password reset
    ("GET", "/v1/auth/email/status"): "get_email_status",
    ("POST", "/v1/auth/me/email"): "start_email_change",
    ("POST", "/v1/auth/email/verify"): "verify_email_change",
    ("POST", "/v1/auth/password-reset/request"): "request_password_reset",
    ("POST", "/v1/auth/password-reset/redeem"): "redeem_password_reset",
    # agents and agent credentials
    ("GET", "/v1/agents"): "list_agents",
    ("GET", "/v1/agents/{agent_id}/eligibility"): "get_agent_eligibility",
    ("GET", "/v1/agent-credentials"): "list_all_agent_credentials",
    ("GET", "/v1/agents/{agent_id}/credentials"): "list_agent_credentials",
    ("POST", "/v1/agents/{agent_id}/credentials"): "issue_agent_credential",
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/revoke"): "revoke_agent_credential",
    ("POST", "/v1/agents/{agent_id}/credentials/{credential_id}/rotate"): "rotate_agent_credential",
    ("POST", "/v1/agents/{agent_id}/credentials/revoke-all"): "revoke_all_agent_credentials",
    # approvals and gate decisions
    ("GET", "/v1/approvals"): "list_approvals",
    ("GET", "/v1/approvals/{approval_id}"): "get_approval",
    ("POST", "/v1/approvals/{approval_id}/approve"): "approve_approval",
    ("POST", "/v1/approvals/{approval_id}/reject"): "reject_approval",
    ("POST", "/v1/approvals/{approval_id}/decision"): "decide_approval_deprecated",
    (
        "POST",
        "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions",
    ): "create_gate_decision",
    (
        "POST",
        "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision",
    ): "decide_gate_deprecated",
    # autonomy
    ("GET", "/v1/autonomy/grants"): "list_autonomy_grants",
    ("POST", "/v1/autonomy/grants"): "create_autonomy_grant",
    ("POST", "/v1/autonomy/grants/{grant_id}/revoke"): "revoke_autonomy_grant",
    ("PUT", "/v1/autonomy/ceilings/{role}/{kind}"): "set_autonomy_ceiling",
    ("PUT", "/v1/autonomy/settings/{name}"): "set_autonomy_setting",
    ("PUT", "/v1/autonomy/model-change-floor/{role}"): "set_model_change_floor",
    ("GET", "/v1/autonomy/proposals"): "list_autonomy_proposals",
    # budgets and costs
    ("GET", "/v1/budgets"): "list_budgets",
    ("GET", "/v1/budgets/{budget_id}"): "get_budget",
    ("POST", "/v1/budgets/{budget_id}/unblock"): "unblock_budget",
    ("GET", "/v1/costs/calls"): "list_model_calls",
    ("GET", "/v1/costs/per-result"): "get_cost_per_result",
    ("GET", "/v1/costs/spend"): "get_spend",
    ("POST", "/v1/costs/quote"): "quote_cost",
    ("GET", "/v1/savings/tokens"): "get_token_savings",
    # dashboards and reports
    ("GET", "/v1/dashboards/approvals"): "get_approvals_dashboard",
    ("GET", "/v1/dashboards/cost-latency"): "get_cost_latency_dashboard",
    ("GET", "/v1/dashboards/verification"): "get_verification_dashboard",
    ("GET", "/v1/dashboards/verifiers"): "get_verifiers_dashboard",
    ("GET", "/v1/reports/downgrade"): "get_downgrade_report",
    ("GET", "/v1/scorecards"): "list_scorecards",
    ("GET", "/v1/verifiers/{agent_id}/calibration"): "get_verifier_calibration",
    # ledger
    ("GET", "/v1/ledger"): "list_ledger_streams",
    ("GET", "/v1/ledger/{stream}"): "list_ledger_events",
    ("GET", "/v1/ledger/{stream}/verify"): "verify_ledger_stream",
    ("GET", "/v1/ledger/verify"): "verify_ledger",
    ("GET", "/v1/content/{org_id}/{content_hash}"): "reveal_content",
    # model catalog
    ("GET", "/v1/models/catalog"): "get_model_catalog",
    ("GET", "/v1/models/catalog/events"): "list_catalog_events",
    ("POST", "/v1/models/catalog/entries"): "create_catalog_entry",
    ("POST", "/v1/models/catalog/prices"): "create_catalog_price",
    ("POST", "/v1/models/catalog/entries/edit"): "update_catalog_entry",
    ("POST", "/v1/models/catalog/entries/approve"): "approve_catalog_entry",
    ("POST", "/v1/models/catalog/entries/pause"): "pause_catalog_entry",
    ("POST", "/v1/models/catalog/entries/retire"): "retire_catalog_entry",
    ("POST", "/v1/models/catalog/entries/remove"): "delete_catalog_entry",
    ("POST", "/v1/models/catalog/edit"): "update_catalog_entry_deprecated",
    ("POST", "/v1/models/catalog/approve"): "approve_catalog_entry_deprecated",
    ("POST", "/v1/models/catalog/pause"): "pause_catalog_entry_deprecated",
    ("POST", "/v1/models/catalog/retire"): "retire_catalog_entry_deprecated",
    ("POST", "/v1/models/catalog/remove"): "delete_catalog_entry_deprecated",
    ("PUT", "/v1/models/catalog/regions"): "set_catalog_regions",
    ("POST", "/v1/models/catalog/regions"): "set_catalog_regions_deprecated",
    # model assignments, connections and keys
    ("GET", "/v1/models/assignments"): "list_model_assignments",
    ("PUT", "/v1/models/assignments/{scope}/{subject}"): "set_model_assignment",
    ("POST", "/v1/models/assignments"): "set_model_assignment_deprecated",
    ("GET", "/v1/models/resolution"): "get_model_resolution",
    ("GET", "/v1/models/connections"): "list_model_connections",
    ("GET", "/v1/models/connections/events"): "list_connection_events",
    ("PUT", "/v1/models/connections/{provider}"): "set_model_connection",
    ("POST", "/v1/models/connections/{provider}/remove"): "delete_model_connection",
    ("POST", "/v1/models/connections/{provider}/test"): "test_model_connection",
    ("GET", "/v1/models/connections/{provider}/key"): "get_model_connection_key",
    ("PUT", "/v1/models/connections/{provider}/key"): "set_model_connection_key",
    ("DELETE", "/v1/models/connections/{provider}/key"): "delete_model_connection_key",
    ("GET", "/v1/models/keys/health"): "get_model_keys_health",
    # organization
    ("GET", "/v1/org/status"): "get_org_status",
    ("PUT", "/v1/org/prompt-cache"): "set_org_prompt_cache",
    ("POST", "/v1/org/halt"): "halt_org",
    ("POST", "/v1/org/resume"): "resume_org",
    ("GET", "/v1/org/halt"): "get_org_halt_deprecated",
    ("GET", "/v1/org/breakers"): "list_org_breakers",
    ("POST", "/v1/org/breakers/{name}/disable"): "disable_org_breaker",
    ("POST", "/v1/org/breakers/{name}/enable"): "enable_org_breaker",
    ("POST", "/v1/org/breakers/{name}/reset"): "reset_org_breaker",
    ("POST", "/v1/org/roles/{role}/pause"): "pause_org_role",
    ("POST", "/v1/org/roles/{role}/resume"): "resume_org_role",
    # runs and tasks
    ("GET", "/v1/runs"): "list_runs",
    ("POST", "/v1/runs"): "start_run",
    ("GET", "/v1/runs/{run_id}"): "get_run",
    ("POST", "/v1/runs/{run_id}/stop"): "stop_run",
    ("GET", "/v1/runs/{run_id}/stop"): "get_run_stop_status",
    ("GET", "/v1/runs/{run_id}/report"): "get_run_report",
    ("GET", "/v1/runs/{run_id}/ledger"): "list_run_ledger_events",
    ("GET", "/v1/runs/{run_id}/ledger/verify"): "verify_run_ledger",
    ("GET", "/v1/runs/{run_id}/tasks"): "list_run_tasks",
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/audit"): "get_task_audit",
    ("GET", "/v1/runs/{run_id}/tasks/{task_id}/timeline"): "get_task_timeline",
}
"""`(METHOD, path template)` -> operation id, every route of the OpenAPI schema."""


class UnnamedRouteError(LookupError):
    """A schema route has no row in `OPERATION_IDS`; the server must not start with it."""


def verb_of(operation_id: str) -> str:
    """The first word of an operation id."""
    return operation_id.split("_", 1)[0]


def is_well_formed(operation_id: str) -> bool:
    """True when the id is snake_case and starts with a verb of `VERBS`."""
    return bool(ID_GRAMMAR.match(operation_id)) and verb_of(operation_id) in VERBS


def sdk_method_name(operation_id: str) -> str:
    """The lowerCamelCase the SDK generator makes of an id (`apps/sdk-ts/scripts/generate.mjs`)."""
    words = [w for w in operation_id.split("_") if w]
    return words[0] + "".join(w[:1].upper() + w[1:] for w in words[1:])


def operation_id_for(route: Any) -> str:
    """FastAPI's `generate_unique_id_function`: the id of a route from the table, or an error."""
    path = str(getattr(route, "path", ""))
    methods = sorted(getattr(route, "methods", None) or ())
    ids = {OPERATION_IDS.get((method, path)) for method in methods}
    if len(ids) != 1 or None in ids:
        raise UnnamedRouteError(
            f"no operation id for {' '.join(methods)} {path}: a plugin route passes "
            "operation_id= (verb-first snake_case, checked at start); a core route needs a row "
            "in nuramesh.server.operation_ids.OPERATION_IDS"
        )
    (operation_id,) = ids
    assert operation_id is not None
    return operation_id


def check_operation_ids() -> None:
    """Every row is well formed and no two rows share an id (called by `create_app`)."""
    bad = sorted(i for i in OPERATION_IDS.values() if not is_well_formed(i))
    if bad:
        raise ValueError(f"operation ids that are not verb-first snake_case: {bad}")
    seen: dict[str, tuple[str, str]] = {}
    for key, operation_id in OPERATION_IDS.items():
        if operation_id in seen:
            raise ValueError(
                f"operation id {operation_id!r} is used by {seen[operation_id]} and {key}"
            )
        seen[operation_id] = key
