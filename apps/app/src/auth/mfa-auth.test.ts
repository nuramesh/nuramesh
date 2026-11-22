import { afterEach, describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { hasNoStore } from "@/api/server/http";
import { CODES, codesAnswer, enrollAnswer, enrolled, fakeFetch, FACTOR, NO_STORE, noLock, PASSWORD, problem, session, status } from "@/mfa/mfa-test-kit";

import { createFixtureAuth } from "./fixture-auth";
import { createServerAuth } from "./server-auth";
import type { TabSync } from "./tab-sync";

function setCookie(value: string | null): void {
  document.cookie = value === null ? "nm_csrf=; Max-Age=0; Path=/" : `nm_csrf=${value}; Path=/`;
}
afterEach(() => {
  setCookie(null);
  vi.restoreAllMocks();
});

const make = (routes: Parameters<typeof fakeFetch>[0], sync?: TabSync) => {
  const server = fakeFetch(routes);
  const auth = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, ...(sync === undefined ? {} : { sync }) });
  return { ...server, auth };
};

describe("M13: the answers that carry secrets are used only with no-store", () => {
  it("hasNoStore is a directive match, not an equality test", () => {
    expect(hasNoStore("no-store")).toBe(true);
    expect(hasNoStore("no-store, private")).toBe(true);
    expect(hasNoStore("NO-STORE")).toBe(true);
    expect(hasNoStore("private,  No-Store")).toBe(true);
    expect(hasNoStore("no-cache")).toBe(false);
    expect(hasNoStore("max-age=0")).toBe(false);
    expect(hasNoStore("x-no-store-ish")).toBe(false);
    expect(hasNoStore(null)).toBe(false);
  });

  it.each([
    ["mfaEnroll", "POST /v1/auth/mfa/enroll", (a: ReturnType<typeof make>["auth"]) => a.mfaEnroll(PASSWORD), enrollAnswer()],
    ["mfaConfirm", "POST /v1/auth/mfa/enroll/confirm", (a: ReturnType<typeof make>["auth"]) => a.mfaConfirm({ factorId: FACTOR, code: "123456" }), codesAnswer()],
    ["mfaRegenerate", "POST /v1/auth/mfa/recovery-codes", (a: ReturnType<typeof make>["auth"]) => a.mfaRegenerate({ password: PASSWORD, code: "123456" }), codesAnswer()],
  ])("%s: a 200 without no-store is discarded (nothing is returned), one with it is used", async (_name, key, call, good) => {
    const bad = make({ [key]: [{ body: good.body, headers: { "cache-control": "no-cache" } }] });
    const error = await call(bad.auth).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect((error as ProblemError).title).toBe("unsafe_answer");
    expect(JSON.stringify(error)).not.toContain(CODES.join(""));
    expect((error as Error).message).not.toContain("AAAA");
    const fine = make({ [key]: [{ body: good.body, headers: { "cache-control": "private, No-Store" } }] });
    await expect(call(fine.auth)).resolves.toBeDefined();
  });

  it("M42: the codes must be exactly ten strings in the display pattern", async () => {
    for (const recovery_codes of [CODES.slice(0, 9), [...CODES, "BBBB-BBBB-BBBB-BBBB"], [...CODES.slice(0, 9), 7], [...CODES.slice(0, 9), "bbbb-bbbb-bbbb-bbbb"], [...CODES.slice(0, 9), "IIII-OOOO-0000-1111"]]) {
      const { auth } = make({ "POST /v1/auth/mfa/recovery-codes": [{ body: { recovery_codes }, headers: NO_STORE }] });
      await expect(auth.mfaRegenerate({ password: PASSWORD, code: "123456" })).rejects.toMatchObject({ title: "unsafe_answer" });
    }
  });
});

describe("M23, M24: request shape, CSRF repeat and no retry", () => {
  it("the secret calls send the CSRF header and are NOT repeated after 403 csrf_invalid; mfaDisable is repeated once", async () => {
    setCookie("csrf-one");
    for (const [key, call] of [
      ["POST /v1/auth/mfa/enroll", (a: ReturnType<typeof make>["auth"]) => a.mfaEnroll(PASSWORD)],
      ["POST /v1/auth/mfa/enroll/confirm", (a: ReturnType<typeof make>["auth"]) => a.mfaConfirm({ factorId: FACTOR, code: "123456" })],
      ["POST /v1/auth/mfa/recovery-codes", (a: ReturnType<typeof make>["auth"]) => a.mfaRegenerate({ password: PASSWORD, code: "123456" })],
    ] as const) {
      const { auth, count, calls } = make({ [key]: [problem(403, "csrf_invalid")], "GET /v1/auth/session": [session("csrf-two")] });
      await expect(call(auth)).rejects.toMatchObject({ status: 403 });
      expect(count(key)).toBe(1);
      expect(calls[0]?.headers["x-csrf-token"]).toBe("csrf-one");
    }
    const disable = make({ "POST /v1/auth/mfa/disable": [problem(403, "csrf_invalid"), { headers: NO_STORE, body: { enrolled: false } }], "GET /v1/auth/session": [session("csrf-two")] });
    await expect(disable.auth.mfaDisable({ password: PASSWORD, code: "123456" })).resolves.toBeUndefined();
    expect(disable.count("POST /v1/auth/mfa/disable")).toBe(2);
  });

  it("sends exactly the fields the server expects", async () => {
    const { auth, calls } = make({
      "POST /v1/auth/mfa/enroll": [enrollAnswer()],
      "POST /v1/auth/mfa/enroll/confirm": [codesAnswer()],
      "POST /v1/auth/mfa/recovery-codes": [codesAnswer()],
      "POST /v1/auth/mfa/disable": [{ headers: NO_STORE, body: { enrolled: false } }],
    });
    await auth.mfaEnroll(PASSWORD);
    await auth.mfaConfirm({ factorId: FACTOR, code: "123456" });
    await auth.mfaRegenerate({ password: PASSWORD, code: "654321" });
    await auth.mfaDisable({ password: PASSWORD, code: "654321" });
    expect(calls.map((c) => c.body)).toEqual([
      { password: PASSWORD },
      { factor_id: FACTOR, code: "123456" },
      { password: PASSWORD, code: "654321" },
      { password: PASSWORD, code: "654321" },
    ]);
    expect(calls.map((c) => c.headers["idempotency-key"])).toEqual([undefined, undefined, undefined, undefined]);
  });

  it("never retries by itself: a 503, a 502 and a dropped connection are one request each, however long the clock runs", async () => {
    vi.useFakeTimers();
    try {
      for (const reply of [problem(503, "busy"), problem(502, "bad_gateway"), undefined]) {
        const routes: Parameters<typeof fakeFetch>[0] = reply === undefined ? {} : { "POST /v1/auth/mfa/enroll/confirm": [reply] };
        const { auth, calls } = make(routes);
        await auth.mfaConfirm({ factorId: FACTOR, code: "123456" }).catch(() => undefined);
        await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
        expect(calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(1);
      }
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("mfaStatus", () => {
  it("maps the server's answer and refuses a body of the wrong shape", async () => {
    const ok = make({ "GET /v1/auth/mfa": [enrolled({ recovery_codes_remaining: 2, required: true })] });
    await expect(ok.auth.mfaStatus()).resolves.toEqual({ available: true, enrolled: true, confirmedAt: "2026-11-13T10:00:00+00:00", recoveryCodesRemaining: 2, required: true });
    const off = make({ "GET /v1/auth/mfa": [status()] });
    await expect(off.auth.mfaStatus()).resolves.toMatchObject({ enrolled: false, confirmedAt: null, recoveryCodesRemaining: 0 });
    const odd = make({ "GET /v1/auth/mfa": [{ body: { available: "yes" }, headers: NO_STORE }] });
    await expect(odd.auth.mfaStatus()).rejects.toBeInstanceOf(ProblemError);
  });
});

/** A tab-sync double: `fire()` is another tab announcing a change. */
function fakeSync() {
  let listener: (() => void) | undefined;
  const sync: TabSync = { notify: vi.fn(), listen: (fn) => { listener = fn; return () => { listener = undefined; }; } };
  return { sync, fire: () => { listener?.(); } };
}

describe("M20b (client part): hold and held (AU-6)", () => {
  it("a sync event while held is remembered, not run; the release runs it once and notifies subscribers each time", async () => {
    const tabs = fakeSync();
    const { auth, count } = make({ "GET /v1/auth/session": [session()] }, tabs.sync);
    await auth.load();
    expect(auth.state().kind).toBe("signed-in");
    const seen: boolean[] = [];
    auth.subscribe(() => { seen.push(auth.held()); });
    const release = auth.hold();
    expect(auth.held()).toBe(true);
    tabs.fire();
    tabs.fire();
    expect(auth.state().kind).toBe("signed-in"); // no "checking", no epoch bump
    expect(count("GET /v1/auth/session")).toBe(1);
    release();
    release(); // once
    await vi.waitFor(() => { expect(count("GET /v1/auth/session")).toBe(2); });
    expect(auth.held()).toBe(false);
    expect(seen[0]).toBe(true);
    expect(seen).toContain(false);
  });

  it("two holds: only the last release runs the held event", async () => {
    const tabs = fakeSync();
    const { auth, count } = make({ "GET /v1/auth/session": [session()] }, tabs.sync);
    await auth.load();
    const a = auth.hold();
    const b = auth.hold();
    tabs.fire();
    a();
    expect(count("GET /v1/auth/session")).toBe(1);
    b();
    await vi.waitFor(() => { expect(count("GET /v1/auth/session")).toBe(2); });
  });

  it("n1: a refresh that the server refuses does nothing to the state while held, and recover() says refused; the real state is acted on at release", async () => {
    const { auth } = make({
      "GET /v1/auth/session": [session(), problem(401, "session_invalid")],
      "POST /v1/auth/refresh": [problem(401, "session_invalid")],
    });
    await auth.load();
    const release = auth.hold();
    await expect(auth.recover()).resolves.toBe("refused");
    expect(auth.state().kind).toBe("signed-in");
    release();
    await vi.waitFor(() => { expect(auth.state().kind).toBe("signed-out"); });
  });

  it("the fixture auth implements hold and held the same way", () => {
    const auth = createFixtureAuth({ account: { email: "a@example.org", password: PASSWORD }, signedIn: true });
    const seen: boolean[] = [];
    auth.subscribe(() => { seen.push(auth.held()); });
    const release = auth.hold();
    expect(auth.held()).toBe(true);
    release();
    release();
    expect(auth.held()).toBe(false);
    expect(seen).toEqual([true, false]);
  });
});
