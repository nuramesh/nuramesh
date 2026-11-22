/**
 * Changing the sign-in email (R1.40, spec/identity/email-verification.md). `start` always resolves for
 * a 202, whether or not the new address is free (the server hides that). Refusals are `ProblemError`:
 * 401 `invalid_credentials`, 409 `same_email`, 429 `rate_limited` / `busy`, 503 `email_not_configured`.
 * `verify` is public: a bad link is always one 403 `invalid_verification_token`; 409 `already_exists`
 * means the address was taken meanwhile. The link token lives only in the fragment and in memory.
 */
export interface EmailChangeApi {
  start(input: { newEmail: string; password: string }): Promise<void>;
  verify(token: string): Promise<void>;
}
