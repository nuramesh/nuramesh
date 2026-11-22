"""Least-privilege grants for the service role (R1.29, spec 5.6, ND188 N2).

`nuramesh db migrate --grant-to ROLE` runs `grant_statements` as the table owner after the tables
exist. The service role owns nothing, so it cannot `ALTER TABLE ... DISABLE TRIGGER`, drop a
trigger or replace a trigger function; it can only do what the store itself does. The sets below
are checked against the SQL in `stores/postgres.py` by `tests/test_compose_grants.py`: a column
write without a grant, or a grant without a write, fails that test.
"""

import re

import psycopg

# Append-only tables: triggers refuse UPDATE, DELETE and TRUNCATE; the role gets no more than this.
APPEND_ONLY: tuple[str, ...] = ("ledger_events", "key_tombstones")
# Tables the store inserts into and reads (see UPDATE_COLUMNS and DELETE_TABLES for the rest).
INSERT_TABLES: tuple[str, ...] = (
    "content_holds",
    "identifier_keys",
    "setup_tokens",
    "api_tokens",
    "agent_credentials",
    "invites",
    "email_changes",
    "password_reset_tokens",
    "password_login_blocks",
    "api_token_revoke_links",
    "security_log",
    "users",
    "credentials",
    "auth_sessions",
    "auth_tokens",
    "auth_failures",
    "mfa_factors",
    "mfa_recovery_codes",
    "mfa_challenges",
    "org_auth_settings",
    "content",
    "content_subjects",
    "run_start_requests",
    "light_jobs",
    "light_leader",
    "tasks",
    "approvals",
    "reservations",
    "budget_balances",
    "stream_heads",
    "idempotency_keys",
    "idempotency_subjects",
    "ledger_projection",
    "ledger_projection_counts",
    "extension_records",  # E8: insert and read only; never updated (ext_put refuses a clash)
    "api_token_retired_names",  # #634: insert and read only; no UPDATE, no DELETE, no function
)
# Column-level UPDATE: exactly the columns the store updates (table -> columns).
UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "stream_heads": ("seq", "hash", "at"),
    "tasks": ("run_id", "status", "version", "data"),
    "budget_balances": ("version", "data"),
    "reservations": ("run_id", "status", "version", "data"),
    "approvals": (
        "run_id",
        "agent_id",
        "status",
        "version",
        "data",
        "approval_mode",
        "decided_by",
        "decided_at",
    ),
    "content": ("expires_at",),
    "content_subjects": ("erase_pending",),
    "run_start_requests": ("state", "claim_owner", "claim_until", "refusal_code"),
    "light_jobs": ("state", "due_at", "lease_owner", "lease_until", "epoch"),
    "light_leader": ("owner", "lease_until", "epoch"),
    "users": (
        "identifier_key",
        "email",
        "display_name",
        "erased_at",
        "role",
        "disabled_at",
        "email_verified_at",
        "email_verified_via",
    ),
    "credentials": ("password_hash", "updated_at"),
    "setup_tokens": ("consumed_at",),
    "auth_sessions": (
        "last_active_at",
        "revoked_at",
        "revoke_reason",
        "mfa_verified_at",
        "mfa_method",
        "password_confirmed_at",
        "step_up_at",
    ),
    "mfa_factors": ("secret_enc", "key_id", "confirmed_at", "last_used_step"),
    "mfa_recovery_codes": ("used_at",),
    "mfa_challenges": ("attempts", "consumed_at", "factor_id"),
    "org_auth_settings": ("mfa_required", "updated_at", "updated_by"),
    "auth_tokens": ("used_at",),
    "auth_failures": ("count", "last_failure_at", "locked_until"),
    "identifier_keys": ("key", "destroyed_at"),
    "api_tokens": ("revoked_at", "revoked_by", "name"),
    "agent_credentials": ("created_by", "revoked_by", "revoked_at", "expires_at"),
    "security_log": ("actor", "details", "subject"),
    "invites": (
        "email",
        "token_hash",
        "created_by",
        "expires_at",
        "resent_count",
        "accepted_at",
        "accepted_user_id",
        "revoked_at",
        "revoke_reason",
    ),
    "email_changes": ("consumed_at", "cancelled_at"),
    "password_reset_tokens": ("created_by", "consumed_at", "revoked_at"),
    "api_token_revoke_links": ("consumed_at", "revoked_at"),
    "ledger_projection_counts": ("n",),
    "idempotency_keys": (
        "fingerprint",
        "state",
        "attempt",
        "fence",
        "created_at",
        "lease_until",
        "expires_at",
        "status",
        "payload",
        "nonce",
        "key_id",
    ),
}
DELETE_TABLES: tuple[str, ...] = (
    "content",
    "content_subjects",
    "content_holds",
    "credentials",
    "auth_failures",
    "auth_tokens",
    "auth_sessions",
    "mfa_factors",
    "mfa_recovery_codes",
    "mfa_challenges",
    "security_log",
    "agent_credentials",
    "invites",
    "email_changes",
    "password_reset_tokens",
    "password_login_blocks",
    "api_token_revoke_links",
    "idempotency_keys",
    "idempotency_subjects",
    # no DELETE on the projection tables (ND239): a repair rebuild runs as the owner role
)
# store_identity is written only by migration 12 (run as the owner role): the service role reads it
READ_ONLY_TABLES: tuple[str, ...] = ("schema_version", "store_identity")
# ND314 item 3: deletes that go only through a SECURITY DEFINER function (no DELETE grant). The
# functions are owned by this NOLOGIN role (created by the migration that adds the first one).
DEFINER_ROLE = "nuramesh_definer"
EXTENSION_FUNCTIONS: tuple[str, ...] = (
    "public.extension_records_erase(text)",
    "public.api_token_delete(text, text, timestamptz)",
    "public.api_token_erase_issuer(text, text)",
    "public.api_token_normalise_issuer(text, text)",
    "public.approval_delete_if_due(text, integer, timestamptz)",  # approvals sweep (T226)
)

_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def valid_role(name: str) -> bool:
    """A plain lower-case identifier (it is quoted into SQL, so nothing else is accepted)."""
    return bool(_ROLE.fullmatch(name))


def _execute_grants(functions: tuple[str, ...], who: str) -> list[str]:
    """EXECUTE on each function for `who` only (never PUBLIC)."""
    out: list[str] = []
    for function in functions:
        out += [
            f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC",
            f"REVOKE ALL ON FUNCTION {function} FROM {who}",
            f"GRANT EXECUTE ON FUNCTION {function} TO {who}",
        ]
    return out


def grant_statements(role: str) -> list[str]:
    """The idempotent statements that leave `role` with exactly the privileges above."""
    if not valid_role(role):
        raise ValueError("the service role must be a plain lower-case identifier")
    who = f'"{role}"'
    out = [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {who}",
        f"GRANT USAGE ON SCHEMA public TO {who}",
    ]
    out += [f"GRANT SELECT, INSERT ON {t} TO {who}" for t in (*APPEND_ONLY, *INSERT_TABLES)]
    out += [f"GRANT SELECT ON {t} TO {who}" for t in READ_ONLY_TABLES]
    for table, columns in UPDATE_COLUMNS.items():
        out.append(f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO {who}")
    out += _execute_grants(EXTENSION_FUNCTIONS, who)  # extension_records: no DELETE (ND314)
    out += [f"GRANT DELETE ON {t} TO {who}" for t in DELETE_TABLES]
    return out


def apply_grants(dsn: str, role: str) -> None:
    """Run `grant_statements` in one transaction (as the table owner)."""
    statements = grant_statements(role)
    with psycopg.connect(dsn) as db:
        for statement in statements:
            db.execute(statement.encode())
