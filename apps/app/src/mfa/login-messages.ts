import { ProblemError } from "@/api/client";

import { BUSY_TEXT, CODE_TEXT, GENERIC_TEXT, UNAVAILABLE_TEXT } from "./messages";

/** Which login-step call failed. */
export type StepCall = "code" | "recovery" | "enroll" | "confirm";

/** What the sign-in step does with a failure (spec/mfa-web W-2, W-9, W-18, W-19, W-20). */
export type StepFailure =
  | { effect: "show"; text: string }
  /** The challenge is spent or over: back to the password step with the timeout text (AU-5). */
  | { effect: "lapse" }
  /** Back to the password step with this text (the enrolment challenge is single use, W-19). */
  | { effect: "restart"; text: string }
  | { effect: "wait"; text: string; seconds: number | undefined }
  /** 503 `mfa_unavailable` at the code step: recovery codes still work. */
  | { effect: "use-recovery" }
  /** A 200 that is not safe or not in shape: the server has acted (W-9). */
  | { effect: "unsafe" }
  /** The server may have committed (W-20). */
  | { effect: "unknown" };

export const TIMEOUT_TEXT = "That sign-in step timed out. Start again.";
export const NO_APP_CODE_TEXT = "Two-step sign-in with an app code is not available on this server right now. Use a recovery code.";
export const LOGIN_UNSAFE_TEXT =
  "This server answered unsafely. Your setup may have changed on the server and the codes cannot be shown. Sign in again, then regenerate your recovery codes in Settings > Security. If this keeps happening, ask the operator: something between you and the server removes a safety header.";
export const UNEXPECTED_TEXT = "The server gave an answer this page does not understand. You are not signed in. Try again.";
export const SESSION_ENDED_TEXT = "Your session ended. Sign in again to carry on.";
export const ALREADY_ENROLLED_TEXT = "MFA is already set up for this account. Sign in again.";
export const SETUP_EXPIRED_TEXT = "Setup expired. Start again.";
export const STEP_GENERIC_TEXT = "That did not work. Try again in a moment.";

const NAMED_503 = new Set(["mfa_unavailable", "busy", "store_busy"]);

/** One fixed text each; no text says which factor, how many tries are left, or whether the account exists (W-2). */
export function describeStepFailure(error: unknown, call: StepCall): StepFailure {
  if (!(error instanceof ProblemError)) return { effect: "show", text: call === "confirm" ? GENERIC_TEXT : STEP_GENERIC_TEXT };
  const { status, title } = error;
  if (title === "unsafe_answer") return { effect: "unsafe" };
  if (status === 401 && title === "challenge_invalid") return { effect: "lapse" };
  const code = call === "code" || call === "recovery";
  if (status === 401 && title === "mfa_invalid") return { effect: "show", text: CODE_TEXT };
  if (call === "confirm") {
    if (status === 409 && title === "no_pending_factor") return { effect: "restart", text: SETUP_EXPIRED_TEXT };
    if (status === 409 && title === "mfa_already_enrolled") return { effect: "restart", text: ALREADY_ENROLLED_TEXT };
    if (status === 0 || (status >= 500 && !(status === 503 && NAMED_503.has(title)))) return { effect: "unknown" };
  }
  if (status === 429) {
    const text = error.retryAfter === undefined ? "Too many tries. Wait a little and try again." : "Too many tries.";
    return call === "enroll" ? { effect: "restart", text: "Too many tries. Wait a little and try again." } : { effect: "wait", text, seconds: error.retryAfter };
  }
  if (status === 503 && title === "mfa_unavailable") {
    if (call === "code") return { effect: "use-recovery" };
    return call === "enroll" ? { effect: "restart", text: UNAVAILABLE_TEXT } : { effect: "show", text: UNAVAILABLE_TEXT };
  }
  if (status === 503 && (title === "busy" || title === "store_busy")) {
    return call === "enroll" ? { effect: "restart", text: BUSY_TEXT } : { effect: "show", text: BUSY_TEXT };
  }
  if (call === "enroll") return { effect: "restart", text: STEP_GENERIC_TEXT };
  return { effect: "show", text: code ? STEP_GENERIC_TEXT : GENERIC_TEXT };
}

/** A recovery code as the server reads it: upper-case, spaces and hyphens removed; valid only as 16 allowed characters (W-1). */
export function recoveryOf(input: string): string | null {
  const code = input.toUpperCase().replace(/[\s-]+/g, "");
  return /^[A-HJ-NP-Z2-9]{16}$/.test(code) ? code : null;
}
