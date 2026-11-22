import { describe, expect, it, vi } from "vitest";

import { answer } from "@/test/contract";

import { InvalidRequestError, NotFoundError, ProblemError } from "../client";
import { isAdmin } from "../models";
import { createServerClient } from "../server-client";

interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

/** Replays one recorded real answer per call: `replay(["add_entry"])` answers the next request with `models.add_entry`. */
function replay(names: string[]) {
  const calls: Call[] = [];
  const queue = [...names];
  const fetchFn = vi.fn((input: string, init?: RequestInit) => {
    const name = queue.shift();
    if (name === undefined) throw new Error(`unexpected request ${init?.method ?? "GET"} ${input}`);
    const recorded = answer<unknown>("models", name);
    calls.push({
      method: init?.method ?? "GET",
      path: input,
      body: init?.body === undefined ? undefined : JSON.parse(init.body as string),
      headers: (init?.headers ?? {}) as Record<string, string>,
    });
    return Promise.resolve(
      new Response(JSON.stringify(recorded.body), {
        status: recorded.status,
        headers: { "content-type": recorded.status >= 400 ? "application/problem+json" : "application/json" },
      }),
    );
  });
  const api = createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } });
  return { api, calls };
}

describe("the catalog (recorded real answers)", () => {
  it("maps the seeded catalog: refs, status, derived data classes, prices, origin", async () => {
    const { api, calls } = replay(["catalog_seeded"]);
    const catalog = await api.models.catalog();
    expect(calls[0]).toMatchObject({ method: "GET", path: "/v1/models/catalog" });
    expect(catalog).toMatchObject({ seeded: true, configDrift: false, ignoredEvents: 0, customerRegions: [] });
    const mini = catalog.entries.find((e) => e.ref === "azure-openai/gpt-5.4-mini");
    expect(mini).toMatchObject({ family: "openai", processing: "global", status: "approved", dataClasses: ["synthetic"], origin: "seed", connected: false });
    expect(mini?.price).toMatchObject({ inputPerMillion: "0.75", outputPerMillion: "4.50", provisional: false });
    const paused = catalog.entries.find((e) => e.ref === "azure-ai/Llama-4-Maverick");
    expect(paused?.status).toBe("paused");
    expect(paused?.price).toBeNull(); // a price not yet in effect is unknown, never guessed
    expect(catalog.entries.find((e) => e.ref === "fake/dev-1")?.dataClasses).toEqual(["synthetic", "customer"]);
  });

  it("maps assignments with where each model comes from", async () => {
    const { api } = replay(["assignments", "assignments_after"]);
    const before = await api.models.assignments();
    expect(before.find((r) => r.subject === "agent=dana")).toMatchObject({ ref: "fake/dev-1", from: "role config", role: "developer" });
    expect(before.find((r) => r.subject === "agent=chief")).toMatchObject({ ref: null, from: "none" });
    const after = await api.models.assignments();
    expect(after.find((r) => r.subject === "agent=dana")).toMatchObject({ ref: "azure-openai/gpt-5.4-mini", from: "agent ledger" });
  });

  it("maps the history with plain words and the reason", async () => {
    const { api } = replay(["events"]);
    const { events, nextBefore } = await api.models.events();
    expect(nextBefore).toBeNull();
    expect(events[0]).toMatchObject({ type: "model.catalog_seeded", summary: "Catalog seeded from the team configuration", reason: null });
    const paused = events.find((e) => e.type === "model.catalog_paused" && e.ref === "azure-openai/gpt-5.4-mini");
    expect(paused?.summary).toBe("Paused azure-openai/gpt-5.4-mini");
    expect(paused?.reason).toBe("under review");
  });
});

describe("a model on an openai-compatible connection (#1485, recorded real answers)", () => {
  const named = { provider: "openai-compatible", endpointId: "local-one", model: "llama3.1" };
  const wire = { provider: "openai-compatible", endpoint_id: "local-one", model: "llama3.1" };

  it("the server refuses such an entry or price without the endpoint id, and an endpoint id for another provider", async () => {
    const needed = "openai-compatible: this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)";
    const entry = await replay(["oc_add_entry_no_endpoint"])
      .api.models.addEntry({ provider: "openai-compatible", model: "llama3.1", family: "meta", processing: "canada", reason: "our local model" })
      .catch((e: unknown) => e);
    expect((entry as InvalidRequestError).fields).toEqual([{ path: "$.endpoint_id", message: needed }]);
    const price = await replay(["oc_price_no_endpoint"])
      .api.models.setPrice({ provider: "openai-compatible", model: "llama3.1", inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" })
      .catch((e: unknown) => e);
    expect((price as InvalidRequestError).fields).toEqual([{ path: "$.endpoint_id", message: needed }]);
    const other = await replay(["oc_add_entry_other_provider"])
      .api.models.addEntry({ ...named, provider: "azure-openai", family: "meta", processing: "canada", reason: "our local model" })
      .catch((e: unknown) => e);
    expect((other as InvalidRequestError).fields).toEqual([{ path: "$.endpoint_id", message: "endpoint_id_not_allowed" }]);
  });

  it("add, price, approve, edit, pause, retire and remove send the connection as endpoint_id", async () => {
    const { api, calls } = replay(["oc_add_entry", "oc_price_set", "oc_approve", "oc_edit", "oc_pause", "oc_retire", "oc_remove_in_use"]);
    const added = await api.models.addEntry({ ...named, family: "meta", processing: "canada", reason: "our local model" });
    expect(added.already).toBe(false);
    await api.models.setPrice({ ...named, inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" });
    const approved = await api.models.approve({ ...named, reason: "looks good" });
    expect(approved.already).toBe(false);
    await api.models.editEntry({ ...named, family: "llama", reason: "its family" });
    const paused = await api.models.pause({ ...named, reason: "under review" });
    expect(paused.affected).toEqual(["agent=theo"]);
    await api.models.retire({ ...named, reason: "replaced" });
    const removed = await api.models.remove({ ...named, reason: "clean up" }).catch((e: unknown) => e);
    expect((removed as ProblemError).title).toBe("in_use");
    expect(calls.map((c) => [c.path, c.body])).toEqual([
      ["/v1/models/catalog/entries", { ...wire, family: "meta", processing: "canada", reason: "our local model" }],
      ["/v1/models/catalog/prices", { ...wire, input_per_million: "0.20", output_per_million: "0.60", effective_from: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" }],
      ["/v1/models/catalog/entries/approve", { ...wire, reason: "looks good" }],
      ["/v1/models/catalog/entries/edit", { ...wire, family: "llama", reason: "its family" }],
      ["/v1/models/catalog/entries/pause", { ...wire, reason: "under review" }],
      ["/v1/models/catalog/entries/retire", { ...wire, reason: "replaced" }],
      ["/v1/models/catalog/entries/remove", { ...wire, reason: "clean up" }],
    ]);
  });

  it("an entry of another provider still sends no endpoint_id", async () => {
    const { api, calls } = replay(["approve"]);
    await api.models.approve({ provider: "azure-openai", model: "gpt-5.4-nano", reason: "looks good" });
    expect(calls[0]?.body).toEqual({ provider: "azure-openai", model: "gpt-5.4-nano", reason: "looks good" });
  });

  it("maps the entry with its connection: the ref is the one assignments use, and no price stays null", async () => {
    const unpriced = (await replay(["oc_catalog_unpriced"]).api.models.catalog()).entries.find((e) => e.provider === "openai-compatible");
    expect(unpriced).toMatchObject({ ref: "openai-compatible/local-one/llama3.1", endpointId: "local-one", model: "llama3.1", status: "paused", connected: true });
    expect(unpriced?.price).toBeNull(); // unknown, never guessed
    const { api } = replay(["oc_catalog", "oc_assignments"]);
    const catalog = await api.models.catalog();
    const entry = catalog.entries.find((e) => e.provider === "openai-compatible");
    expect(entry).toMatchObject({ ref: "openai-compatible/local-one/llama3.1", endpointId: "local-one", status: "approved" });
    expect(entry?.price).toMatchObject({ inputPerMillion: "0", outputPerMillion: "0", source: "declared free (local)" }); // the server's text, the later version
    expect(catalog.entries.find((e) => e.ref === "azure-openai/gpt-5.4-mini")).not.toHaveProperty("endpointId");
    const theo = (await api.models.assignments()).find((r) => r.subject === "agent=theo");
    expect(theo?.ref).toBe(entry?.ref);
  });

  it("a zero price is refused unless it is declared free; the history names the connection", async () => {
    const zero = { ...named, inputPerMillion: "0", outputPerMillion: "0", effectiveFrom: "2026-04-01T00:00:00Z", reason: "cost of our own server" };
    const refused = await replay(["oc_price_zero_not_declared"]).api.models.setPrice({ ...zero, source: "our hosting cost sheet" }).catch((e: unknown) => e);
    expect((refused as InvalidRequestError).message).toContain("a zero price must be declared free for a local connection");
    const declared = await replay(["oc_price_declared_free"]).api.models.setPrice({ ...zero, source: "declared free (local)" });
    expect(declared.ledgerSeq).toBeGreaterThan(0);
    const { events } = await replay(["oc_events"]).api.models.events();
    const mine = events.filter((e) => e.ref === "openai-compatible/local-one/llama3.1").map((e) => e.summary);
    expect(mine).toContain("Added openai-compatible/local-one/llama3.1 (paused until approved)");
    expect(mine).toContain("Approved openai-compatible/local-one/llama3.1");
    expect(mine).toContain("Price set for openai-compatible/local-one/llama3.1");
  });
});

describe("writes (recorded real answers)", () => {
  it("sends only the fields the server accepts, and reads the ledger position", async () => {
    const { api, calls } = replay(["add_entry"]);
    const done = await api.models.addEntry({ provider: "azure-openai", model: "gpt-5.4-nano", family: "openai", processing: "canada", reason: "cheap tier" });
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/models/catalog/entries" });
    expect(calls[0]?.body).toEqual({ provider: "azure-openai", model: "gpt-5.4-nano", family: "openai", processing: "canada", reason: "cheap tier" });
    expect(done.already).toBe(false);
    expect(done.ledgerSeq).toBeGreaterThan(0);
  });

  it("says 'already' for a write that changed nothing", async () => {
    const { api } = replay(["approve_again"]);
    const done = await api.models.approve({ provider: "azure-openai", model: "gpt-5.4-nano", reason: "looks good" });
    expect(done.already).toBe(true);
  });

  it("reports who is affected by a pause", async () => {
    const { api } = replay(["pause_assigned"]);
    const done = await api.models.pause({ provider: "azure-openai", model: "gpt-5.4-mini", reason: "under review" });
    expect(done.affected).toEqual(["agent=dana"]);
  });

  it("reports the entry a widening edit paused, and the entries a regions write paused", async () => {
    const edit = await replay(["edit_widen"]).api.models.editEntry({ provider: "azure-openai", model: "gpt-5.4-ca", processing: "canada", reason: "canada only" });
    expect(edit).toMatchObject({ entryRepaused: true, pausedBecause: "processing_widened", repaused: [] });
    const narrow = await replay(["edit_narrow"]).api.models.editEntry({ provider: "azure-openai", model: "gpt-5.4-ca", processing: "global", reason: "global only" });
    expect(narrow.entryRepaused).toBe(false);
    const regions = await replay(["regions_widen"]).api.models.setRegions({ customerRegions: ["eu-west"], reason: "EU customers" });
    expect(regions).toMatchObject({ repaused: ["azure-ai/Mistral-Large"], pausedBecause: "regions_widened" });
  });

  it("keeps the server's problems, with their paths, on a refusal", async () => {
    const { api } = replay(["add_entry_bad_processing"]);
    const error = await api.models
      .addEntry({ provider: "azure-openai", model: "x-2", family: "openai", processing: "mars", reason: "try it" })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(InvalidRequestError);
    expect((error as InvalidRequestError).fields).toEqual([{ path: "$.processing", message: "processing scope 'mars' is not canada, global or region:<id>" }]);
  });

  it("names the 409 refusals by their code", async () => {
    for (const [name, code] of [["add_entry_again", "already_exists"], ["remove_approved", "not_paused"], ["remove_in_use", "in_use"], ["price_duplicate", "price_version_exists"]] as const) {
      const { api } = replay([name]);
      const error = await api.models.remove({ provider: "x", model: "y", reason: "clean up" }).catch((e: unknown) => e);
      expect(error, name).toBeInstanceOf(ProblemError);
      expect((error as ProblemError).title).toBe(code);
      expect((error as ProblemError).status).toBe(409);
    }
  });

  it("sends a price with the server's field names and no float", async () => {
    const { api, calls } = replay(["price_set"]);
    await api.models.setPrice({ provider: "azure-openai", model: "gpt-5.4-nano", inputPerMillion: "0.10", outputPerMillion: "0.40", effectiveFrom: "2026-03-01T00:00:00Z", source: "vendor price list", reason: "list price" });
    expect(calls[0]?.body).toEqual({ provider: "azure-openai", model: "gpt-5.4-nano", input_per_million: "0.10", output_per_million: "0.40", effective_from: "2026-03-01T00:00:00Z", source: "vendor price list", reason: "list price" });
  });

  it("assigns and clears (a null model)", async () => {
    const { api, calls } = replay(["assign", "assign"]);
    await api.models.assign({ scope: "agent", id: "dana", model: "azure-openai/gpt-5.4-mini", reason: "try it" });
    await api.models.assign({ scope: "agent", id: "dana", model: null, reason: "back" });
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(["PUT /v1/models/assignments/agent/dana", "PUT /v1/models/assignments/agent/dana"]);
    expect(calls.map((c) => c.body)).toEqual([
      { model: "azure-openai/gpt-5.4-mini", reason: "try it" },
      { model: null, reason: "back" },
    ]);
  });
});

describe("connections (recorded real answers)", () => {
  it("lists them and reads the test result states", async () => {
    const { api } = replay(["connections_after", "test_ok", "test_auth_failed", "test_unreachable"]);
    const list = await api.models.connections();
    expect(list[0]).toMatchObject({ provider: "azure-openai", status: "active", endpoint: "https://example-openai.openai.azure.com" });
    expect(await api.models.testConnection("azure-openai")).toMatchObject({ ok: true, code: "ok", text: "the endpoint answered and accepted the Entra login" });
    expect(await api.models.testConnection("azure-openai")).toMatchObject({ ok: false, code: "auth_failed" });
    expect(await api.models.testConnection("azure-openai")).toMatchObject({ ok: false, code: "unreachable" });
  });

  it("a rate-limited test keeps the seconds to wait", async () => {
    const { api } = replay(["test_rate_limited"]);
    const error = await api.models.testConnection("azure-openai").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect(error).toMatchObject({ status: 429, title: "rate_limited", retryAfter: 10 });
  });

  it("a test of a missing connection is not found", async () => {
    const error = await replay(["test_missing"]).api.models.testConnection("azure-ai").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NotFoundError);
  });

  it("a repoint lists the entries it paused again; it needs a reason", async () => {
    const { api, calls } = replay(["repoint_no_reason", "repoint"]);
    const refused = await api.models.putConnection("azure-ai", { endpoint: "https://ais-other.cognitiveservices.azure.com" }).catch((e: unknown) => e);
    expect((refused as InvalidRequestError).fields[0]).toMatchObject({ path: "$.reason" });
    const done = await api.models.putConnection("azure-ai", { endpoint: "https://ais-other.cognitiveservices.azure.com", reason: "moved to the other resource" });
    expect(done.repaused).toEqual(["azure-ai/Mistral-Large"]);
    expect(calls[1]).toMatchObject({ method: "PUT", path: "/v1/models/connections/azure-ai" });
    expect(calls[1]?.body).toEqual({ endpoint: "https://ais-other.cognitiveservices.azure.com", reason: "moved to the other resource" });
  });

  it("sends no key and no secret field, whatever the screen holds", async () => {
    const { api, calls } = replay(["connect"]);
    await api.models.putConnection("azure-openai", { endpoint: "https://example-openai.openai.azure.com", status: "active" });
    expect(Object.keys(calls[0]?.body as object).sort()).toEqual(["endpoint", "status"]);
  });

  it("disconnects with a POST to .../remove", async () => {
    const { api, calls } = replay(["disconnect"]);
    await api.models.removeConnection("azure-ai");
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/models/connections/azure-ai/remove" });
  });
});

describe("resolution (recorded real answers)", () => {
  it("maps a passing report and sends the data class", async () => {
    const { api, calls } = replay(["resolution_synthetic"]);
    const report = await api.models.resolution("synthetic");
    expect(calls[0]?.path).toBe("/v1/models/resolution?data_class=synthetic");
    expect(report.ok).toBe(true);
    expect(report.resolutions.map((r) => r.subject)).toEqual(["agent=dana", "agent=theo", "agent=rhea", "agent=omar"]);
  });

  it("a blocked report has the texts and no resolutions", async () => {
    const report = await replay(["resolution_customer"]).api.models.resolution("customer");
    expect(report.ok).toBe(false);
    expect(report.resolutions).toEqual([]);
    expect(report.blocks[0]).toMatchObject({ subject: "agent=dana", reason: "residency_refused" });
    expect(report.blocks[0]?.text).toContain("may not receive customer data");
  });
});

describe("who is signed in", () => {
  it("an owner is an admin; a viewer is not", async () => {
    const owner = await replay(["session_owner"]).api.session.current();
    expect(owner).toMatchObject({ role: "owner", name: "Ada Example" });
    expect(isAdmin(owner)).toBe(true);
    const viewer = await replay(["session_viewer"]).api.session.current();
    expect(viewer?.role).toBe("viewer");
    expect(isAdmin(viewer)).toBe(false);
  });

  it("nobody signed in is null, not an error", async () => {
    expect(await replay(["session_none"]).api.session.current()).toBeNull();
    expect(isAdmin(null)).toBe(false);
  });

  it("a viewer's admin reads are 403 problems the screens can name", async () => {
    const error = await replay(["viewer_connections"]).api.models.connections().catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 403, title: "forbidden" });
  });
});

describe("the CSRF token", () => {
  it("is sent on writes and never on reads, from the cookie the server sets", async () => {
    document.cookie = "nm_csrf=tok-123";
    try {
      const { api, calls } = replay(["catalog_seeded", "approve"]);
      await api.models.catalog();
      await api.models.approve({ provider: "azure-openai", model: "gpt-5.4-nano", reason: "looks good" });
      expect(calls[0]?.headers["x-csrf-token"]).toBeUndefined();
      expect(calls[1]?.headers["x-csrf-token"]).toBe("tok-123");
    } finally {
      document.cookie = "nm_csrf=; max-age=0";
    }
  });

  it("a missing token is the server's 403 csrf_invalid, shown as it is", async () => {
    const error = await replay(["owner_no_csrf"]).api.models.pause({ provider: "a", model: "b", reason: "no token" }).catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 403, title: "csrf_invalid" });
  });
});

describe("connection history request and mapper", () => {
  function raw(rows: unknown[], nextBefore: number | null = null) {
    const urls: string[] = [];
    const fetchFn = vi.fn((input: string) => {
      urls.push(input);
      return Promise.resolve(new Response(JSON.stringify({ rows, next_before: nextBefore }), { status: 200, headers: { "content-type": "application/json" } }));
    });
    const api = createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } });
    return { api, urls };
  }

  it("builds the query from provider, endpoint id, before and limit, and maps next_before", async () => {
    const { api, urls } = raw([], 12);
    const page = await api.models.connectionHistory({ provider: "openai-compatible", endpointId: "local one", before: 40, limit: 25 });
    expect(urls[0]).toBe("/v1/models/connections/events?provider=openai-compatible&endpoint_id=local+one&before=40&limit=25");
    expect(page).toEqual({ rows: [], nextBefore: 12 });
    const bare = raw([]);
    expect((await bare.api.models.connectionHistory({})).nextBefore).toBeNull();
    expect(bare.urls[0]).toBe("/v1/models/connections/events");
  });

  it("a hostile invalid row keeps only seq, type, actor and time through the real mapper", async () => {
    const hostile = { seq: 4, type: "model.connection_updated", actor: "human:u_1", at: "2026-10-25T12:00:00+00:00", provider: "azure-openai", valid: false, endpoint: "https://evil.example", endpoint_id: "x", key: "obviously-fake-test-key-not-a-secret", reason: "leaked reason", status: "active", auth: "key" };
    const named = { ...hostile, seq: 5, provider: "(invalid)", valid: true };
    const { api } = raw([hostile, named]);
    const { rows } = await api.models.connectionHistory({ provider: "azure-openai" });
    for (const row of rows) {
      expect(row).toEqual({ seq: row.seq, type: "model.connection_updated", actor: "human:u_1", at: "2026-10-25T12:00:00+00:00", provider: "(invalid)", valid: false });
    }
  });

  it("a valid row keeps the key name and drops wrongly typed fields", async () => {
    const { api } = raw([{ seq: 6, type: "model.connection_created", actor: "a", at: "t", provider: "openai-compatible", valid: true, endpoint_id: "local", key: "my-key-name", repoint: "yes", ok: 1, endpoint: 5 }]);
    const { rows } = await api.models.connectionHistory({});
    expect(rows[0]).toEqual({ seq: 6, type: "model.connection_created", actor: "a", at: "t", provider: "openai-compatible", valid: true, endpointId: "local", keyName: "my-key-name" });
  });

  it("a stored-key row keeps the key id, version, previous version, grace, cause and code, never a fingerprint", async () => {
    const head = { actor: "human:u_1", at: "t", provider: "anthropic", valid: true, key_id: "mk_0000000000aaaaaaaaaa" };
    const { api } = raw([
      { ...head, seq: 7, type: "model.key_activated", version: 2, previous_version: 1, grace_seconds: 60, reason: "planned", fingerprint: "fp-should-not-pass" },
      { ...head, seq: 8, type: "model.key_revoked", version: "all", cause: "connection_removed", reason: null },
      { ...head, seq: 9, type: "model.key_test_failed", version: 3, code: "auth_failed", grace_seconds: "x", version_extra: 1 },
    ]);
    const { rows } = await api.models.connectionHistory({ provider: "anthropic" });
    const base = { actor: "human:u_1", at: "t", provider: "anthropic", valid: true, keyId: "mk_0000000000aaaaaaaaaa" };
    expect(rows).toEqual([
      { ...base, seq: 7, type: "model.key_activated", version: 2, previousVersion: 1, graceSeconds: 60, reason: "planned" },
      { ...base, seq: 8, type: "model.key_revoked", version: "all", cause: "connection_removed", reason: null },
      { ...base, seq: 9, type: "model.key_test_failed", version: 3, code: "auth_failed" },
    ]);
    expect(JSON.stringify(rows)).not.toContain("fp-should-not-pass");
  });
});
