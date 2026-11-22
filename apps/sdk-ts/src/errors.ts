/** The stable error codes the API answers in `application/problem+json` (`urn:nuramesh:<code>`).
 *  A test checks that every code the server source raises is listed here. */
export const ERROR_CODES = [
  "agent_forbidden",
  "agent_mismatch",
  "agent_suspended",
  "already_exists",
  "already_halted",
  "already_paused",
  "already_running",
  "ambiguous_budget",
  "approval_refused",
  "authentication_required",
  "busy",
  "calibration_unavailable",
  "challenge_invalid",
  "conflict",
  "content_store_unavailable",
  "content_unreadable",
  "creator_inactive",
  "cursor_invalid",
  "credential_limit",
  "credential_not_active",
  "csrf_invalid",
  "customer_data_not_enabled",
  "decider_not_allowed",
  "email_not_configured",
  "email_send_failed",
  "email_taken",
  "email_unsupported",
  "email_unverified",
  "engine_unavailable",
  "entitlement_required",
  "forbidden",
  "foreign_origin",
  "hosted_refused",
  "human_required",
  "idempotency_in_progress",
  "idempotency_key_invalid",
  "idempotency_key_reused",
  "idempotency_key_unsupported",
  "idempotency_response_unavailable",
  "idempotency_unavailable",
  "internal_error",
  "invalid_amount",
  "invalid_config",
  "invalid_credentials",
  "invalid_decision",
  "invalid_hash",
  "invalid_host",
  "invalid_invite",
  "invalid_request",
  "invalid_reset_token",
  "invalid_revoke_link",
  "invalid_setup_token",
  "invalid_verification_token",
  "invite_limit",
  "invite_not_pending",
  "invite_pending",
  "key_ref_server_only",
  "last_owner",
  "length_required",
  "mfa_already_enrolled",
  "mfa_enrollment_required",
  "mfa_invalid",
  "mfa_not_enrolled",
  "mfa_required",
  "mfa_required_by_policy",
  "mfa_unavailable",
  "no_config",
  "no_content_store",
  "no_password_credential",
  "no_pending_factor",
  "no_rule",
  "no_such_breaker",
  "no_such_role",
  "not_allowed_yet",
  "not_found",
  "not_paused",
  "not_tripped",
  "org_erased",
  "org_halted",
  "org_key_destroyed",
  "org_key_missing",
  "person_disabled",
  "plan_cycle",
  "plan_invalid",
  "plan_too_many_tasks",
  "rate_limited",
  "reauth_required",
  "resend_limit",
  "role_not_assignable",
  "run_budget_blocked",
  "run_budget_exhausted",
  "run_budget_missing",
  "run_cap_reached",
  "run_finished",
  "run_id_conflict",
  "run_rate_limited",
  "same_email",
  "self_approval",
  "session_invalid",
  "shipped_prices_invalid",
  "store_busy",
  "store_error",
  "template_unknown",
  "tls_required",
  "token_active",
  "token_invalid",
  "token_name_taken",
  "too_large",
  "too_many_attempts",
  "unblock_refused",
  "unsupported_media_type",
  "use_self_service",
  "user_disabled",
  "version_unsupported",
  "acknowledge_required",
  "action_changed",
  "solo_rate_limited",
  "step_up_required",
] as const;

/** Codes the SDK itself produces when there is no problem document to read. */
export const CLIENT_ERROR_CODES = ["network_error", "unknown_error"] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];
export type ClientErrorCode = (typeof CLIENT_ERROR_CODES)[number];

/** Any failed call: a problem answer, a non-JSON failure (status, `unknown_error`) or a network
 *  failure (status 0, `network_error`). `code` is a known literal or, for a newer server, a string. */
export class NuraMeshError extends Error {
  readonly status: number;
  readonly code: ErrorCode | ClientErrorCode | (string & {});
  readonly detail: string;
  /** Extra fields of the problem document (for example `key` of `entitlement_required`). */
  readonly extra: Readonly<Record<string, unknown>>;

  constructor(
    status: number,
    code: string,
    detail: string,
    extra: Readonly<Record<string, unknown>> = {},
    options?: { cause?: unknown },
  ) {
    super(`${String(status)} ${code}: ${detail}`, options);
    this.name = "NuraMeshError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.extra = extra;
  }
}

/** True when `error` is a NuraMeshError with that code. */
export function isErrorCode(error: unknown, code: ErrorCode | ClientErrorCode): boolean {
  return error instanceof NuraMeshError && error.code === code;
}
