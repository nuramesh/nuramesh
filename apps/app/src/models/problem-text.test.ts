import { describe, expect, it } from "vitest";

import { InvalidRequestError, NotAvailableError, NotFoundError, ProblemError } from "@/api/client";

import { describeKeyWrite, describeWrite, resultLines, testAdvice } from "./problem-text";

describe("describeWrite: what to tell a person when a model write is refused", () => {
  it("shows every problem of a 422 with its path, in the server's words", () => {
    const text = describeWrite(
      new InvalidRequestError("x", [
        { path: "$.processing", message: "processing scope 'mars' is not canada, global or region:<id>" },
        { path: "$.reason", message: "a reason (3 to 500 characters) is required" },
      ]),
    );
    expect(text).toEqual([
      "processing: processing scope 'mars' is not canada, global or region:<id>",
      "reason: a reason (3 to 500 characters) is required",
    ]);
  });

  it("a problem with the path $ (a state rule) has no label", () => {
    expect(describeWrite(new InvalidRequestError("x", [{ path: "$", message: "nope" }]))).toEqual(["nope"]);
  });

  it("falls back to the message when a 422 has no list", () => {
    expect(describeWrite(new InvalidRequestError("bad body"))).toEqual(["bad body"]);
  });

  it("explains a viewer's 403 without offering a way around it", () => {
    const [line] = describeWrite(new ProblemError("your role doesn't allow this", 403, "forbidden"));
    expect(line).toMatch(/only an admin/i);
  });

  it("explains a missing or wrong CSRF token", () => {
    expect(describeWrite(new ProblemError("missing or wrong CSRF token", 403, "csrf_invalid"))[0]).toMatch(/security token/i);
  });

  it("explains that tokens can never do this", () => {
    expect(describeWrite(new ProblemError("x", 403, "human_required"))[0]).toMatch(/person signed in/i);
  });

  it("explains sign-in off", () => {
    expect(describeWrite(new ProblemError("x", 403, "authentication_required"))[0]).toMatch(/sign in/i);
    expect(describeWrite(new ProblemError("x", 401, "authentication_required"))[0]).toMatch(/sign in/i);
    expect(describeWrite(new ProblemError("x", 403, "authentication_required"))[0]).toMatch(/--auth off/);
  });

  it("says nothing was changed when the content store is down", () => {
    const [line] = describeWrite(new ProblemError("a reason is stored in the content store, which isn't available: x", 503, "content_unavailable"));
    expect(line).toMatch(/nothing was changed/i);
    expect(line).toMatch(/content store/i);
  });

  it("a rate-limited test says how long to wait", () => {
    expect(describeWrite(new ProblemError("x", 429, "rate_limited", 10))[0]).toBe("Too many connection tests: try again in 10 s.");
  });

  it("a halted organization is named", () => {
    expect(describeWrite(new ProblemError("the organization is halted: no connection is tested until a human lifts it", 409, "org_halted"))[0]).toMatch(/halted/);
  });

  it("keeps the server's text for 409 refusals", () => {
    expect(describeWrite(new ProblemError("'a/b' is still assigned to agent=dana; reassign them first", 409, "in_use"))).toEqual([
      "'a/b' is still assigned to agent=dana; reassign them first",
    ]);
  });

  it("not found and not available are plain", () => {
    expect(describeWrite(new NotFoundError("connection"))[0]).toMatch(/not found/i);
    expect(describeWrite(new NotAvailableError("models.approve"))[0]).toMatch(/isn't available/);
  });

  it("an unreachable server says so", () => {
    expect(describeWrite(new ProblemError("Couldn't reach the NuraMesh server. Is \"nuramesh serve\" running?", 0, "unreachable"))[0]).toMatch(/Couldn't reach/);
  });
});

describe("describeKeyWrite: key writes belong to the owner", () => {
  it("a forbidden key write names the owner, not an admin", () => {
    const [line] = describeKeyWrite(new ProblemError("your role doesn't allow this", 403, "forbidden"));
    expect(line).toBe("Only the owner can set, rotate or revoke keys.");
    expect(describeWrite(new ProblemError("x", 403, "forbidden"))[0]).toMatch(/admin/);
  });

  it("a failed test of the new key says nothing was stored", () => {
    const [line] = describeKeyWrite(new ProblemError("the connection test of the new key failed; the connection is unchanged", 422, "key_test_failed"));
    expect(line).toMatch(/did not pass its test request/);
    expect(line).toMatch(/nothing was stored/);
  });

  it("M41: 403 mfa_required says to sign in again with a code; mfa_enrollment_required does not get that text", () => {
    const [line] = describeKeyWrite(new ProblemError("x", 403, "mfa_required"));
    expect(line).toBe(
      "This needs a recent second-factor check, within 10 minutes of signing in with a code. Sign out, sign in again with your code, then send the key again. Nothing was changed.",
    );
    const [other] = describeKeyWrite(new ProblemError("not enrolled", 403, "mfa_enrollment_required"));
    expect(other).not.toMatch(/Sign out, sign in again/);
  });

  it("every other refusal reads as before", () => {
    expect(describeKeyWrite(new ProblemError("x", 403, "mfa_required"))).toEqual(describeWrite(new ProblemError("x", 403, "mfa_required")));
  });
});

describe("resultLines: the consequences of a write that succeeded", () => {
  const base = { already: false, ledgerSeq: 12, repaused: [], entryRepaused: false, pausedBecause: null, affected: [] };

  it("names the ledger entry", () => {
    expect(resultLines(base, "Approved azure-openai/m")).toEqual(["Approved azure-openai/m · ledger #12"]);
  });

  it("says when nothing changed", () => {
    expect(resultLines({ ...base, already: true, ledgerSeq: null }, "Approved x")).toEqual(["Nothing changed: it was already so. Nothing was written."]);
  });

  it("lists who is blocked by a pause", () => {
    const lines = resultLines({ ...base, affected: ["agent=dana", "role=tester"] }, "Paused x");
    expect(lines[1]).toBe("Their next call is blocked: agent=dana, role=tester.");
  });

  it("says nobody is affected when nobody uses it", () => {
    expect(resultLines(base, "Paused x", { listAffected: true })[1]).toBe("No agent or role uses it.");
  });

  it("says a widening edit paused the entry and it needs a fresh approval", () => {
    const lines = resultLines({ ...base, entryRepaused: true, pausedBecause: "processing_widened" }, "Edited x");
    expect(lines[1]).toMatch(/accepts data it did not before/);
    expect(lines[1]).toMatch(/approve it again/i);
  });

  it("lists the entries a regions write or a repoint paused again", () => {
    const regions = resultLines({ ...base, repaused: ["azure-ai/m"], pausedBecause: "regions_widened" }, "Regions set");
    expect(regions[1]).toContain("Paused again, approve each again with a reason: azure-ai/m.");
    expect(regions[1]).toMatch(/accept customer data/);
    const repoint = resultLines({ ...base, repaused: ["a/b", "a/c"], pausedBecause: "endpoint_changed" }, "Repointed");
    expect(repoint[1]).toContain("a/b, a/c");
    expect(repoint[1]).toMatch(/new endpoint/);
  });
});

describe("testAdvice: what to do after a failed connection test", () => {
  it.each([
    ["unauthorized", /check the key file behind the key binding name/],
    ["unreachable", /reached from the NuraMesh server \(not from your browser\)/],
    ["redirect", /a redirect is never followed/],
    ["server_error", /Try again later/],
    ["unexpected_status", /often ends in \/v1/],
  ])("%s gets one plain next step", (code, text) => {
    expect(testAdvice(code, "the server's text")).toMatch(text);
  });

  it("a refused private address names the server setting and says the connection is kept", () => {
    const advice = testAdvice("refused", "refused: set NURAMESH_ALLOW_PRIVATE_NETWORK=1 on the server");
    expect(advice).toMatch(/unless its operator sets NURAMESH_ALLOW_PRIVATE_NETWORK=1 \(a self-hosted server/);
    expect(advice).toMatch(/The connection is kept/);
  });

  it("says nothing for a pass or a code it does not know (the server's text stands alone)", () => {
    expect(testAdvice("ok", "the endpoint answered")).toBeNull();
    expect(testAdvice("refused", "some other refusal")).toBeNull();
    expect(testAdvice("brand_new_code", "text")).toBeNull();
  });
});
