import { describe, expect, it } from "vitest";
import {
  API_VERSION,
  ERROR_CODES,
  NuraMeshClient,
  NuraMeshError,
  OPERATIONS,
  isErrorCode,
} from "../src/index.js";

interface Seen {
  url: string;
  method: string;
  headers: Headers;
  body: string | null;
}

/** A fake server: records each request, answers from a queue. No network. */
function fake(
  ...answers: (Response | Error)[]
): { fetch: typeof fetch; seen: Seen[] } {
  const seen: Seen[] = [];
  const queue: (Response | Error)[] = [...answers];
  const fetchFake = (input: URL | RequestInfo, init?: RequestInit): Promise<Response> => {
    seen.push({
      url: input instanceof Request ? input.url : input.toString(),
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? init.body : null,
    });
    const next = queue.shift();
    if (next === undefined) return Promise.reject(new Error("no more answers"));
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  };
  return { fetch: fetchFake, seen };
}

const json = (body: unknown, status = 200, type = "application/json"): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": type } });

const problem = (code: string, status: number, detail: string, extra = {}): Response =>
  json({ type: `urn:nuramesh:${code}`, title: code, status, detail, ...extra }, status, "application/problem+json");

describe("requests", () => {
  it("fills path parameters (encoded) and query strings, skipping undefined", async () => {
    const f = fake(json({ ok: true }));
    const c = new NuraMeshClient({ baseUrl: "http://127.0.0.1:8765/", fetch: f.fetch });
    await c.getRun({ path: { run_id: "run a/b" } });
    expect(f.seen[0]?.url).toBe("http://127.0.0.1:8765/v1/runs/run%20a%2Fb");
    expect(f.seen[0]?.method).toBe("GET");
  });

  it("sends JSON bodies with a content type and the bearer token", async () => {
    const f = fake(json({}));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch, token: "obviously-fake-test-key-not-a-secret" });
    await c.login({ body: { email: "a@b.c", password: "xxxxxxxxxxxx" } });
    const s = f.seen[0];
    expect(s?.method).toBe("POST");
    expect(s?.headers.get("content-type")).toBe("application/json");
    expect(s?.headers.get("authorization")).toBe("Bearer obviously-fake-test-key-not-a-secret");
    expect(JSON.parse(s?.body ?? "null")).toEqual({ email: "a@b.c", password: "xxxxxxxxxxxx" });
  });

  it("sends no telemetry headers and nothing but the request", async () => {
    const f = fake(json({}));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    await c.getHealth();
    expect(f.seen).toHaveLength(1);
    expect([...(f.seen[0]?.headers.keys() ?? [])]).toEqual(["accept"]);
  });

  it("catalog pause keeps its optional Idempotency-Key; halt and role pause take none", async () => {
    const f = fake(json({}), json({}), json({}));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    await c.pauseCatalogEntry({ body: { ref: "x" } as never, headers: { "Idempotency-Key": "k1" } });
    expect(f.seen[0]?.headers.get("idempotency-key")).toBe("k1");
    await c.haltOrg();
    expect(f.seen[1]?.headers.has("idempotency-key")).toBe(false);
    // @ts-expect-error halt and role pause accept no Idempotency-Key argument
    await c.pauseOrgRole({ path: { role: "r" }, headers: { "Idempotency-Key": "k3" } });
  });

  it("every operation has a typed method on the client", () => {
    const c = new NuraMeshClient({ baseUrl: "http://h" }) as unknown as Record<string, unknown>;
    expect(Object.keys(OPERATIONS).length).toBeGreaterThan(60);
    for (const name of Object.keys(OPERATIONS)) expect(typeof c[name], name).toBe("function");
    expect(API_VERSION).toBe("1");
  });
});

describe("errors", () => {
  it("turns application/problem+json into a NuraMeshError with the code", async () => {
    const f = fake(problem("not_found", 404, "no such run"));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    const err = await c.getRun({ path: { run_id: "x" } }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(NuraMeshError);
    const e = err as NuraMeshError;
    expect([e.status, e.code, e.detail]).toEqual([404, "not_found", "no such run"]);
    expect(isErrorCode(e, "not_found")).toBe(true);
    expect(isErrorCode(e, "conflict")).toBe(false);
  });

  it("keeps extra problem fields (entitlement_required carries key)", async () => {
    const f = fake(problem("entitlement_required", 403, "not granted", { key: "sso" }));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    const e = (await c.getHealth().catch((x: unknown) => x)) as NuraMeshError;
    expect(e.code).toBe("entitlement_required");
    expect(e.extra).toEqual({ key: "sso" });
  });

  it("maps FastAPI validation answers and non-JSON failures to stable codes", async () => {
    const f = fake(
      json({ detail: [{ loc: ["body"], msg: "bad", type: "x" }] }, 422),
      new Response("<html>bad gateway</html>", { status: 502, headers: { "content-type": "text/html" } }),
    );
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    const a = (await c.getHealth().catch((x: unknown) => x)) as NuraMeshError;
    const b = (await c.getHealth().catch((x: unknown) => x)) as NuraMeshError;
    expect([a.status, a.code]).toEqual([422, "invalid_request"]);
    expect([b.status, b.code]).toEqual([502, "unknown_error"]);
  });

  it("wraps network failures as network_error and honours abort", async () => {
    const f = fake(new TypeError("fetch failed"));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    const e = (await c.getHealth().catch((x: unknown) => x)) as NuraMeshError;
    expect([e.status, e.code]).toEqual([0, "network_error"]);
  });

  it("lists the known codes", () => {
    for (const c of ["not_found", "conflict", "rate_limited", "entitlement_required"]) {
      expect(ERROR_CODES).toContain(c);
    }
  });
});

describe("responses", () => {
  it("returns parsed JSON, and undefined for an empty 204", async () => {
    const f = fake(json({ ok: true, version: "1" }), new Response(null, { status: 204 }));
    const c = new NuraMeshClient({ baseUrl: "http://h", fetch: f.fetch });
    expect(await c.getHealth()).toEqual({ ok: true, version: "1" });
    expect(await c.logout({ body: {} })).toBeUndefined();
  });
});
