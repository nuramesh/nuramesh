import { afterEach, describe, expect, it } from "vitest";

import { fakeFetch, NO_STORE, noLock, PASSWORD, problem, session } from "@/mfa/mfa-test-kit";

import { createServerAuth } from "./server-auth";

function setCookie(value: string | null): void {
  document.cookie = value === null ? "nm_csrf=; Max-Age=0; Path=/" : `nm_csrf=${value}; Path=/`;
}
afterEach(() => {
  setCookie(null);
});

const KEY = "POST /v1/auth/password/confirm";
const make = (routes: Parameters<typeof fakeFetch>[0]) => {
  const server = fakeFetch(routes);
  return { ...server, auth: createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock }) };
};

describe("confirmPassword: the fresh proof of a solo approval (SA-8)", () => {
  it("posts only the password, with the CSRF header and no idempotency key", async () => {
    setCookie("csrf-one");
    const { auth, calls } = make({ [KEY]: [{ headers: NO_STORE, body: { expires_in: 600 } }] });
    await expect(auth.confirmPassword(PASSWORD)).resolves.toBeUndefined();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toEqual({ password: PASSWORD });
    expect(calls[0]?.headers["x-csrf-token"]).toBe("csrf-one");
    expect(calls[0]?.headers["idempotency-key"]).toBeUndefined();
  });

  it("a wrong password and a lock reject with the server's code; nothing is sent twice", async () => {
    const wrong = make({ [KEY]: [problem(401, "invalid_credentials")] });
    await expect(wrong.auth.confirmPassword(PASSWORD)).rejects.toMatchObject({ status: 401, title: "invalid_credentials" });
    expect(wrong.count(KEY)).toBe(1);
    const locked = make({ [KEY]: [problem(429, "too_many_attempts", { "retry-after": "240" })] });
    await expect(locked.auth.confirmPassword(PASSWORD)).rejects.toMatchObject({ status: 429, title: "too_many_attempts", retryAfter: 240 });
    expect(locked.count(KEY)).toBe(1);
  });

  it("repeats once with the session's token after 403 csrf_invalid", async () => {
    setCookie("csrf-one");
    const { auth, count } = make({ [KEY]: [problem(403, "csrf_invalid"), { headers: NO_STORE, body: { expires_in: 600 } }], "GET /v1/auth/session": [session("csrf-two")] });
    await expect(auth.confirmPassword(PASSWORD)).resolves.toBeUndefined();
    expect(count(KEY)).toBe(2);
  });
});
