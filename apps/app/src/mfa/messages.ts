import { ProblemError } from "@/api/client";
import { waitWords } from "@/auth/messages";

/** Which call failed. */
export type MfaCall = "status" | "enroll" | "confirm" | "regenerate" | "disable";

/** What the screen does with a failure. */
export type MfaFailure =
  | { effect: "show"; text: string }
  /** The flow starts again at the password step (the enrolment is single use). */
  | { effect: "restart"; text: string }
  /** The factor is already on: refresh the status. */
  | { effect: "refresh"; text: string }
  | { effect: "wait"; text: string; seconds: number | undefined }
  /** 401 `session_invalid`: renew (W-21), send nothing again. */
  | { effect: "session" }
  /** The server may have committed (W-20): re-read the truth. */
  | { effect: "unknown" }
  /** The answer carried a secret without `no-store` or in a wrong shape (W-9): nothing is shown, the server acted. */
  | { effect: "unsafe"; text: string };

export const UNSAFE_TEXT =
  "This server answered unsafely. Your setup may have changed and the codes cannot be shown. Reload this page and, if MFA is on, regenerate your recovery codes. If this keeps happening, ask the operator.";
export const UNAVAILABLE_TEXT = "Two-step sign-in is not available on this server. Ask the operator.";
export const BUSY_TEXT = "The server is busy. Try again in a moment.";
export const SSO_TEXT =
  "That password is not right. If you sign in with single sign-on you have no password and MFA does not apply to your account here.";
export const REPLAY_TEXT = "That code is not right, or it was already used. Wait for the next code, then try again.";
export const CODE_TEXT = "That code is not right.";
export const REAUTH_TEXT = "For your security, enter your password again.";
export const CODES_LOST_TEXT = "MFA is on. Your recovery codes were not saved: regenerate them in Settings > Security.";
export const UNKNOWN_REGEN_TEXT =
  "We could not tell whether new codes were made. Your old codes may no longer work. Regenerate again with your next app code, and save the new codes.";
export const UNKNOWN_TEXT =
  "We could not tell whether this worked. MFA may be on and your recovery codes may not have been shown. Sign in again; if MFA is on, use your app code, then regenerate your recovery codes in Settings > Security.";
export const GENERIC_TEXT = "Something went wrong. Nothing was changed that you can rely on; reload and check your status.";

const NAMED_503 = new Set(["mfa_unavailable", "busy", "store_busy"]);

/** Maps a failed call to one fixed text and an effect. No text carries a count of tries, or says which factor failed. */
export function describeMfaFailure(error: unknown, call: MfaCall): MfaFailure {
  if (!(error instanceof ProblemError)) return { effect: "show", text: GENERIC_TEXT };
  const { status, title } = error;
  if (title === "unsafe_answer") return { effect: "unsafe", text: UNSAFE_TEXT };
  if (status === 401) {
    if (title === "session_invalid") return { effect: "session" };
    if (title === "invalid_credentials") return { effect: "show", text: call === "enroll" ? SSO_TEXT : "That password is not right." };
    if (title === "mfa_invalid") return { effect: "show", text: call === "confirm" ? CODE_TEXT : REPLAY_TEXT };
    return { effect: "show", text: GENERIC_TEXT }; // never "password not right" for another 401
  }
  if (status === 409) {
    if (title === "reauth_required") return { effect: "restart", text: REAUTH_TEXT };
    if (title === "mfa_already_enrolled") return { effect: "refresh", text: "MFA is already set up for this account." };
    if (title === "no_pending_factor") return { effect: "restart", text: "Setup expired. Start again." };
    if (title === "mfa_required_by_policy") return { effect: "show", text: "Your organization requires MFA for owners, so it cannot be turned off." };
    return { effect: "show", text: GENERIC_TEXT };
  }
  if (status === 429) {
    const wait = error.retryAfter;
    return {
      effect: "wait",
      text: wait === undefined ? "Too many tries. Wait a little and try again." : "Too many tries.",
      seconds: wait,
    };
  }
  if (status === 503 && title === "mfa_unavailable") return { effect: "show", text: UNAVAILABLE_TEXT };
  if (status === 503 && (title === "busy" || title === "store_busy")) return { effect: "show", text: BUSY_TEXT };
  const codeReturning = call === "confirm" || call === "regenerate";
  if (codeReturning && (status === 0 || (status >= 500 && !(status === 503 && NAMED_503.has(title))))) return { effect: "unknown" };
  return { effect: "show", text: GENERIC_TEXT };
}

export { waitWords };

/** A code as the person typed it: spaces removed; valid only as six digits (ASCII). */
export function totpOf(input: string): string | null {
  const code = input.replace(/\s+/g, "");
  return /^[0-9]{6}$/.test(code) ? code : null;
}
