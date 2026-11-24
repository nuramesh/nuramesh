import { InvalidRequestError, ProblemError } from "@/api/client";
import { formatMoney, fromMicros, toMicros } from "@/api/money";
import type { Approval } from "@/api/types";
import { waitWords } from "@/auth/messages";

/**
 * The kinds whose solo approve needs a fresh proof and, in the dialog, the kind word typed
 * (the solo approval notes section 1, "HR"; the server's `HIGH_RISK_KINDS`). The server decides;
 * this list only chooses what the dialog asks for.
 */
const HIGH_RISK_KINDS: ReadonlySet<string> = new Set(["spend", "external_message", "merge", "run_command", "network", "use_tool"]);

export function isHighRisk(kind: string): boolean {
  return HIGH_RISK_KINDS.has(kind);
}

/** The words every solo approve shows (SA-17). */
export const NOBODY_ELSE = "Nobody else will review this.";
export const YOU_STARTED = "You started this run.";

/** Where the proof stands when it cannot be given in the dialog. */
export const SIGN_IN_AGAIN_MFA = "Approving this kind of action alone needs a fresh sign-in with your second factor. Sign out, sign in again with your second factor, then come back to this request within 10 minutes.";
export const SIGN_IN_AGAIN = "Approving this kind of action alone needs a fresh sign-in. Sign out and sign in again, then come back to this request within 10 minutes.";
export const NO_PROOF_HELPS = "Approving this kind of action alone needs a second sign-in step, and it is not set up for you or not available on this server. You can't approve this request until it is.";

/** The page's notice after a 409 `action_changed`: the dialog closed and the request was read again. */
export const ACTION_CHANGED = "This request changed after you opened it, so nothing was approved. The current request is shown now: read it again before you decide.";
/** The same words the decision form uses for a request someone else decided meanwhile. */
export const ALREADY_DECIDED = "Your decision was not recorded: this request was already decided (perhaps in another window). It is read-only now.";

const tryAgain = (seconds: number | undefined): string => (seconds === undefined ? "Wait a little and try again." : `Try again in ${waitWords(seconds)}.`);

/**
 * A refused solo approve in plain words (SA-7, SA-9; spec 3.3), or null when the problem is not one
 * of the solo refusals. Never the server's own text: these codes have fixed meanings.
 */
export function soloRefusal(error: unknown): string | null {
  if (!(error instanceof ProblemError)) return null;
  switch (error.title) {
    case "mfa_required":
      return "Your second-factor sign-in is more than 10 minutes old. Sign out, sign in again with your second factor, then approve. Nothing was approved.";
    case "step_up_required":
      return "Your sign-in is more than 10 minutes old. Confirm your password (or sign in again), then approve. Nothing was approved.";
    case "mfa_unavailable":
      return "Approving this kind of action alone needs a second sign-in step, and this server cannot check one right now. Nothing was approved. Ask whoever runs the server to restore it.";
    case "mfa_enrollment_required":
      return "Approving this kind of action alone needs a second sign-in step, and you have not set one up. Nothing was approved. Set it up under Settings > Security, sign in again, then approve.";
    case "solo_rate_limited":
      return `Nothing was approved: you have approved alone as often as this organization allows in one hour. ${tryAgain(error.retryAfter)}`;
    case "acknowledge_required":
      return "The server did not get your confirmation that you are approving alone, so nothing was approved. Reload the page and try again.";
    default:
      return null;
  }
}

/** The proofs a refusal leaves open (SA-7), so the dialog can ask for the right one; null when the refusal is not about a proof. */
export function proofsAfter(error: unknown): string[] | null {
  if (!(error instanceof ProblemError)) return null;
  switch (error.title) {
    case "step_up_required":
      return ["password", "sign_in"];
    case "mfa_required":
      return ["mfa"];
    case "mfa_unavailable":
    case "mfa_enrollment_required":
      return [];
    default:
      return null;
  }
}

/** A refused password confirm (SA-8) in plain words. The password itself is never part of the text. */
export function passwordProblem(error: unknown): string {
  if (error instanceof InvalidRequestError) return "That is not a password the server accepts. Nothing was approved.";
  if (error instanceof ProblemError) {
    switch (error.title) {
      case "invalid_credentials":
        return "That password is not right. Nothing was approved.";
      case "too_many_attempts":
        return `Too many wrong passwords. ${tryAgain(error.retryAfter)}`;
      case "busy":
      case "rate_limited":
        return `The server is busy. ${tryAgain(error.retryAfter)}`;
      case "session_invalid":
        return "Your session has ended. Sign in again, then come back to this request.";
      case "human_required":
      case "authentication_required":
        return "Only a person signed in to this server can confirm a password.";
    }
  }
  return "The password could not be checked, so nothing was approved. Try again.";
}

/** What is left of the run's budget, exact (integer micro-units); words when it is not known. */
export function budgetLeft(runSpend: Approval["runSpend"]): string {
  if (runSpend === null) return "not available";
  if (runSpend.limit === null) return "no run budget recorded";
  if (runSpend.limit.currency !== runSpend.spent.currency) return "not available";
  try {
    return formatMoney(fromMicros(toMicros(runSpend.limit) - toMicros(runSpend.spent), runSpend.limit.currency));
  } catch {
    return "not available";
  }
}
