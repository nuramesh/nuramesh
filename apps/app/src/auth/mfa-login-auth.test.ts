import { afterEach, describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import {
  CHALLENGE,
  CODES,
  enrollAnswer,
  enrollRequired,
  FACTOR,
  fakeFetch,
  loginConfirmAnswer,
  mfaRequired,
  NO_STORE,
  noLock,
  PASSWORD,
  problem,
  SECRET,
  session,
  USER,
} from "@/mfa/mfa-test-kit";

import { createServerAuth } from "./server-auth";
import type { TabSync } from "./tab-sync";

const LOGIN = "POST /v1/auth/login";
const ENROLL = "POST /v1/auth/login/mfa/enroll";
const CONFIRM = "POST /v1/auth/login/mfa/enroll/confirm";
const SESSION = "GET /v1/auth/session";

function setCookie(value: string | null): void {
  document.cookie = value === null ? "nm_csrf=; Max-Age=0; Path=/" : `nm_csrf=${value}; Path=/`;
}
afterEach(() => {
  setCookie(null);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const make = (routes: Parameters<typeof fakeFetch>[0], sync?: TabSync) => {
  const server = fakeFetch(routes);
  const auth = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, ...(sync === undefined ? {} : { sync }) });
  return { ...server, auth };
};
const notify = (): { sync: TabSync; notify: ReturnType<typeof vi.fn> } => {
  const fn = vi.fn();
  return { sync: { notify: fn, listen: () => () => undefined }, notify: fn };
};

describe("AU-1 / M1 / M2: login answers a closed union and never signs in on a guess", () => {
  it("a body with a user still signs in (M22)", async () => {
    const { sync, notify: sent } = notify();
    const { auth } = make({ [LOGIN]: [session()] }, sync);
    await expect(auth.login("ada@example.org", PASSWORD)).resolves.toEqual({ kind: "signed-in" });
    expect(auth.state()).toMatchObject({ kind: "signed-in" });
    expect(sent).toHaveBeenCalledTimes(1);
  });

  it("mfa_required: returns the challenge, stays signed out, tells no other tab", async () => {
    const { sync, notify: sent } = notify();
    const { auth } = make({ [LOGIN]: [mfaRequired()] }, sync);
    await expect(auth.login("ada@example.org", PASSWORD)).resolves.toEqual({
      kind: "mfa",
      challenge: CHALLENGE,
      methods: ["totp", "recovery_code"],
      expiresIn: 300,
    });
    expect(auth.state().kind).not.toBe("signed-in");
    expect(sent).not.toHaveBeenCalled();
  });

  it("mfa_enroll_required: returns the challenge (when well formed), stays signed out", async () => {
    const { auth } = make({ [LOGIN]: [enrollRequired({ expires_in: 900 })] });
    await expect(auth.login("ada@example.org", PASSWORD)).resolves.toEqual({ kind: "mfa-enroll", challenge: CHALLENGE, expiresIn: 900 });
    expect(auth.state().kind).not.toBe("signed-in");
  });

  const BAD: [string, Record<string, unknown>][] = [
    ["no user and no status", { csrf_token: null }],
    ["a user and a status", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp"], expires_in: 60, user: USER, csrf_token: null }],
    ["an unknown status", { status: "weird", challenge: CHALLENGE, expires_in: 60 }],
    ["no challenge", { status: "mfa_required", methods: ["totp"], expires_in: 60 }],
    ["expires_in 0", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp"], expires_in: 0 }],
    ["expires_in 901", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp"], expires_in: 901 }],
    ["expires_in a string", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp"], expires_in: "x" }],
    ["expires_in missing", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp"] }],
    ["methods missing", { status: "mfa_required", challenge: CHALLENGE, expires_in: 60 }],
    ["methods not an array", { status: "mfa_required", challenge: CHALLENGE, methods: "totp", expires_in: 60 }],
    ["methods without totp", { status: "mfa_required", challenge: CHALLENGE, methods: ["recovery_code"], expires_in: 60 }],
    ["methods with an unknown value", { status: "mfa_required", challenge: CHALLENGE, methods: ["totp", "sms"], expires_in: 60 }],
    ["enrol with a bad challenge", { status: "mfa_enroll_required", challenge: "x", expires_in: 60 }],
    ["enrol with expires_in 0", { status: "mfa_enroll_required", challenge: CHALLENGE, expires_in: 0 }],
    ["csrf_token of a wrong type", { user: USER, csrf_token: 5 }],
  ];
  it.each(BAD)("refuses %s: unexpected answer, never signed in, no TypeError", async (_name, body) => {
    const { auth } = make({ [LOGIN]: [{ body }] });
    const error = await auth.login("ada@example.org", PASSWORD).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect(error).not.toBeInstanceOf(TypeError);
    expect((error as ProblemError).title).toBe("unexpected_answer");
    expect(auth.state().kind).not.toBe("signed-in");
  });
});

describe("AU-2 / M23: the login-step calls", () => {
  it("loginEnroll and loginEnrollConfirm carry no CSRF header and are not repeated after a 403 csrf_invalid", async () => {
    setCookie("csrf-one");
    const a = make({ [ENROLL]: [problem(403, "csrf_invalid")], [SESSION]: [session("csrf-two")] });
    await expect(a.auth.loginEnroll(CHALLENGE)).rejects.toMatchObject({ status: 403 });
    expect(a.count(ENROLL)).toBe(1);
    expect(Object.keys(a.calls[0]?.headers ?? {}).map((h) => h.toLowerCase())).not.toContain("x-csrf-token");
    const b = make({ [CONFIRM]: [problem(403, "csrf_invalid")], [SESSION]: [session("csrf-two")] });
    await expect(b.auth.loginEnrollConfirm({ challenge: CHALLENGE, factorId: FACTOR, code: "123456" })).rejects.toMatchObject({ status: 403 });
    expect(b.count(CONFIRM)).toBe(1);
  });

  it("completeMfa is not repeated after a 403 csrf_invalid (exactly one request)", async () => {
    setCookie("csrf-one");
    const c = make({ "POST /v1/auth/login/mfa": [problem(403, "csrf_invalid"), problem(403, "csrf_invalid")], [SESSION]: [session("csrf-two")] });
    await expect(c.auth.completeMfa(CHALLENGE, { code: "123456" })).rejects.toMatchObject({ status: 403 });
    expect(c.count("POST /v1/auth/login/mfa")).toBe(1);
  });

});

describe("AU-3 / M42: what a success does", () => {
  it("a completeMfa success signs in through the same path as a password login and tells the other tabs once", async () => {
    const { sync, notify: sent } = notify();
    const { auth } = make({ "POST /v1/auth/login/mfa": [session()] }, sync);
    await auth.completeMfa(CHALLENGE, { code: "123456" });
    expect(auth.state()).toMatchObject({ kind: "signed-in", user: { email: "ada@example.org" } });
    expect(sent).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["no user", { csrf_token: null }],
    ["a status present", { user: USER, csrf_token: null, status: "mfa_required" }],
    ["csrf_token of a wrong type", { user: USER, csrf_token: 7 }],
  ])("a completeMfa answer with %s is refused as unsafe and signs nobody in", async (_n, body) => {
    const { auth } = make({ "POST /v1/auth/login/mfa": [{ body }] });
    await expect(auth.completeMfa(CHALLENGE, { code: "123456" })).rejects.toMatchObject({ title: "unsafe_answer" });
    expect(auth.state().kind).not.toBe("signed-in");
  });

  it("loginEnroll needs no-store; a good answer is the pending factor", async () => {
    const bad = make({ [ENROLL]: [{ body: enrollAnswer().body, headers: { "cache-control": "no-cache" } }] });
    await expect(bad.auth.loginEnroll(CHALLENGE)).rejects.toMatchObject({ title: "unsafe_answer" });
    const good = make({ [ENROLL]: [enrollAnswer()] });
    await expect(good.auth.loginEnroll(CHALLENGE)).resolves.toMatchObject({ secret: SECRET, factorId: FACTOR });
    expect(good.calls[0]?.body).toEqual({ challenge: CHALLENGE });
  });

  it("loginEnrollConfirm returns the ten codes and an apply; nothing signs in and nobody is told until apply()", async () => {
    const { sync, notify: sent } = notify();
    const { auth, count } = make({ [CONFIRM]: [loginConfirmAnswer()], [SESSION]: [session()] }, sync);
    const done = await auth.loginEnrollConfirm({ challenge: CHALLENGE, factorId: FACTOR, code: "123456" });
    expect(done.recoveryCodes).toEqual(CODES);
    expect(Object.keys(done).sort()).toEqual(["apply", "recoveryCodes"]); // no csrf_token, no raw answer
    expect(auth.state().kind).not.toBe("signed-in");
    expect(sent).not.toHaveBeenCalled();
    expect(count(SESSION)).toBe(0);
    await done.apply();
    expect(count(SESSION)).toBe(1); // re-read, never signedIn() on the stale answer
    expect(auth.state()).toMatchObject({ kind: "signed-in" });
    expect(sent).toHaveBeenCalledTimes(1);
  });

  it("apply() lands signed out when the server says there is no session any more (M43)", async () => {
    const { auth } = make({ [CONFIRM]: [loginConfirmAnswer()], [SESSION]: [problem(401, "session_invalid")], "POST /v1/auth/refresh": [problem(401, "session_invalid")] });
    const done = await auth.loginEnrollConfirm({ challenge: CHALLENGE, factorId: FACTOR, code: "123456" });
    await done.apply();
    expect(auth.state().kind).not.toBe("signed-in");
  });

  it.each([
    ["no user", { recovery_codes: CODES }],
    ["a status present", { user: USER, csrf_token: null, recovery_codes: CODES, status: "x" }],
    ["csrf_token of a wrong type", { user: USER, csrf_token: 1, recovery_codes: CODES }],
    ["9 codes", { user: USER, csrf_token: null, recovery_codes: CODES.slice(0, 9) }],
    ["11 codes", { user: USER, csrf_token: null, recovery_codes: [...CODES, "BBBB-BBBB-BBBB-BBBB"] }],
    ["a non-string code", { user: USER, csrf_token: null, recovery_codes: [...CODES.slice(0, 9), 4] }],
    ["a code off the pattern", { user: USER, csrf_token: null, recovery_codes: [...CODES.slice(0, 9), "iiii-oooo-0000-1111"] }],
  ])("a loginEnrollConfirm answer with %s is refused: no codes, nobody signed in", async (_n, body) => {
    const { auth } = make({ [CONFIRM]: [{ body, headers: NO_STORE }] });
    const error = await auth.loginEnrollConfirm({ challenge: CHALLENGE, factorId: FACTOR, code: "123456" }).catch((e: unknown) => e);
    expect(error).toMatchObject({ title: "unsafe_answer" });
    expect(auth.state().kind).not.toBe("signed-in");
    expect(JSON.stringify(error)).not.toContain("BBBB");
  });

  it("loginEnrollConfirm without no-store is discarded (the signed-in codes route is not mixed up with it)", async () => {
    const { auth } = make({ [CONFIRM]: [{ body: loginConfirmAnswer().body, headers: { "cache-control": "no-cache" } }] });
    await expect(auth.loginEnrollConfirm({ challenge: CHALLENGE, factorId: FACTOR, code: "123456" })).rejects.toMatchObject({ title: "unsafe_answer" });
  });
});
