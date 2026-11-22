/** Shared by the MFA tests: a fake server for `fetch`, and obviously fake secrets. Not shipped (only tests import it). */
import { vi } from "vitest";

export interface Reply {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}
export interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}
export type Handler = (call: Call) => Reply | Promise<Reply>;

/** Routes "METHOD /path" to a handler (a list answers one reply per call, the last one repeating) and records every call. */
export function fakeFetch(routes: Record<string, Handler | Reply[]>) {
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
    return new Response(JSON.stringify(reply.body ?? {}), {
      status,
      headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json", ...(reply.headers ?? {}) },
    });
  });
  return { fetchFn, calls, count: (key: string) => calls.filter((c) => `${c.method} ${c.path}` === key).length };
}

export const problem = (status: number, title: string, headers?: Record<string, string>): Reply => ({
  status,
  body: { title, status, detail: `${title} detail` },
  ...(headers === undefined ? {} : { headers }),
});

export const NO_STORE = { "cache-control": "no-store" };

/** Ten recovery codes in the display pattern, each a repeated character: obviously fake. */
export const CODES: string[] = ["B", "C", "D", "F", "G", "H", "J", "K", "L", "M"].map((c) => [c.repeat(4), c.repeat(4), c.repeat(4), c.repeat(4)].join("-"));
/** A setup key that is obviously fake (one repeated character). */
export const SECRET = "A".repeat(32);
export const URI = `otpauth://totp/NuraMesh:ada%40example.org?secret=${SECRET}&issuer=NuraMesh`;
export const PASSWORD = "obviously-fake-test-password";
export const FACTOR = "mf_" + "0".repeat(20);
export const USER = { id: "human:u_0a1b2c3d4e5f60718293", org_id: "acme", role: "owner", email: "ada@example.org", display_name: "Ada" };
export const session = (csrf: string | null = "csrf-one"): Reply => ({ body: { user: USER, session: { id: "s" }, csrf_token: csrf } });
export const noLock = <T>(task: () => Promise<T>): Promise<T> => task();

export const status = (over: Record<string, unknown> = {}): Reply => ({
  headers: NO_STORE,
  body: { available: true, enrolled: false, confirmed_at: null, recovery_codes_remaining: 0, required: false, ...over },
});
export const enrolled = (over: Record<string, unknown> = {}): Reply => status({ enrolled: true, confirmed_at: "2026-11-13T10:00:00+00:00", recovery_codes_remaining: 10, ...over });
export const enrollAnswer = (): Reply => ({ headers: NO_STORE, body: { factor_id: FACTOR, secret: SECRET, otpauth_uri: URI } });
export const codesAnswer = (): Reply => ({ headers: NO_STORE, body: { recovery_codes: CODES } });

/** A sign-in challenge in the server's shape (`nmc_` plus an obviously fake body). */
export const CHALLENGE = "nmc_" + "0".repeat(20);
export const mfaRequired = (over: Record<string, unknown> = {}): Reply => ({
  headers: NO_STORE,
  body: { status: "mfa_required", challenge: CHALLENGE, methods: ["totp", "recovery_code"], expires_in: 300, ...over },
});
export const enrollRequired = (over: Record<string, unknown> = {}): Reply => ({
  headers: NO_STORE,
  body: { status: "mfa_enroll_required", challenge: CHALLENGE, expires_in: 300, ...over },
});
/** The answer of the login-step calls that end in a session. */
export const loginConfirmAnswer = (over: Record<string, unknown> = {}): Reply => ({
  headers: NO_STORE,
  body: { user: USER, csrf_token: "csrf-one", recovery_codes: CODES, ...over },
});
