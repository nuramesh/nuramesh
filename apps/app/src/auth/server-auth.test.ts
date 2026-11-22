import { afterEach, describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { createServerClient } from "@/api/server-client";

import { createServerAuth } from "./server-auth";

interface Reply {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}
type Handler = (call: Call) => Reply | Promise<Reply>;
interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

/** A fake server: routes "METHOD /path" to a handler (a list answers one reply per call), records every call. */
function fakeFetch(routes: Record<string, Handler | Reply[]>) {
  const calls: Call[] = [];
  const counters = new Map<string, number>();
  const fetchFn = vi.fn(async (input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = new URL(input, "http://127.0.0.1:5173").pathname;
    const call: Call = {
      method,
      path,
      body: init?.body === undefined ? undefined : JSON.parse(init.body as string),
      headers: (init?.headers ?? {}) as Record<string, string>,
    };
    calls.push(call);
    const route = routes[`${method} ${path}`];
    if (route === undefined) throw new TypeError("network down");
    let reply: Reply;
    if (Array.isArray(route)) {
      const n = counters.get(`${method} ${path}`) ?? 0;
      counters.set(`${method} ${path}`, n + 1);
      reply = route[Math.min(n, route.length - 1)] ?? {};
    } else {
      reply = await route(call);
    }
    const status = reply.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(reply.body ?? {}), {
      status,
      headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json", ...(reply.headers ?? {}) },
    });
  });
  return { fetchFn, calls, count: (key: string) => calls.filter((c) => `${c.method} ${c.path}` === key).length };
}

const problem = (status: number, title: string, headers?: Record<string, string>): Reply => ({
  status,
  body: { title, status, detail: `${title} detail` },
  ...(headers === undefined ? {} : { headers }),
});
const USER = { id: "human:u_0a1b2c3d4e5f60718293", org_id: "acme", role: "owner", email: "ada@example.org", display_name: "Ada" };
const session = (csrf: string | null = "csrf-one"): Reply => ({ body: { user: USER, session: { id: "s" }, csrf_token: csrf } });
const noLock = <T>(task: () => Promise<T>): Promise<T> => task();

function setCookie(value: string | null): void {
  document.cookie = value === null ? "nm_csrf=; Max-Age=0; Path=/" : `nm_csrf=${value}; Path=/`;
}

afterEach(() => {
  setCookie(null);
  vi.restoreAllMocks();
});

function auth(routes: Record<string, Handler | Reply[]>, lock = noLock) {
  const server = fakeFetch(routes);
  return { ...server, auth: createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock }) };
}

describe("who is signed in", () => {
  it("reads the session and becomes signed in", async () => {
    const { auth: a } = auth({ "GET /v1/auth/session": () => session() });
    expect(a.state()).toEqual({ kind: "checking" });
    await a.load();
    expect(a.state()).toEqual({
      kind: "signed-in",
      user: { id: USER.id, orgId: "acme", role: "owner", email: "ada@example.org", displayName: "Ada" },
    });
  });

  it("is signed out on 401 when there is no refresh cookie either", async () => {
    const { auth: a, count } = auth({
      "GET /v1/auth/session": () => problem(401, "session_invalid"),
      "POST /v1/auth/refresh": () => problem(401, "session_invalid"),
    });
    await a.load();
    expect(a.state()).toEqual({ kind: "signed-out" });
    expect(count("POST /v1/auth/refresh")).toBe(1);
  });

  it("renews once when only the 15-minute access token ran out, then asks again", async () => {
    const { auth: a, calls } = auth({
      "GET /v1/auth/session": [problem(401, "session_invalid"), session("csrf-two")],
      "POST /v1/auth/refresh": () => session("csrf-two"),
    });
    await a.load();
    expect(a.state().kind).toBe("signed-in");
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /v1/auth/session",
      "POST /v1/auth/refresh",
      "GET /v1/auth/session",
    ]);
  });

  it("treats a server without accounts as open (nothing to sign in to)", async () => {
    const { auth: a } = auth({ "GET /v1/auth/session": () => problem(404, "not_found") });
    await a.load();
    expect(a.state()).toEqual({ kind: "open" });
  });

  it("says 'unavailable', not 'signed out', when the server can't be reached", async () => {
    const { auth: a } = auth({});
    await a.load();
    expect(a.state().kind).toBe("unavailable");
  });

  it("is unavailable, not signed out, when the renewal can't reach the server", async () => {
    const { auth: a } = auth({
      "GET /v1/auth/session": () => problem(401, "session_invalid"),
      "POST /v1/auth/refresh": () => problem(429, "busy", { "retry-after": "1" }),
    });
    await a.load();
    expect(a.state().kind).toBe("unavailable");
  });

  it("notifies listeners on every change", async () => {
    const { auth: a } = auth({ "GET /v1/auth/session": () => session() });
    const listener = vi.fn();
    const stop = a.subscribe(listener);
    await a.load();
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
    await a.load();
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe("setup status", () => {
  it("asks the public status route and is true only for owner_missing: true", async () => {
    const { auth: a, count } = auth({ "GET /v1/auth/setup/status": [{ body: { owner_missing: true } }, { body: { owner_missing: false } }] });
    expect(await a.setupStatus()).toBe(true);
    expect(await a.setupStatus()).toBe(false);
    expect(count("GET /v1/auth/setup/status")).toBe(2);
  });

  it("is false (nothing offered) when the answer is anything else: 404, 429, an odd body, a dead network", async () => {
    for (const reply of [problem(404, "not_found"), problem(429, "rate_limited", { "retry-after": "5" }), { body: { owner_missing: "yes" } }, { body: {} }]) {
      expect(await auth({ "GET /v1/auth/setup/status": [reply] }).auth.setupStatus()).toBe(false);
    }
    expect(await auth({}).auth.setupStatus()).toBe(false);
  });

  it("never changes who is signed in and sends no CSRF token", async () => {
    const { auth: a, calls } = auth({ "GET /v1/auth/setup/status": [{ body: { owner_missing: true } }] });
    await a.setupStatus();
    expect(a.state()).toEqual({ kind: "checking" });
    expect(calls[0]?.headers["X-CSRF-Token"]).toBeUndefined();
  });
});

describe("login", () => {
  it("a password that needs a second step leaves the state alone and returns the challenge", async () => {
    const { auth: a } = auth({
      "POST /v1/auth/login": () => ({ body: { status: "mfa_required", challenge: "nmc_obviously-fake-challenge", methods: ["totp", "recovery_code"], expires_in: 300 } }),
    });
    const step = await a.login("ada@example.org", "obviously-fake-test-password");
    expect(step).toEqual({ kind: "mfa", challenge: "nmc_obviously-fake-challenge", methods: ["totp", "recovery_code"], expiresIn: 300 });
    expect(a.state().kind).toBe("checking");
  });

  it("an enrolment-required answer is its own step and carries the challenge for the enrolment screens", async () => {
    const { auth: a } = auth({
      "POST /v1/auth/login": () => ({ body: { status: "mfa_enroll_required", challenge: "nmc_obviously-fake-challenge", expires_in: 300 } }),
    });
    expect(await a.login("ada@example.org", "obviously-fake-test-password")).toEqual({ kind: "mfa-enroll", challenge: "nmc_obviously-fake-challenge", expiresIn: 300 });
    expect(a.state().kind).toBe("checking");
  });

  it("an unknown 200 answer is refused as unexpected_answer and changes nothing", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => ({ body: { status: "weird" } }) });
    await expect(a.login("ada@example.org", "obviously-fake-test-password")).rejects.toMatchObject({ status: 200, title: "unexpected_answer" });
    expect(a.state().kind).toBe("checking");
  });

  it("a plain login returns signed-in", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => session() });
    expect(await a.login("ada@example.org", "obviously-fake-test-password")).toEqual({ kind: "signed-in" });
  });

  it("completeMfa posts the challenge and a code, then signs in", async () => {
    const { auth: a, calls } = auth({ "POST /v1/auth/login/mfa": () => session() });
    await a.completeMfa("nmc_obviously-fake-challenge", { code: "123456" });
    expect(calls[0]?.body).toEqual({ challenge: "nmc_obviously-fake-challenge", code: "123456" });
    expect(a.state().kind).toBe("signed-in");
  });

  it("completeMfa sends a recovery code as recovery_code and never both", async () => {
    const { auth: a, calls } = auth({ "POST /v1/auth/login/mfa": () => session() });
    await a.completeMfa("nmc_obviously-fake-challenge", { recoveryCode: "obviously-fake-recovery" });
    expect(calls[0]?.body).toEqual({ challenge: "nmc_obviously-fake-challenge", recovery_code: "obviously-fake-recovery" });
    expect(a.state().kind).toBe("signed-in");
  });

  it("completeMfa rejects with the server's 401 and stays signed out", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login/mfa": () => problem(401, "invalid_credentials") });
    await expect(a.completeMfa("c", { code: "000000" })).rejects.toMatchObject({ status: 401 });
    expect(a.state().kind).toBe("checking");
  });

  it("posts the email and password and becomes signed in", async () => {
    const { auth: a, calls } = auth({ "POST /v1/auth/login": () => session() });
    await a.login("ada@example.org", "obviously-fake-test-password");
    expect(calls[0]?.body).toEqual({ email: "ada@example.org", password: "obviously-fake-test-password" });
    expect(a.state().kind).toBe("signed-in");
  });

  it("rejects with the server's 401 and stays signed out", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => problem(401, "invalid_credentials") });
    await expect(a.login("a@b.co", "x")).rejects.toMatchObject({ status: 401, title: "invalid_credentials" });
    expect(a.state().kind).toBe("checking");
  });

  it("carries Retry-After from a 429 lockout", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => problem(429, "too_many_attempts", { "retry-after": "240" }) });
    const error: unknown = await a.login("a@b.co", "x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect(error).toMatchObject({ status: 429, title: "too_many_attempts", retryAfter: 240 });
  });

  it("ignores a Retry-After that is not whole seconds", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => problem(429, "rate_limited", { "retry-after": "Wed, 21 Oct 2026 07:28:00 GMT" }) });
    const error: unknown = await a.login("a@b.co", "x").catch((e: unknown) => e);
    expect((error as ProblemError).retryAfter).toBeUndefined();
  });

  it("keeps nothing in browser storage", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => session("csrf-secret-ish") });
    await a.login("ada@example.org", "obviously-fake-test-password");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});

describe("CSRF and renewal on ordinary calls", () => {
  const STOP = "POST /v1/runs/run-1/stop";

  it("sends the readable CSRF cookie as X-CSRF-Token on writes, and on no read", async () => {
    setCookie("csrf-from-cookie");
    const server = fakeFetch({
      [STOP]: () => ({ body: { run_id: "run-1", status: "stopping", ledger_seq: 9 } }),
      "GET /v1/runs": () => ({ body: { runs: [], truncated: false } }),
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.runs.listPage();
    await client.runs.stop("run-1").catch(() => undefined);
    const write = server.calls.find((c) => c.method === "POST");
    const read = server.calls.find((c) => c.method === "GET");
    expect(write?.headers["x-csrf-token"]).toBe("csrf-from-cookie");
    expect(read?.headers["x-csrf-token"]).toBeUndefined();
  });

  it("uses the token from the last sign-in when the cookie can't be read", async () => {
    const server = fakeFetch({
      "POST /v1/auth/login": () => session("csrf-in-memory"),
      [STOP]: () => ({ body: {} }),
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.auth?.login("ada@example.org", "obviously-fake-test-password");
    await client.runs.stop("run-1").catch(() => undefined);
    expect(server.calls.find((c) => c.path.endsWith("/stop"))?.headers["x-csrf-token"]).toBe("csrf-in-memory");
  });

  it("on a 401 renews the session once and repeats the call with the new token", async () => {
    setCookie("csrf-old");
    const server = fakeFetch({
      "GET /v1/runs": [problem(401, "session_invalid"), { body: { runs: [], truncated: false } }],
      "POST /v1/auth/refresh": () => {
        setCookie("csrf-new");
        return session("csrf-new");
      },
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await expect(client.runs.listPage()).resolves.toEqual({ runs: [], truncated: false });
    expect(server.count("POST /v1/auth/refresh")).toBe(1);
    expect(server.calls.find((c) => c.path === "/v1/auth/refresh")?.headers["x-csrf-token"]).toBe("csrf-old");
    expect(server.count("GET /v1/runs")).toBe(2);
  });

  it("never repeats a call more than once", async () => {
    const server = fakeFetch({
      "GET /v1/runs": () => problem(401, "session_invalid"),
      "POST /v1/auth/refresh": () => session("csrf-new"),
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await expect(client.runs.listPage()).rejects.toMatchObject({ status: 401 });
    expect(server.count("GET /v1/runs")).toBe(2);
    expect(server.count("POST /v1/auth/refresh")).toBe(1);
  });

  it("when the renewal is refused, signs out as 'expired' and the call fails with the 401", async () => {
    const server = fakeFetch({
      "POST /v1/auth/login": () => session(),
      "GET /v1/runs": () => problem(401, "session_invalid"),
      "POST /v1/auth/refresh": () => problem(401, "session_invalid"),
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.auth?.login("ada@example.org", "obviously-fake-test-password");
    await expect(client.runs.listPage()).rejects.toMatchObject({ status: 401 });
    expect(client.auth?.state()).toEqual({ kind: "signed-out", reason: "expired" });
    expect(server.count("GET /v1/runs")).toBe(1);
  });

  it("does not try to renew a failed sign-in (the 401 of /v1/auth/ is the answer)", async () => {
    const server = fakeFetch({ "POST /v1/auth/login": () => problem(401, "invalid_credentials") });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.auth?.login("a@b.co", "x").catch(() => undefined);
    expect(server.count("POST /v1/auth/refresh")).toBe(0);
  });

  it("two calls that fail together make one renewal (a refresh token works once)", async () => {
    const server = fakeFetch({
      "GET /v1/runs": [problem(401, "session_invalid"), problem(401, "session_invalid"), { body: { runs: [], truncated: false } }, { body: { runs: [], truncated: false } }],
      "POST /v1/auth/refresh": async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        return session("csrf-new");
      },
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await Promise.all([client.runs.listPage(), client.runs.listPage()]);
    expect(server.count("POST /v1/auth/refresh")).toBe(1);
  });

  it("holds the Web Lock while it renews, and skips the renewal when another tab already did it", async () => {
    setCookie("csrf-old");
    const held: string[] = [];
    // The other tab renews while this one waits for the lock.
    const lock = async <T,>(task: () => Promise<T>): Promise<T> => {
      held.push("lock");
      setCookie("csrf-from-other-tab");
      return task();
    };
    const server = fakeFetch({
      "GET /v1/runs": [problem(401, "session_invalid"), { body: { runs: [], truncated: false } }],
      "POST /v1/auth/refresh": () => session("never"),
    });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock });
    expect(await a.recover()).toBe("renewed");
    expect(held).toEqual(["lock"]);
    expect(server.count("POST /v1/auth/refresh")).toBe(0);
  });

  it("uses navigator.locks when the browser has it", async () => {
    const request = vi.fn(<T,>(_name: string, task: () => Promise<T>) => task());
    vi.stubGlobal("navigator", { userAgent: "test", locks: { request } });
    try {
      const server = fakeFetch({ "POST /v1/auth/refresh": () => session("c") });
      const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn });
      await a.recover();
      expect(request).toHaveBeenCalledWith("nuramesh-session-refresh", expect.any(Function));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("stale CSRF token on any write", () => {
  it("reads the session for the new token and repeats the write once", async () => {
    const server = fakeFetch({
      "POST /v1/runs/r1/stop": [problem(403, "csrf_invalid"), { body: { ok: true } }],
      "GET /v1/auth/session": [session("csrf-old"), session("csrf-new")],
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.auth?.load();
    await client.runs.stop("r1", "why");
    const stops = server.calls.filter((c) => c.path === "/v1/runs/r1/stop");
    expect(stops).toHaveLength(2);
    expect(stops[1]?.headers["x-csrf-token"]).toBe("csrf-new");
  });

  it("does not loop when the server keeps refusing", async () => {
    const server = fakeFetch({
      "POST /v1/runs/r1/stop": () => problem(403, "csrf_invalid"),
      "GET /v1/auth/session": [session("csrf-old"), session("csrf-new")],
    });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await client.auth?.load();
    await expect(client.runs.stop("r1", "why")).rejects.toMatchObject({ status: 403 });
    expect(server.count("POST /v1/runs/r1/stop")).toBe(2);
  });

  it("a 403 that is not csrf_invalid is not retried", async () => {
    const server = fakeFetch({ "POST /v1/runs/r1/stop": () => problem(403, "authentication_required") });
    const client = createServerClient({ fetch: server.fetchFn, socket: () => { throw new Error("no sockets"); } });
    await expect(client.runs.stop("r1", "why")).rejects.toMatchObject({ status: 403 });
    expect(server.count("POST /v1/runs/r1/stop")).toBe(1);
    expect(server.count("GET /v1/auth/session")).toBe(0);
  });
});

describe("without the Web Locks API", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("waits a short random moment, then skips the renewal if another tab already did it", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("navigator", { userAgent: "test" });
    setCookie("csrf-old");
    const server = fakeFetch({ "POST /v1/auth/refresh": () => session("never") });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn });
    const done = a.recover();
    setCookie("csrf-from-other-tab");
    await vi.advanceTimersByTimeAsync(300);
    expect(await done).toBe("renewed");
    expect(server.count("POST /v1/auth/refresh")).toBe(0);
  });

  it("still renews once when nobody else did", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("navigator", { userAgent: "test" });
    const server = fakeFetch({ "POST /v1/auth/refresh": () => session("csrf-2") });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn });
    const done = Promise.all([a.recover(), a.recover()]);
    await vi.advanceTimersByTimeAsync(300);
    expect(await done).toEqual(["renewed", "renewed"]);
    expect(server.count("POST /v1/auth/refresh")).toBe(1);
  });
});

describe("logout and setup", () => {
  it("logs out with the CSRF token and becomes signed out", async () => {
    setCookie("csrf-1");
    const { auth: a, calls } = auth({ "POST /v1/auth/login": () => session("csrf-1"), "POST /v1/auth/logout": () => ({ status: 204 }) });
    await a.login("ada@example.org", "obviously-fake-test-password");
    await a.logout();
    expect(calls.at(-1)?.headers["x-csrf-token"]).toBe("csrf-1");
    expect(a.state()).toEqual({ kind: "signed-out", reason: "logged-out" });
  });

  it("after a stale-token 403 reads the session for the new token and says it again", async () => {
    const { auth: a, count } = auth({
      "POST /v1/auth/login": () => session("csrf-1"),
      "POST /v1/auth/logout": [problem(403, "csrf_invalid"), { status: 204 }],
      "GET /v1/auth/session": () => session("csrf-2"),
    });
    await a.login("ada@example.org", "obviously-fake-test-password");
    await a.logout();
    expect(count("POST /v1/auth/logout")).toBe(2);
    expect(a.state()).toEqual({ kind: "signed-out", reason: "logged-out" });
  });

  it("a 401 on logout means already signed out", async () => {
    const { auth: a } = auth({ "POST /v1/auth/login": () => session(), "POST /v1/auth/logout": () => problem(401, "session_invalid") });
    await a.login("ada@example.org", "obviously-fake-test-password");
    await a.logout();
    expect(a.state()).toEqual({ kind: "signed-out", reason: "logged-out" });
  });

  it("an unreachable server on logout keeps the person signed in and says so", async () => {
    const { auth: a, fetchFn } = auth({ "POST /v1/auth/login": () => session() });
    await a.login("ada@example.org", "obviously-fake-test-password");
    fetchFn.mockRejectedValueOnce(new TypeError("offline"));
    await expect(a.logout()).rejects.toMatchObject({ status: 0 });
    expect(a.state().kind).toBe("signed-in");
  });

  it("redeems a setup token, sending the display name only when given, and signs nobody in", async () => {
    const { auth: a, calls } = auth({ "POST /v1/auth/setup/redeem": () => ({ status: 201, body: { user: USER } }) });
    await a.redeemSetup({ token: "obviously-fake-setup-token", email: "ada@example.org", displayName: "  ", password: "obviously-fake-test-password" });
    expect(calls[0]?.body).toEqual({ token: "obviously-fake-setup-token", email: "ada@example.org", password: "obviously-fake-test-password" });
    expect(a.state().kind).toBe("checking");
    await a.redeemSetup({ token: "t", email: "e@e.co", displayName: " Ada ", password: "p" });
    expect(calls[1]?.body).toMatchObject({ display_name: "Ada" });
  });

  it("a refused token is the server's 403", async () => {
    const { auth: a } = auth({ "POST /v1/auth/setup/redeem": () => problem(403, "invalid_setup_token") });
    await expect(a.redeemSetup({ token: "t", email: "e@e.co", displayName: "", password: "p" })).rejects.toMatchObject({ status: 403 });
  });
});

describe("review fixes (round 1)", () => {
  it("recover tells refused from failed: 401 is refused, 503 and a dead network are failed", async () => {
    const refused = auth({ "POST /v1/auth/refresh": () => problem(401, "session_invalid") });
    expect(await refused.auth.recover()).toBe("refused");
    const busy = auth({ "POST /v1/auth/refresh": () => problem(503, "unavailable") });
    expect(await busy.auth.recover()).toBe("failed");
    const limited = auth({ "POST /v1/auth/refresh": () => problem(429, "rate_limited", { "retry-after": "5" }) });
    expect(await limited.auth.recover()).toBe("failed");
    const down = auth({});
    expect(await down.auth.recover()).toBe("failed");
  });

  it("a tab that skipped its renewal re-reads the session and takes a different person", async () => {
    setCookie("csrf-old");
    const other = { ...USER, id: "human:u_other", display_name: "Grace", email: "grace@example.org" };
    const lock = async <T,>(task: () => Promise<T>): Promise<T> => {
      setCookie("csrf-from-other-tab");
      return task();
    };
    const server = fakeFetch({
      "GET /v1/auth/session": [session("csrf-old"), { body: { user: other, session: { id: "s2" }, csrf_token: "csrf-from-other-tab" } }],
    });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock });
    await a.load();
    expect(await a.recover()).toBe("renewed");
    expect(a.state()).toMatchObject({ kind: "signed-in", user: { id: "human:u_other", displayName: "Grace" } });
  });

  it("a hanging refresh is cut off, releases the lock and reports failed", async () => {
    vi.useFakeTimers();
    try {
      const fetchFn = vi.fn((_input: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => { reject(new DOMException("aborted", "AbortError")); });
        }),
      );
      let released = false;
      const lock = async <T,>(task: () => Promise<T>): Promise<T> => {
        try {
          return await task();
        } finally {
          released = true;
        }
      };
      const a = createServerAuth({ baseUrl: "", fetch: fetchFn, lock });
      const done = a.recover();
      await vi.advanceTimersByTimeAsync(20_000);
      expect(await done).toBe("failed");
      expect(released).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses only the __Host- CSRF cookie when it exists; the plain name only when there is no __Host- one", () => {
    const jar = vi.spyOn(document, "cookie", "get");
    try {
      const a = createServerAuth({ baseUrl: "", fetch: fakeFetch({}).fetchFn, lock: noLock });
      jar.mockReturnValue("nm_csrf=planted-plain; __Host-nm_csrf=real-host");
      expect(a.csrf()).toBe("real-host");
      jar.mockReturnValue("__Host-nm_csrf=real-host; nm_csrf=planted-plain");
      expect(a.csrf()).toBe("real-host");
      jar.mockReturnValue("nm_csrf=plain-dev");
      expect(a.csrf()).toBe("plain-dev");
    } finally {
      jar.mockRestore();
    }
  });

  it("a late 401 after Sign out keeps the 'logged-out' reason", async () => {
    setCookie("csrf-1");
    const { auth: a } = auth({
      "POST /v1/auth/login": () => session("csrf-1"),
      "POST /v1/auth/logout": () => ({ status: 204 }),
      "POST /v1/auth/refresh": () => problem(401, "session_invalid"),
    });
    await a.login("ada@example.org", "obviously-fake-test-password");
    await a.logout();
    expect(await a.recover()).toBe("refused");
    expect(a.state()).toEqual({ kind: "signed-out", reason: "logged-out" });
  });
});

/** A fake BroadcastChannel network: every channel of a name hears the others, never itself. */
function fakeHub() {
  const channels = new Set<{ onmessage: (() => void) | null; id: number }>();
  let next = 0;
  class Fake {
    onmessage: (() => void) | null = null;
    id = next++;
    constructor() {
      channels.add(this);
    }
    postMessage(): void {
      for (const other of channels) if (other.id !== this.id) other.onmessage?.();
    }
    close(): void {
      channels.delete(this);
    }
  }
  return Fake;
}

const OTHER = { id: "human:u_other", role: "viewer", email: "grace@example.org", display_name: "Grace" };

describe("two tabs, one browser (identity changes)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function twoTabs() {
    const Hub = fakeHub();
    vi.stubGlobal("BroadcastChannel", Hub);
    const { browserTabSync } = await import("./tab-sync");
    // tab A asks as Ada first; later the server's answer is Grace's (tab B signed in as Grace)
    const serverA = fakeFetch({
      "GET /v1/auth/session": [session("c1"), { body: { user: OTHER, session: { id: "s2" }, csrf_token: "c2" } }],
    });
    const a = createServerAuth({ baseUrl: "", fetch: serverA.fetchFn, lock: noLock, sync: browserTabSync() });
    const serverB = fakeFetch({
      "POST /v1/auth/login": () => ({ body: { user: OTHER, session: { id: "s2" }, csrf_token: "c2" } }),
      "POST /v1/auth/logout": () => ({ status: 204 }),
    });
    const b = createServerAuth({ baseUrl: "", fetch: serverB.fetchFn, lock: noLock, sync: browserTabSync() });
    await a.load();
    return { a, b };
  }

  it("tab A never shows Ada again once tab B signed in as Grace", async () => {
    const { a, b } = await twoTabs();
    expect(a.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Ada" } });
    const seen: string[] = [];
    a.subscribe(() => {
      const st = a.state();
      seen.push(st.kind === "signed-in" ? st.user.displayName : st.kind);
    });
    await b.login("grace@example.org", "obviously-fake-test-password");
    await vi.waitFor(() => {
      expect(a.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
    });
    expect(seen[0]).toBe("checking"); // the very first thing A did was to hide Ada
    expect(seen).not.toContain("Ada");
  });

  it("a tab does not react to its own sign-in or sign-out", async () => {
    vi.stubGlobal("BroadcastChannel", fakeHub());
    const { browserTabSync } = await import("./tab-sync");
    const server = fakeFetch({ "POST /v1/auth/login": () => session("c"), "POST /v1/auth/logout": () => ({ status: 204 }) });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, sync: browserTabSync() });
    const seen: string[] = [];
    a.subscribe(() => seen.push(a.state().kind));
    await a.login("ada@example.org", "obviously-fake-test-password");
    await a.logout();
    expect(seen).toEqual(["signed-in", "signed-out"]);
    expect(server.count("GET /v1/auth/session")).toBe(0);
  });

  it("signing out in one tab signs out the other", async () => {
    const { a, b } = await twoTabs();
    await b.login("grace@example.org", "obviously-fake-test-password");
    await vi.waitFor(() => {
      expect(a.state().kind).toBe("signed-in");
    });
    // the server now has no session for the browser
    const gone = fakeFetch({ "GET /v1/auth/session": () => problem(401, "session_invalid"), "POST /v1/auth/refresh": () => problem(401, "session_invalid") });
    const Hub = fakeHub();
    vi.stubGlobal("BroadcastChannel", Hub);
    const { browserTabSync } = await import("./tab-sync");
    const c = createServerAuth({ baseUrl: "", fetch: gone.fetchFn, lock: noLock, sync: browserTabSync() });
    const d = createServerAuth({ baseUrl: "", fetch: fakeFetch({ "POST /v1/auth/login": () => session("x"), "POST /v1/auth/logout": () => ({ status: 204 }) }).fetchFn, lock: noLock, sync: browserTabSync() });
    await d.login("ada@example.org", "obviously-fake-test-password");
    await d.logout();
    await vi.waitFor(() => {
      expect(c.state().kind).toBe("signed-out");
    });
  });

  it("a slow answer for the old person that arrives after the new answer is dropped", async () => {
    // a tab starts a load whose answer (Ada) is held back while another tab signs in as Grace
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("BroadcastChannel", fakeHub());
    const { browserTabSync } = await import("./tab-sync");
    let calls = 0;
    const fetchFn = vi.fn(async (): Promise<Response> => {
      calls += 1;
      const mine = calls;
      if (mine === 1) await held; // the first request is the old, slow one
      const user = mine === 1 ? USER : OTHER;
      return new Response(JSON.stringify({ user, session: { id: "s" }, csrf_token: `c${String(mine)}` }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    const c = createServerAuth({ baseUrl: "", fetch: fetchFn, lock: noLock, sync: browserTabSync() });
    const d = createServerAuth({
      baseUrl: "",
      fetch: fakeFetch({ "POST /v1/auth/login": () => session("x") }).fetchFn,
      lock: noLock,
      sync: browserTabSync(),
    });
    const first = c.load(); // in flight, will answer Ada late
    await d.login("grace@example.org", "obviously-fake-test-password"); // C hears it and asks again
    await vi.waitFor(() => {
      expect(c.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
    });
    release();
    await first;
    expect(c.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
  });

  it("a refresh answer that arrives after another tab changed the person does not overwrite the newer state", async () => {
    vi.stubGlobal("BroadcastChannel", fakeHub());
    const { browserTabSync } = await import("./tab-sync");
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const reply = (status: number, body: unknown): Response =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } });
    let sessions = 0;
    const fetchFn = vi.fn(async (input: string): Promise<Response> => {
      const path = new URL(input, "http://127.0.0.1:5173").pathname;
      if (path === "/v1/auth/session") {
        sessions += 1;
        return sessions === 1
          ? reply(200, { user: USER, session: { id: "s" }, csrf_token: "c1" })
          : reply(200, { user: OTHER, session: { id: "s2" }, csrf_token: "c2" });
      }
      if (path === "/v1/auth/refresh") {
        await held; // the old refresh: answered late, and refused
        return reply(401, { title: "session_invalid", status: 401, detail: "x" });
      }
      return reply(404, { title: "not_found", status: 404, detail: "x" });
    });
    const tab = createServerAuth({ baseUrl: "", fetch: fetchFn, lock: noLock, sync: browserTabSync() });
    const other = createServerAuth({
      baseUrl: "",
      fetch: fakeFetch({ "POST /v1/auth/login": () => session("x") }).fetchFn,
      lock: noLock,
      sync: browserTabSync(),
    });
    await tab.load();
    const late = tab.recover(); // in flight for Ada
    await other.login("grace@example.org", "obviously-fake-test-password"); // another tab changed the person
    await vi.waitFor(() => {
      expect(tab.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
    });
    release();
    await late;
    expect(tab.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
  });

  it("a tab that learns the session is gone from another tab says 'expired', not a bare login", async () => {
    vi.stubGlobal("BroadcastChannel", fakeHub());
    const { browserTabSync } = await import("./tab-sync");
    const gone = fakeFetch({
      "GET /v1/auth/session": [session("c1"), problem(401, "session_invalid")],
      "POST /v1/auth/refresh": () => problem(401, "session_invalid"),
    });
    const tabB = createServerAuth({ baseUrl: "", fetch: gone.fetchFn, lock: noLock, sync: browserTabSync() });
    const tabA = createServerAuth({
      baseUrl: "",
      fetch: fakeFetch({ "POST /v1/auth/logout": () => ({ status: 204 }), "POST /v1/auth/login": () => session("x") }).fetchFn,
      lock: noLock,
      sync: browserTabSync(),
    });
    await tabB.load();
    await tabA.login("ada@example.org", "obviously-fake-test-password");
    await tabA.logout();
    await vi.waitFor(() => {
      expect(tabB.state().kind).toBe("signed-out");
    });
    expect(tabB.state()).toEqual({ kind: "signed-out", reason: "expired" });
  });

  it("without BroadcastChannel the storage event carries only an opaque counter", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const { browserTabSync } = await import("./tab-sync");
    const server = fakeFetch({
      "GET /v1/auth/session": [session("c1"), { body: { user: OTHER, session: { id: "s2" }, csrf_token: "c2" } }],
    });
    const a = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, sync: browserTabSync() });
    await a.load();
    const sender = browserTabSync();
    sender.notify();
    const stored = window.localStorage.getItem("nuramesh-auth-generation");
    expect(stored).not.toBeNull();
    expect(stored).not.toMatch(/ada|grace|human:|csrf/i);
    window.dispatchEvent(new StorageEvent("storage", { key: "nuramesh-auth-generation", newValue: stored }));
    expect(a.state().kind).toBe("checking");
    await vi.waitFor(() => {
      expect(a.state()).toMatchObject({ kind: "signed-in", user: { displayName: "Grace" } });
    });
    window.localStorage.clear();
  });
});

describe("mailEnabled (the public health answer)", () => {
  const make = (routes: Record<string, Handler | Reply[]>) =>
    createServerAuth({ baseUrl: "", fetch: fakeFetch(routes).fetchFn });

  it("is true only for a real true", async () => {
    expect(await make({ "GET /v1/health": () => ({ body: { mail_enabled: true } }) }).mailEnabled?.()).toBe(true);
  });

  it.each([
    ["false", { body: { mail_enabled: false } }],
    ["a missing field", { body: { status: "ok" } }],
    ["a non-boolean", { body: { mail_enabled: "true" } }],
    ["a limit", { status: 429, body: {} }],
    ["a server error", { status: 500, body: {} }],
  ] as [string, Reply][])("is false for %s", async (_name, reply) => {
    expect(await make({ "GET /v1/health": () => reply }).mailEnabled?.()).toBe(false);
  });

  it("is false when the server cannot be reached", async () => {
    expect(await make({}).mailEnabled?.()).toBe(false);
  });
});
