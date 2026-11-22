import { InvalidRequestError, ProblemError } from "./client";

/**
 * A server problem in plain words, for a screen to show. The server's own text is kept for the
 * cases it explains well; the ones a person can fix get an instruction.
 */
export function describeProblem(error: unknown): string {
  if (!(error instanceof Error)) return "Something went wrong.";
  if (!(error instanceof ProblemError)) return error.message;
  switch (error.title) {
    case "authentication_required":
      return "This server was started with `--auth off`, so it refuses Stop, approvals and unblock. Restart it without `--auth off`.";
    case "content_store_unavailable":
      return "The server couldn't store the reason because its content store isn't set up, so nothing was changed. Install nuramesh[content] and set NURAMESH_KEY_DIR on the server, or leave the reason empty if the action allows it.";
    case "run_finished":
      return "This run has already ended, so there is nothing to stop.";
    case "foreign_origin":
    case "invalid_host":
      return "The server refused the address this page was opened from. Open the app on 127.0.0.1 or localhost and let the dev server forward /v1 to it (see docs/app.md).";
    case "unreachable":
      return error.message;
    default:
      return error.message;
  }
}

/**
 * A refused decision (an approval or a human gate) in plain words (SA-5, SA-19). A sole decider
 * who did not acknowledge is told what to tick; a changed action or round is told to reload; a
 * 422 says what the server did not accept.
 */
export function describeDecisionProblem(error: unknown, hasAcknowledgementBox = true): string {
  if (error instanceof InvalidRequestError) return `The server did not accept this decision: ${error.message}`;
  if (error instanceof ProblemError) {
    switch (error.title) {
      case "acknowledge_required":
        if (!hasAcknowledgementBox) return "You are now the only person who can decide this. Reload, read the request again, then decide.";
        return "You are the only person who can decide this, so nobody else will review it. Tick the acknowledgement and send again.";
      case "action_changed":
        return "The action changed since you opened it. Reload to see the current request, then decide again.";
      case "already_decided":
        return "This gate already has a decision in this round. Reload the page to see it.";
      case "stale_round":
        return "This gate's round changed since you opened it. Reload the page, read the current round, then decide again.";
      case "self_approval":
        return "You started this run and another person can decide it, so you can't decide it yourself.";
    }
  }
  return describeProblem(error);
}
