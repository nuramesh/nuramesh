/**
 * Password reset (R1.41, the password reset notes). A person asks for a link by email, opens the
 * single-use link, and sets a new password; a second emailed link can revoke one API token. The app never
 * sees a stored token or hash: link tokens live in the fragment and in memory alone.
 *
 * `request` always resolves for a 202 (the answer never depends on the address); `rate_limited` (429) is the only
 * refusal. `redeem` rejects `ProblemError` 403 `invalid_reset_token` for any bad link, `InvalidRequestError` for a
 * weak password, 429 `busy`/`rate_limited`. `revokeByLink` rejects 403 `invalid_revoke_link`.
 */
export interface PasswordResetApi {
  request(email: string): Promise<void>;
  redeem(input: { token: string; password: string }): Promise<void>;
  revokeByLink(token: string): Promise<void>;
}

/** The shortest password the server accepts. */
export const RESET_MIN_PASSWORD = 12;
