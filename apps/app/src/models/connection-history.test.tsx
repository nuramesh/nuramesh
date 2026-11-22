import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { CatalogEvent, Connection, ConnectionHistoryPage, ConnectionHistoryRow } from "@/api/models";
import { renderAt, serverLike } from "@/test/server-like";

const LOAD_WAIT_MS = 20_000;
const AT = "2026-10-25T12:00:00+00:00";

const AZURE: Connection = {
  provider: "azure-openai",
  auth: "entra",
  endpoint: "https://example-openai.openai.azure.com",
  status: "active",
  setBy: "human:u_1",
  setAt: AT,
  seq: 7,
};
const LOCAL: Connection = {
  provider: "openai-compatible",
  endpointId: "local",
  auth: "none",
  endpoint: "https://llm.acme.example/v1",
  status: "active",
  setBy: "human:u_1",
  setAt: AT,
  seq: 9,
};

function created(seq: number, over: Partial<ConnectionHistoryRow> = {}): ConnectionHistoryRow {
  return { seq, type: "model.connection_created", actor: "human:u_1", at: AT, provider: "azure-openai", valid: true, endpoint: AZURE.endpoint, status: "active", auth: "entra", repoint: false, reason: null, ...over };
}

function client(history: (q: { provider?: string; endpointId?: string; before?: number }) => ConnectionHistoryPage, connections: Connection[] = [AZURE]) {
  const fixtures = createFixtureClient();
  const connectionHistory = vi.fn((q: { provider?: string; endpointId?: string; before?: number }) => Promise.resolve(history(q)));
  const api = serverLike({
    models: { ...fixtures.models, connections: () => Promise.resolve(connections), connectionHistory },
    session: fixtures.session,
  });
  return { api, connectionHistory };
}

async function openDrawer(api: ApiClient, name: string) {
  const user = userEvent.setup();
  renderAt("/models/connections", api);
  const list = await screen.findByRole("list", { name: "Connections" }, { timeout: LOAD_WAIT_MS });
  await user.click(within(list).getByRole("button", { name: name }));
  const drawer = await screen.findByRole("dialog", { name: /History/ });
  return { user, drawer };
}

describe("connection History drawer", () => {
  it("empty: says there is nothing yet", async () => {
    const { api } = client(() => ({ rows: [], nextBefore: null }));
    const { drawer } = await openDrawer(api, "History azure-openai");
    expect(await within(drawer).findByText(/No changes recorded/)).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
  }, 40_000);

  it("one page: newest at the top, repoint as a labelled chip, reason shown, no Load older", async () => {
    const { api, connectionHistory } = client(() => ({
      rows: [created(3, { reason: "first connection" }), created(5, { type: "model.connection_updated", repoint: true, endpoint: "https://aoai-other.openai.azure.com", reason: "moved to the new resource" })],
      nextBefore: null,
    }));
    const { drawer } = await openDrawer(api, "History azure-openai");
    const list = await within(drawer).findByRole("list", { name: "Connection history" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("ledger #5");
    expect(items[1]).toHaveTextContent("ledger #3");
    expect(items[0]).toHaveTextContent("Repointed");
    expect(items[1]).not.toHaveTextContent("Repointed");
    expect(items[0]).toHaveTextContent("moved to the new resource");
    expect(items[0]).toHaveTextContent("https://aoai-other.openai.azure.com");
    expect(within(drawer).queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
    expect(connectionHistory).toHaveBeenCalledWith({ provider: "azure-openai" });
  }, 40_000);

  it("two pages: Load older appends the older page below and keeps the newest on top", async () => {
    const { api, connectionHistory } = client((q) =>
      q.before === undefined
        ? { rows: [created(10), created(11, { type: "model.connection_removed" })], nextBefore: 10 }
        : { rows: [created(1), created(2, { reason: "oldest change" })], nextBefore: null },
    );
    const { user, drawer } = await openDrawer(api, "History azure-openai");
    await within(drawer).findByRole("list", { name: "Connection history" });
    await user.click(within(drawer).getByRole("button", { name: "Load older" }));
    await waitFor(() => { expect(within(drawer).getAllByRole("listitem")).toHaveLength(4); });
    const seqs = within(drawer).getAllByRole("listitem").map((li) => /ledger #(\d+)/.exec(li.textContent)?.[1]);
    expect(seqs).toEqual(["11", "10", "2", "1"]);
    expect(connectionHistory).toHaveBeenLastCalledWith({ provider: "azure-openai", before: 10 });
    expect(within(drawer).queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
  }, 40_000);

  it("a malformed row is a placeholder: no raw value, whatever extra the server sent", async () => {
    const hostile = { seq: 4, type: "model.connection_updated", actor: "human:u_1", at: AT, provider: "(invalid)", valid: false, endpoint: "https://evil.example", key: "obviously-fake-test-key-not-a-secret", reason: "leaked reason", reason_hash: "ffff", old_key: "obviously-fake-old-key" } as unknown as ConnectionHistoryRow;
    const { api } = client(() => ({ rows: [hostile, created(5)], nextBefore: null }));
    const { drawer } = await openDrawer(api, "History azure-openai");
    const list = await within(drawer).findByRole("list", { name: "Connection history" });
    const placeholder = within(list).getAllByRole("listitem")[1];
    expect(placeholder).toHaveTextContent("ledger #4");
    expect(placeholder).toHaveTextContent(/not shown/i);
    for (const secret of ["evil.example", "obviously-fake", "leaked reason", "ffff"]) {
      expect(drawer).not.toHaveTextContent(secret);
    }
  }, 40_000);

  it("an unknown or prototype-named type gets the safe word, never a prototype member", async () => {
    const rows = ["__proto__", "constructor", "toString", "model.something_new"].map((type, i) => created(10 + i, { type }));
    const { api } = client(() => ({ rows, nextBefore: null }));
    const { drawer } = await openDrawer(api, "History azure-openai");
    const list = await within(drawer).findByRole("list", { name: "Connection history" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    for (const item of items) expect(item).toHaveTextContent(/^Changed/);
  }, 40_000);

  it("the fixture client honours endpointId and limit", async () => {
    const { models } = createFixtureClient();
    await models.putConnection("azure-openai", { endpoint: "https://aoai-test.openai.azure.com", reason: "first connect" });
    await models.testConnection("azure-openai");
    const all = await models.connectionHistory({ provider: "azure-openai" });
    expect(all.rows.length).toBeGreaterThanOrEqual(2);
    const one = await models.connectionHistory({ provider: "azure-openai", limit: 1 });
    expect(one.rows).toHaveLength(1);
    expect(one.nextBefore).toBe(one.rows[0]?.seq);
    expect((await models.connectionHistory({ provider: "azure-openai", endpointId: "nope" })).rows).toEqual([]);
  });

  it("a test failure row shows the code and class, not an error", async () => {
    const { api } = client(() => ({
      rows: [{ seq: 8, type: "model.connection_tested", actor: "human:u_1", at: AT, provider: "azure-openai", valid: true, ok: false, code: "auth_failed", statusClass: "4xx" }, { seq: 9, type: "model.connection_tested", actor: "human:u_1", at: AT, provider: "azure-openai", valid: true, ok: true, code: "ok", statusClass: "2xx" }],
      nextBefore: null,
    }));
    const { drawer } = await openDrawer(api, "History azure-openai");
    const list = await within(drawer).findByRole("list", { name: "Connection history" });
    const [ok, failed] = within(list).getAllByRole("listitem");
    expect(failed).toHaveTextContent(/Test failed/);
    expect(failed).toHaveTextContent("auth_failed");
    expect(failed).toHaveTextContent("4xx");
    expect(ok).toHaveTextContent(/Test passed/);
  }, 40_000);

  it("openai-compatible: the drawer filters by endpoint id, and hosted placeholders show as the API returns them", async () => {
    const placeholders: ConnectionHistoryRow[] = [3, 4, 5].map((seq) => ({ seq, type: "model.connection_updated", actor: "human:u_1", at: AT, provider: "(invalid)", valid: false }));
    const { api, connectionHistory } = client(() => ({ rows: placeholders, nextBefore: null }), [LOCAL]);
    const { drawer } = await openDrawer(api, "History openai-compatible local");
    const items = await within(drawer).findAllByRole("listitem");
    expect(items).toHaveLength(3);
    for (const item of items) expect(item).toHaveTextContent(/not shown/i);
    expect(drawer).not.toHaveTextContent("llm.acme.example");
    expect(connectionHistory).toHaveBeenCalledWith({ provider: "openai-compatible", endpointId: "local" });
  }, 40_000);

  it("shows a key NAME for an openai-compatible row, never a key", async () => {
    const row: ConnectionHistoryRow = { seq: 6, type: "model.connection_updated", actor: "human:u_1", at: AT, provider: "openai-compatible", valid: true, endpointId: "local", endpoint: "https://llm.acme.example/v1", status: "active", auth: "key", keyName: "kn", repoint: true, reason: null };
    const { api } = client(() => ({ rows: [row], nextBefore: null }), [LOCAL]);
    const { drawer } = await openDrawer(api, "History openai-compatible local");
    expect(await within(drawer).findByText(/Key name: kn/)).toBeInTheDocument();
  }, 40_000);

  it("stored-key rows: words, key id, version, previous version, grace, cause and code; a reason only where one can be kept", async () => {
    const ANTHROPIC: Connection = { provider: "anthropic", auth: "stored", endpoint: "https://api.anthropic.com", status: "active", setBy: "human:u_1", setAt: AT, seq: 3 };
    const head = { actor: "human:u_1", at: AT, provider: "anthropic", valid: true, keyId: "mk_0000000000aaaaaaaaaa" };
    const rows: ConnectionHistoryRow[] = [
      { ...head, seq: 20, type: "model.key_stored", version: 1 },
      { ...head, seq: 21, type: "model.key_activated", version: 1, reason: null },
      { ...head, seq: 22, type: "model.key_activated", version: 2, previousVersion: 1, graceSeconds: 3600, reason: "planned rotation" },
      { ...head, seq: 23, type: "model.key_test_failed", version: 3, code: "auth_failed" },
      { ...head, seq: 24, type: "model.key_revoked", version: "all", cause: "connection_removed", reason: null },
      { ...head, seq: 25, type: "model.key_destroyed", actor: "system:key-sweeper", version: 1, cause: "revoked" },
      { ...head, seq: 26, type: "model.key_read_failed", actor: "system:runner", version: 2, code: "key_missing" },
    ];
    const { api } = client(() => ({ rows, nextBefore: null }), [ANTHROPIC]);
    const { drawer } = await openDrawer(api, "History anthropic");
    const list = await within(drawer).findByRole("list", { name: "Connection history" });
    const [read, destroyed, revoked, failed, rotated, activated, stored] = within(list).getAllByRole("listitem");
    expect(stored).toHaveTextContent(/^Key stored/);
    expect(stored).toHaveTextContent("mk_0000000000aaaaaaaaaa · version 1");
    expect(stored).not.toHaveTextContent(/reason/i);
    expect(activated).toHaveTextContent(/^Key activated/);
    expect(activated).toHaveTextContent(/no reason kept/);
    expect(rotated).toHaveTextContent("replaces version 1, which stays usable for 3600 s");
    expect(rotated).toHaveTextContent("Reason: planned rotation");
    expect(failed).toHaveTextContent(/^Key test failed/);
    expect(failed).toHaveTextContent("Result: auth_failed");
    expect(revoked).toHaveTextContent(/^Key revoked/);
    expect(revoked).toHaveTextContent("all versions");
    expect(revoked).toHaveTextContent("Cause: connection_removed");
    expect(destroyed).toHaveTextContent(/^Key destroyed/);
    expect(destroyed).toHaveTextContent("system:key-sweeper");
    expect(destroyed).toHaveTextContent("Cause: revoked");
    expect(destroyed).not.toHaveTextContent(/reason/i);
    expect(read).toHaveTextContent(/^Key read failed/);
    expect(read).toHaveTextContent("Result: key_missing");
  }, 40_000);

  it("a refused read (a hosted or non-admin caller) says so with a retry", async () => {
    const fixtures = createFixtureClient();
    const api = serverLike({
      models: { ...fixtures.models, connections: () => Promise.resolve([AZURE]), connectionHistory: () => Promise.reject(new Error("boom")) },
      session: fixtures.session,
    });
    const { drawer } = await openDrawer(api, "History azure-openai");
    expect(await within(drawer).findByRole("alert")).toHaveTextContent(/Couldn't load the connection history/);
  }, 40_000);
});

describe("catalog history: Load older", () => {
  function manyEvents(from: number, to: number): CatalogEvent[] {
    return Array.from({ length: to - from + 1 }, (_, i) => ({ seq: from + i, type: "model.catalog_paused", actor: "human:u_1", at: AT, ref: "fake/dev-1", summary: `Paused fake/dev-1 #${String(from + i)}`, reason: null }));
  }

  it("more than 500 rows: the first page holds 500, Load older fetches before the first seq and appends below", async () => {
    const fixtures = createFixtureClient();
    const events = vi.fn((before?: number) =>
      Promise.resolve(before === undefined ? { events: manyEvents(101, 600), nextBefore: 101 } : { events: manyEvents(1, 100), nextBefore: null }),
    );
    const api = serverLike({ models: { ...fixtures.models, events }, session: fixtures.session });
    const user = userEvent.setup();
    renderAt("/models/catalog", api);
    await screen.findByRole("heading", { level: 1, name: "Models" }, { timeout: LOAD_WAIT_MS });
    await user.click(await screen.findByRole("button", { name: "History" }));
    const drawer = await screen.findByRole("dialog", { name: "Catalog history" });
    const list = await within(drawer).findByRole("list", { name: "History" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(500);
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("#600");
    await user.click(within(drawer).getByRole("button", { name: "Load older" }));
    await waitFor(() => { expect(within(drawer).getAllByRole("listitem")).toHaveLength(600); });
    const items = within(drawer).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("#600");
    expect(items[599]).toHaveTextContent("#1");
    expect(events).toHaveBeenLastCalledWith(101);
    expect(within(drawer).queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
  }, 60_000);

  it("a single page has no Load older button", async () => {
    const user = userEvent.setup();
    renderAt("/models/catalog", createFixtureClient());
    await screen.findByRole("heading", { level: 1, name: "Models" }, { timeout: LOAD_WAIT_MS });
    await user.click(await screen.findByRole("button", { name: "History" }));
    const drawer = await screen.findByRole("dialog", { name: "Catalog history" });
    await within(drawer).findByText("Catalog seeded from the team configuration");
    expect(within(drawer).queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
  }, 40_000);
});
