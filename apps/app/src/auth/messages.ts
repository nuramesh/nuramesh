import { InvalidRequestError, ProblemError } from "@/api/client";
import { describeProblem } from "@/api/problems";
import { TIMEOUT_TEXT } from "@/mfa/login-messages";

/** "4 minutes", "1 minute 30 seconds", "45 seconds": how long to wait, in words. */
export function waitWords(seconds: number): string {
  const total = Math.max(1, Math.ceil(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  const m = `${String(minutes)} minute${minutes === 1 ? "" : "s"}`;
  const s = `${String(rest)} second${rest === 1 ? "" : "s"}`;
  if (minutes === 0) return s;
  return rest === 0 ? m : `${m} ${s}`;
}

export interface LoginFailure {
  /** What to tell the person; when `waitSeconds` is set the screen adds the countdown ("Try again in 4 minutes."). */
  message: string;
  /** Seconds before another try is allowed (a lock or a rate limit), else undefined. */
  waitSeconds?: number;
}

/** A refused sign-in in plain words. The wrong-password message is the server's one answer for every cause. */
export function describeLoginFailure(error: unknown): LoginFailure {
  if (error instanceof ProblemError) {
    if (error.title === "unexpected_answer") return { message: "The server gave an answer this page does not understand. You are not signed in. Try again." };
    if (error.status === 503 && error.title === "mfa_unavailable") return { message: "Two-step sign-in is not available on this server. Ask the operator." };
    if (error.status === 503 && (error.title === "store_busy" || error.title === "busy")) return { message: "The server is busy. Try again in a moment." };
    if (error.status === 401) return { message: "The email or password is not right." };
    if (error.status === 429) {
      const wait = error.retryAfter;
      const why =
        error.title === "too_many_attempts"
          ? "Too many sign-in attempts."
          : error.title === "busy"
            ? "The server is busy right now."
            : "Too many requests from this device.";
      return wait === undefined ? { message: `${why} Wait a little and try again.` } : { message: why, waitSeconds: wait };
    }
    if (error.status === 403) return { message: "The browser's request was refused. Reload the page and try again." };
  }
  if (error instanceof InvalidRequestError) return { message: "Enter a valid email address and your password." };
  return { message: describeProblem(error) };
}

/** A refused second step in plain words: one text for every wrong code, so it never says which part failed. */
export function describeMfaFailure(error: unknown): LoginFailure & { startOver: boolean } {
  if (error instanceof ProblemError) {
    if (error.status === 401 && error.title === "challenge_invalid") {
      return { message: TIMEOUT_TEXT, startOver: true };
    }
    if (error.status === 401) return { message: "That code is not right. Check it and try again.", startOver: false };
    if (error.status === 503 && error.title === "mfa_unavailable") {
      return { message: "Second-factor sign-in is temporarily unavailable. Try again in a minute.", startOver: false };
    }
    if (error.status === 429 || error.status === 403) return { ...describeLoginFailure(error), startOver: false };
  }
  if (error instanceof InvalidRequestError) return { message: "Enter the code exactly as shown.", startOver: false };
  return { message: describeProblem(error), startOver: false };
}

export const MFA_ENROLL_MESSAGE =
  "Your organization requires a second step and yours is not set up yet. Sign in through the API to get a challenge and set it up there (see docs/api.md), then sign in here again.";

/** A refused setup in plain words. The server gives one answer for a wrong, used or expired token. */
export function describeSetupFailure(error: unknown): LoginFailure {
  if (error instanceof ProblemError && error.status === 403) {
    return {
      message:
        // old-name-ok: STALE #611 old positional form shown to the operator
        "That setup token can't be used. It may be wrong, already used or expired (15 minutes from serve, 24 hours from new-setup-token), or an owner already exists. Restart serve, or make a new one on the server with: nuramesh auth new-setup-token <team>",
    };
  }
  if (error instanceof ProblemError && error.status === 429) return describeLoginFailure(error);
  if (error instanceof InvalidRequestError) {
    return { message: "The server refused these details. Check the email address and that the password has at least 12 characters." };
  }
  if (error instanceof ProblemError && error.status === 409) {
    return { message: "That account already exists." };
  }
  return { message: describeProblem(error) };
}

/** The token in a `/setup#token=<t>` link, or null. Only the fragment is read; a query or a path is never a token. */
export function tokenFromFragment(hash: string): string | null {
  const value = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return value !== null && value.trim() !== "" ? value.trim() : null;
}
