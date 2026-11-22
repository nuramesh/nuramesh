import { describe, expect, it } from "vitest";

import { InvalidRequestError, ProblemError } from "@/api/client";

import { describeLoginFailure, describeMfaFailure, describeSetupFailure, MFA_ENROLL_MESSAGE, waitWords } from "./messages";
import { safeNextPath } from "./next-path";

describe("waitWords", () => {
  it.each([
    [1, "1 second"],
    [45, "45 seconds"],
    [60, "1 minute"],
    [90, "1 minute 30 seconds"],
    [240, "4 minutes"],
    [900, "15 minutes"],
    [0, "1 second"],
    [59.2, "1 minute"],
  ])("%s seconds reads %s", (seconds, words) => {
    expect(waitWords(seconds)).toBe(words);
  });
});

describe("describeLoginFailure", () => {
  it("gives one answer for every wrong sign-in", () => {
    expect(describeLoginFailure(new ProblemError("x", 401, "invalid_credentials")).message).toBe("The email or password is not right.");
  });
  it("a lock carries its wait", () => {
    expect(describeLoginFailure(new ProblemError("x", 429, "too_many_attempts", 240))).toEqual({
      message: "Too many sign-in attempts.",
      waitSeconds: 240,
    });
  });
  it("a rate limit and a busy server are told apart", () => {
    expect(describeLoginFailure(new ProblemError("x", 429, "rate_limited", 30)).message).toMatch(/Too many requests/);
    expect(describeLoginFailure(new ProblemError("x", 429, "busy", 1)).message).toMatch(/busy/);
  });
  it("a 429 without Retry-After still says to wait", () => {
    const failure = describeLoginFailure(new ProblemError("x", 429, "too_many_attempts"));
    expect(failure.waitSeconds).toBeUndefined();
    expect(failure.message).toMatch(/Wait a little/);
  });
  it("an unreachable server uses the app's plain words", () => {
    expect(describeLoginFailure(new ProblemError("Couldn't reach the NuraMesh server.", 0, "unreachable")).message).toMatch(/Couldn't reach/);
  });
  it("an invalid request asks for a valid email and password", () => {
    expect(describeLoginFailure(new InvalidRequestError("the request is invalid")).message).toMatch(/valid email/);
  });
});

describe("describeSetupFailure", () => {
  it("one message for a wrong, used or expired token, with the command to make a new one", () => {
    const message = describeSetupFailure(new ProblemError("x", 403, "invalid_setup_token")).message;
    expect(message).toMatch(/wrong, already used or expired/);
    expect(message).toMatch(/nuramesh auth new-setup-token/);
  });
  it("a 422 points at the email and the 12-character minimum", () => {
    expect(describeSetupFailure(new InvalidRequestError("x")).message).toMatch(/12 characters/);
  });
});

describe("safeNextPath", () => {
  it.each([
    ["/runs", "/runs"],
    ["/approvals/ap-1?x=1#top", "/approvals/ap-1?x=1#top"],
    ["//evil.example/path", "/"],
    ["https://evil.example", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/login", "/"],
    ["/setup?x=1", "/"],
    ["/runs\n", "/"],
    ["", "/"],
    [undefined, "/"],
    [42, "/"],
  ])("%j goes to %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });
});

describe("second step messages", () => {
  it("503 mfa_unavailable gets a fixed message, not the generic error", () => {
    const out = describeMfaFailure(new ProblemError("detail from the server", 503, "mfa_unavailable"));
    expect(out.message).toBe("Second-factor sign-in is temporarily unavailable. Try again in a minute.");
    expect(out.startOver).toBe(false);
  });

  it("the enrolment text names a real doc and the endpoint, not a missing guide", () => {
    expect(MFA_ENROLL_MESSAGE).toContain("docs/api.md");
    expect(MFA_ENROLL_MESSAGE).toContain("get a challenge");
    expect(MFA_ENROLL_MESSAGE).not.toContain("MFA guide");
  });
});

describe("describeMfaFailure texts", () => {
  it("the wrong-code text does not tell a person on the recovery field to use a recovery code", () => {
    expect(describeMfaFailure(new ProblemError("x", 401, "mfa_invalid")).message).not.toMatch(/recovery code/);
  });
  it("the expired-step text is the AU-5 timeout text", () => {
    const out = describeMfaFailure(new ProblemError("x", 401, "challenge_invalid"));
    expect(out.message).toBe("That sign-in step timed out. Start again.");
  });
});
