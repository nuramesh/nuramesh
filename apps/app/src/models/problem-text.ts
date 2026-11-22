import { InvalidRequestError, NotAvailableError, NotFoundError, ProblemError } from "@/api/client";
import type { WriteResult } from "@/api/models";

/** `$.processing` is shown as `processing`; `$` (a rule about the whole state) has no label. */
function label(path: string): string {
  const name = path.replace(/^\$\.?/, "");
  return name === "" ? "" : `${name}: `;
}

/**
 * What to tell a person when a model request is refused, one line per problem. The server's own
 * words are kept for the refusals it explains well (409s, the 422 problem list); the ones a
 * person can act on get an instruction. Nothing here offers a way around a refusal.
 */
export function describeWrite(error: unknown): string[] {
  if (error instanceof InvalidRequestError) {
    return error.fields.length > 0 ? error.fields.map((f) => `${label(f.path)}${f.message}`) : [error.message];
  }
  if (error instanceof NotFoundError) return [`${error.message}. It may have been removed: reload the page.`];
  if (error instanceof NotAvailableError) return [error.message];
  if (error instanceof ProblemError) {
    switch (error.title) {
      case "forbidden":
        return ["Only an admin can do this. Your role can read the catalog and assignments but not change them."];
      case "human_required":
        return ["This needs a person signed in; an API token can never do it."];
      case "authentication_required":
      case "session_invalid":
        return ["This server has no signed-in session for you, so it refuses changes. Sign in as an admin (a server started with `--auth off` has no sign-in; restart it without that flag)."];
      case "csrf_invalid":
        return ["The session's security token is missing or has expired, so nothing was changed. Reload the page and sign in again."];
      case "content_unavailable":
        return [`The reason could not be stored because the content store is not available, so nothing was changed. ${error.message}`];
      case "rate_limited":
        return [`Too many connection tests: try again in ${String(error.retryAfter ?? "a few")} s.`];
      case "org_halted":
        return [error.message.startsWith("the organization is halted") ? "The organization is halted: no connection is tested until a human lifts it." : error.message];
      case "not_allowed_yet":
      case "mfa_unavailable":
      case "mfa_enrollment_required":
        return [`${error.message}. Nothing was changed.`];
      case "mfa_required":
        return ["This needs a recent second-factor check, within 10 minutes of signing in with a code. Sign out, sign in again with your code, then send the key again. Nothing was changed."];
      case "too_large":
        return ["The request was too large to be a key. Check what you pasted."];
      case "secret_store_unavailable":
        return ["The server's secret store is not available, so nothing was stored. Try again later or ask the operator."];
      case "unreachable":
        return [error.message];
      default:
        return [error.message];
    }
  }
  return [error instanceof Error ? error.message : "Something went wrong."];
}

/**
 * The same for the Key tab's set, rotate and revoke: key writes are owner-only (not admin), and a
 * failed test of the new key is a refusal that changed nothing.
 */
export function describeKeyWrite(error: unknown): string[] {
  if (error instanceof ProblemError) {
    if (error.title === "forbidden") return ["Only the owner can set, rotate or revoke keys."];
    if (error.title === "key_test_failed") {
      return ["The new key did not pass its test request, so nothing was stored and the connection is unchanged."];
    }
  }
  return describeWrite(error);
}

/** The lines shown after a write that went through: what was written and what it changed. */
export function resultLines(result: WriteResult, done: string, options: { listAffected?: boolean } = {}): string[] {
  if (result.already) return ["Nothing changed: it was already so. Nothing was written."];
  const lines = [`${done}${result.ledgerSeq === null ? "" : ` · ledger #${String(result.ledgerSeq)}`}`];
  if (result.affected.length > 0) lines.push(`Their next call is blocked: ${result.affected.join(", ")}.`);
  else if (options.listAffected === true) lines.push("No agent or role uses it.");
  if (result.entryRepaused) {
    lines.push("The entry now accepts data it did not before, so it was paused: approve it again with a reason to use it.");
  }
  if (result.repaused.length > 0) {
    const why =
      result.pausedBecause === "endpoint_changed"
        ? " Their data now goes to a new endpoint."
        : result.pausedBecause === "regions_widened"
          ? " They now accept customer data."
          : "";
    lines.push(`Paused again, approve each again with a reason: ${result.repaused.join(", ")}.${why}`);
  }
  return lines;
}

const TEST_ADVICE: Record<string, string> = {
  unauthorized: "The endpoint did not accept the key or login. For a connection with a key, ask your operator to check the key file behind the key binding name, then test again.",
  unreachable: "Check the URL, and that the endpoint is running and can be reached from the NuraMesh server (not from your browser).",
  redirect: "Enter the URL the endpoint redirects to: a redirect is never followed.",
  server_error: "The endpoint itself reported an error. Try again later.",
  unexpected_status: "Check that the URL is the base path of the endpoint's API (for an OpenAI-compatible server it often ends in /v1).",
};

/**
 * One plain next step after a failed connection test, by the server's result code, or `null` when
 * the server's own text is all there is to say. It never offers a way around a refusal: a private
 * address stays refused until the server's operator allows it.
 */
export function testAdvice(code: string, text: string): string | null {
  if (code === "refused") {
    return text.includes("NURAMESH_ALLOW_PRIVATE_NETWORK")
      ? "This server refuses private, loopback and link-local addresses unless its operator sets NURAMESH_ALLOW_PRIVATE_NETWORK=1 (a self-hosted server with a model on your own network). The connection is kept; nothing else changed."
      : null;
  }
  return TEST_ADVICE[code] ?? null;
}
