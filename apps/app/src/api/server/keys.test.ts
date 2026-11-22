import { describe, expect, it, vi } from "vitest";

import { createServerClient } from "../server-client";

const FAKE_KEY = "obviously-fake-test-key-not-a-secret";

function client(status: number, body: unknown) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchFn = vi.fn((input: string, init?: RequestInit) => {
    calls.push({ method: init?.method ?? "GET", path: input, body: init?.body === undefined ? undefined : JSON.parse(init.body as string) });
    return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } }));
  });
  return { api: createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } }), calls };
}

describe("stored key calls", () => {
  it("setKey PUTs the value once to the key path and maps the answer without a value", async () => {
    const { api, calls } = client(201, { key_id: "k_1", version: 1, state: "current", created_at: "2026-11-08T10:00:00Z" });
    const out = await api.models.setKey({ provider: "openai-compatible", endpointId: "ep one", value: FAKE_KEY, graceSeconds: 60, reason: "first key" });
    expect(calls).toEqual([{ method: "PUT", path: "/v1/models/connections/openai-compatible/key?endpoint_id=ep%20one", body: { value: FAKE_KEY, grace_seconds: 60, reason: "first key" } }]);
    expect(out).toEqual({ keyId: "k_1", version: 1, state: "current", createdAt: "2026-11-08T10:00:00Z", first: true });
    expect(JSON.stringify(out)).not.toContain(FAKE_KEY);
  });

  it("revokeKey sends a DELETE with a JSON body", async () => {
    const { api, calls } = client(200, { key_id: "k_1", revoked_versions: [1, 2] });
    expect(await api.models.revokeKey({ provider: "anthropic", reason: "leaked", version: 2 })).toEqual({ keyId: "k_1", revokedVersions: [1, 2] });
    expect(calls[0]).toEqual({ method: "DELETE", path: "/v1/models/connections/anthropic/key", body: { version: 2, reason: "leaked" } });
  });

  it("keyState maps states and the read-only fields, and 404 is not found", async () => {
    const ok = client(200, { versions: [{ version: 1, state: "current" }], key_id: "k_1", age_days: 120, rotate_advised: true });
    expect(await ok.api.models.keyState({ provider: "anthropic" })).toEqual({ versions: [{ version: 1, state: "current" }], keyId: "k_1", ageDays: 120, rotateAdvised: true });
    const missing = client(404, { title: "unknown_key", detail: "no key", status: 404 });
    await expect(missing.api.models.keyState({ provider: "anthropic" })).rejects.toMatchObject({ code: "not_found" });
  });

  it("is org-scoped by the session only: no call carries an org id of its own", async () => {
    const c = client(200, { versions: [], key_id: "k_1", revoked_versions: [], version: 1, state: "current", created_at: "2026-11-08T10:00:00Z" });
    await c.api.models.keyState({ provider: "anthropic" });
    await c.api.models.setKey({ provider: "anthropic", value: FAKE_KEY, graceSeconds: 0, reason: "r" });
    await c.api.models.revokeKey({ provider: "anthropic", reason: "r" });
    expect(c.calls).toHaveLength(3);
    for (const call of c.calls) {
      expect(call.path).not.toMatch(/org/i);
      expect(JSON.stringify(call.body ?? {})).not.toMatch(/org/i);
    }
  });

  it("keysHealth is ok only for exactly ok", async () => {
    expect(await client(200, { store: "ok" }).api.models.keysHealth()).toBe("ok");
    expect(await client(200, { store: "weird" }).api.models.keysHealth()).toBe("unavailable");
  });
});
