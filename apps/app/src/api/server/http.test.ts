import { describe, expect, it, vi } from "vitest";

import { createHttp, MAX_PENDING_KEYS, PENDING_KEY_WINDOW_MS } from "./http";

const ok = () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
const status = (code: number, title = "x") => new Response(JSON.stringify({ title, status: code }), { status: code, headers: { "content-type": "application/problem+json" } });
const keys = (fetchFn: ReturnType<typeof vi.fn>) => fetchFn.mock.calls.map((c) => ((c[1] as RequestInit).headers as Record<string, string>)["idempotency-key"]);

describe("Idempotency-Key on keyed writes (W2)", () => {
  it("sends no key unless asked, and never on a GET", async () => {
    const fetchFn = vi.fn(() => Promise.resolve(ok()));
    const http = createHttp("", fetchFn);
    await http.post("/v1/org/halt", {});
    await http.get("/v1/org/status");
    expect(keys(fetchFn)).toEqual([undefined, undefined]);
  });

  it("reuses the key on a retry after a network failure and after a 5xx, then drops it once answered", async () => {
    const fetchFn = vi.fn<(...a: unknown[]) => Promise<Response>>();
    fetchFn.mockRejectedValueOnce(new TypeError("down")).mockResolvedValueOnce(status(503)).mockResolvedValueOnce(ok()).mockResolvedValueOnce(ok());
    const http = createHttp("", fetchFn);
    await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toMatchObject({ status: 0 });
    await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toMatchObject({ status: 503 });
    await http.post("/v1/org/resume", {}, { keyed: true });
    await http.post("/v1/org/resume", {}, { keyed: true });
    const [a, b, c, d] = keys(fetchFn as never);
    expect(a).toMatch(/^[0-9a-f-]{36}$/);
    expect([b, c]).toEqual([a, a]);
    expect(d).not.toBe(a);
  });

  it("a refusal (409) ends the action: the next click is a new action with a new key", async () => {
    const fetchFn = vi.fn<(...a: unknown[]) => Promise<Response>>();
    fetchFn.mockResolvedValueOnce(status(409, "already_running")).mockResolvedValueOnce(ok());
    const http = createHttp("", fetchFn);
    await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toBeDefined();
    await http.post("/v1/org/resume", {}, { keyed: true });
    const [a, b] = keys(fetchFn as never);
    expect(a).toBeDefined();
    expect(b).not.toBe(a);
  });

  it("a different body is a different action", async () => {
    const fetchFn = vi.fn(() => Promise.resolve(ok()));
    const http = createHttp("", fetchFn);
    fetchFn.mockRejectedValueOnce(new TypeError("down"));
    await expect(http.post("/v1/budgets/b/unblock", { reason: "a" }, { keyed: true })).rejects.toBeDefined();
    await http.post("/v1/budgets/b/unblock", { reason: "b" }, { keyed: true });
    const [a, b] = keys(fetchFn);
    expect(b).not.toBe(a);
  });

  it("the repeat after a 401 renewal carries the same key", async () => {
    const fetchFn = vi.fn<(...a: unknown[]) => Promise<Response>>();
    fetchFn.mockResolvedValueOnce(status(401, "authentication_required")).mockResolvedValueOnce(ok());
    const http = createHttp("", fetchFn, { recover: () => Promise.resolve("renewed" as const) });
    await http.post("/v1/org/resume", {}, { keyed: true });
    const [a, b] = keys(fetchFn as never);
    expect(a).toBeDefined();
    expect(b).toBe(a);
  });

  it("remembers at most MAX_PENDING_KEYS unanswered requests, the oldest is forgotten first", async () => {
    const fetchFn = vi.fn(() => Promise.reject(new TypeError("down")));
    const http = createHttp("", fetchFn);
    const total = MAX_PENDING_KEYS + 5;
    for (let n = 0; n < total; n++) await expect(http.post(`/v1/org/resume/${String(n)}`, {}, { keyed: true })).rejects.toBeDefined();
    const first = keys(fetchFn as never);
    // The newest request still repeats with its key; the oldest, pushed out, gets a new one.
    await expect(http.post(`/v1/org/resume/${String(total - 1)}`, {}, { keyed: true })).rejects.toBeDefined();
    await expect(http.post("/v1/org/resume/0", {}, { keyed: true })).rejects.toBeDefined();
    const all = keys(fetchFn as never);
    expect(all[total]).toBe(first[total - 1]);
    expect(all[total + 1]).not.toBe(first[0]);
  });

  it("a pending key is reused only inside the server lease window; a later action gets a new key", async () => {
    vi.useFakeTimers();
    try {
      const fetchFn = vi.fn<(...a: unknown[]) => Promise<Response>>();
      fetchFn.mockRejectedValueOnce(new TypeError("lost answer")).mockResolvedValue(ok());
      const http = createHttp("", fetchFn);
      await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toMatchObject({ status: 0 });
      vi.advanceTimersByTime(PENDING_KEY_WINDOW_MS - 1000);
      await http.post("/v1/org/resume", {}, { keyed: true });
      const [a, b] = keys(fetchFn as never);
      expect(b).toBe(a);

      fetchFn.mockRejectedValueOnce(new TypeError("lost answer"));
      await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toMatchObject({ status: 0 });
      vi.advanceTimersByTime(PENDING_KEY_WINDOW_MS + 1000);
      await http.post("/v1/org/resume", {}, { keyed: true });
      const all = keys(fetchFn as never);
      expect(all[3]).not.toBe(all[2]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the key on 409 idempotency_in_progress so the retry hits the same attempt", async () => {
    const fetchFn = vi.fn<(...a: unknown[]) => Promise<Response>>();
    fetchFn.mockResolvedValueOnce(status(409, "idempotency_in_progress")).mockResolvedValueOnce(ok());
    const http = createHttp("", fetchFn);
    await expect(http.post("/v1/org/resume", {}, { keyed: true })).rejects.toMatchObject({ status: 409 });
    await http.post("/v1/org/resume", {}, { keyed: true });
    const [a, b] = keys(fetchFn as never);
    expect(a).toBeDefined();
    expect(b).toBe(a);
  });
});
