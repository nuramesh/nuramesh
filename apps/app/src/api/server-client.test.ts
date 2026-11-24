import { describe, expect, it, vi } from "vitest";

import { rawDone, rawLedger, rawNeedsHuman, rawRunList, rawVerifyAnchored } from "@/test/contract";

import { NotAllowedError, NotAvailableError, NotFoundError, ProblemError } from "./client";
import { describeProblem } from "./problems";
import { createServerClient } from "./server-client";

type Handler = (url: URL, init: RequestInit | undefined) => { status?: number; body: unknown } | Promise<{ status?: number; body: unknown }>;

/** A fake fetch: routes by "METHOD /path", records every call, answers JSON. */
function fakeServer(routes: Record<string, Handler>) {
  const calls: { method: string; url: URL; body: unknown; headers: Record<string, string> }[] = [];
  const fetchFn = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, "http://127.0.0.1:5173");
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body === undefined ? undefined : JSON.parse(init.body as string), headers: (init?.headers ?? {}) as Record<string, string> });
    const handler = routes[`${method} ${url.pathname}`];
    if (handler === undefined) return new Response(JSON.stringify({ title: "not_found", detail: "no route", status: 404 }), { status: 404, headers: { "content-type": "application/problem+json" } });
    const { status = 200, body } = await handler(url, init);
    return new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } });
  });
  return { fetchFn, calls };
}

const problem = (status: number, title: string, detail: string): ReturnType<Handler> => ({ status, body: { type: `urn:nuramesh:${title}`, title, status, detail } });
const NOW = () => new Date("2026-10-24T12:00:00Z");
const RUN = "run-20261023-193432-f5e5";

function client(routes: Record<string, Handler>, baseUrl = "") {
  const server = fakeServer(routes);
  return { ...server, api: createServerClient({ baseUrl, fetch: server.fetchFn, now: NOW, socket: () => { throw new Error("no sockets in this test"); } }) };
}

describe("runs", () => {
  it("lists runs from /v1/runs and carries the truncated flag", async () => {
    const { api, calls } = client({ "GET /v1/runs": () => ({ body: { ...(rawRunList as object), truncated: true } }) });
    const page = await api.runs.listPage();
    expect(page.truncated).toBe(true);
    expect(page.runs.map((r) => r.status)).toEqual(["done", "waiting"]);
    expect(calls[0]?.url.search).toBe("?limit=200");
    expect((await api.runs.list()).length).toBe(2);
  });

  it("sends the status filter the server understands, and filters the rest here", async () => {
    const { api, calls } = client({ "GET /v1/runs": () => ({ body: rawRunList }) });
    await api.runs.listPage({ status: "waiting" });
    expect(calls[0]?.url.searchParams.get("status")).toBe("needs_human");
    await api.runs.listPage({ status: "done" });
    expect(calls[1]?.url.searchParams.get("status")).toBe("done");
    const failed = await api.runs.listPage({ status: "failed" }); // covers failed and aborted, so it filters here
    expect(calls[2]?.url.searchParams.has("status")).toBe(false);
    expect(failed.runs).toEqual([]);
  });

  it("reads one run, and says 'not found' for an unknown one", async () => {
    const { api } = client({ [`GET /v1/runs/${RUN}`]: () => ({ body: rawDone }) });
    expect((await api.runs.get(RUN)).status).toBe("done");
    await expect(api.runs.get("nope")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("pages the run's ledger until it has everything, and returns the cursor", async () => {
    const page = (after: number) => ({ stream: "x", events: (rawLedger as { events: unknown[] }).events.slice(after, after + 10), head_seq: 32, head: "h", next_after_seq: Math.min(32, after + 10), has_more: after + 10 < 32 });
    const { api, calls } = client({ [`GET /v1/runs/${RUN}/ledger`]: (url) => ({ body: page(Number(url.searchParams.get("after_seq"))) }) });
    const { events, nextSeq } = await api.runs.events(RUN, 0);
    expect(events).toHaveLength(32);
    expect(nextSeq).toBe(32);
    expect(calls).toHaveLength(4);
    expect(calls.map((c) => c.url.searchParams.get("after_seq"))).toEqual(["0", "10", "20", "30"]);
    expect((await api.runs.events(RUN, 32)).events.length).toBe(0);
  });

  it("not wired: starting and pausing say so", async () => {
    const { api } = client({});
    await expect(api.runs.start({ template: "t", budget: { amount: "1", currency: "USD" } })).rejects.toBeInstanceOf(NotAvailableError);
    await expect(api.runs.pause("r")).rejects.toBeInstanceOf(NotAvailableError);
  });
});

describe("Stop", () => {
  const stopped = { run_id: RUN, status: "stopping", already_requested: false, requested_by: "human:you", requested_at: "2026-10-24T11:00:00Z", ledger_seq: 14, via: "api" };
  const stoppingDetail = { ...(rawNeedsHuman as object), status: "stopping", ended_at: null, stop: { requested_by: "human:you" } };

  it("posts the reason as JSON and returns the run as 'stopping' with the ledger entry", async () => {
    const { api, calls } = client({
      [`POST /v1/runs/${RUN}/stop`]: () => ({ body: stopped }),
      [`GET /v1/runs/${RUN}`]: () => ({ body: stoppingDetail }),
    });
    const result = await api.runs.stop(RUN, "  wrong branch  ");
    expect(calls[0]).toMatchObject({ method: "POST", body: { reason: "wrong branch" }, headers: { "content-type": "application/json" } });
    expect(calls[0]?.body).not.toHaveProperty("actor"); // the actor is the server's, never sent from here
    expect(result.ledgerEntry).toBe("14");
    expect(result.result).toMatchObject({ status: "stopping", stopRequestedBy: "human:you" });
  });

  it("carries the fixed 'reason withheld' words when the organization's key was destroyed", async () => {
    const withheld = { ...stopped, reason_withheld: "org_key_destroyed", stop_reason_withheld: "reason withheld: org key destroyed" };
    const detail = { ...stoppingDetail, stop: { requested_by: "human:you", stop_reason_withheld: "reason withheld: org key destroyed" } };
    const { api } = client({ [`POST /v1/runs/${RUN}/stop`]: () => ({ body: withheld }), [`GET /v1/runs/${RUN}`]: () => ({ body: detail }) });
    const result = await api.runs.stop(RUN, "secret words");
    expect(result.result).toMatchObject({ status: "stopping", stopReasonWithheld: "reason withheld: org key destroyed" });
    const plain = client({ [`POST /v1/runs/${RUN}/stop`]: () => ({ body: stopped }), [`GET /v1/runs/${RUN}`]: () => ({ body: stoppingDetail }) });
    expect((await plain.api.runs.stop(RUN, "x")).result).not.toHaveProperty("stopReasonWithheld");
  });

  it("sends an empty body when there is no reason", async () => {
    const { api, calls } = client({ [`POST /v1/runs/${RUN}/stop`]: () => ({ body: stopped }), [`GET /v1/runs/${RUN}`]: () => ({ body: stoppingDetail }) });
    await api.runs.stop(RUN);
    expect(calls[0]?.body).toEqual({});
    await api.runs.stop(RUN, "   ");
    expect(calls[2]?.body).toEqual({});
  });

  it.each([
    [409, "run_finished", /already ended/],
    [503, "content_store_unavailable", /content store/],
    [403, "authentication_required", /--auth/],
    [400, "invalid_host", /127\.0\.0\.1/],
  ])("%i %s is a ProblemError that is explained plainly", async (status, title, words) => {
    const { api } = client({ [`POST /v1/runs/${RUN}/stop`]: () => problem(status, title, "server text") });
    const error = await api.runs.stop(RUN, "x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect(error).toMatchObject({ status, title });
    expect(describeProblem(error)).toMatch(words);
  });

  it("an unlisted code (wrong_organization is dead since T84) shows the server's own text", async () => {
    const { api } = client({ [`POST /v1/runs/${RUN}/stop`]: () => problem(409, "wrong_organization", "server text") });
    const error = await api.runs.stop(RUN, "x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProblemError);
    expect(describeProblem(error)).toBe("server text");
    expect(describeProblem(error)).not.toContain("different organization");
  });

  it("a stop of a foreign or missing run is a NotFoundError that names the path, not a ProblemError (R1)", async () => {
    const { api } = client({ [`POST /v1/runs/${RUN}/stop`]: () => problem(404, "not_found", "no such run") });
    const error = await api.runs.stop(RUN, "x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NotFoundError);
    expect(error).not.toBeInstanceOf(ProblemError);
    expect(describeProblem(error)).toBe(`/v1/runs/${RUN}/stop was not found`);
  });

  it("a stop that was written but could not be read back is still a stop (never 'couldn't reach')", async () => {
    const failing = createServerClient({
      fetch: vi.fn((_input: string, init?: RequestInit) => {
        if (init?.method === "POST") return Promise.resolve(new Response(JSON.stringify(stopped), { status: 200 }));
        return Promise.reject(new TypeError("Failed to fetch"));
      }),
      now: NOW,
    });
    const result = await failing.runs.stop(RUN, "x");
    expect(result.result).toMatchObject({ id: RUN, status: "stopping", stopRequestedBy: "human:you", ended: false });
    expect(result.ledgerEntry).toBe("14");
  });

  it("an unreachable server is a status 0 problem that names the address", async () => {
    const fetchFn = vi.fn<(input: string) => Promise<Response>>(() => Promise.reject(new TypeError("Failed to fetch")));
    const api = createServerClient({ baseUrl: "http://127.0.0.1:9999/", fetch: fetchFn });
    const error = await api.runs.stop(RUN).catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 0, title: "unreachable" });
    expect(describeProblem(error)).toContain("http://127.0.0.1:9999");
    expect(String(fetchFn.mock.calls[0]?.[0])).toBe("http://127.0.0.1:9999/v1/runs/" + RUN + "/stop");
  });
});

describe("approvals", () => {
  const pending = { id: "ap-1", run_id: "run-1-abcd", task_id: null, agent_id: "dana", action: { kind: "run_command", target: "pytest", amount: null }, asked_because: "rule", status: "pending", requested_at: "2026-10-24T10:00:00Z", decided_by: null, decided_at: null, decision_reason_hash: null, decision_reason: null };
  it("lists, reads one with its run's spend, and decides with approve + reason", async () => {
    let decided = false;
    const { api, calls } = client({
      "GET /v1/approvals": () => ({ body: [pending] }),
      "GET /v1/approvals/ap-1": () => ({
        body: decided
          ? { ...pending, status: "approved", decided_by: "human:you", decided_at: "2026-10-24T10:05:00Z", decision_reason_hash: "b".repeat(64), ledger: [{ ledger_seq: 9, type: "approval.decided", at: "x", actor: "human:you" }] }
          : pending,
      }),
      "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }),
      "POST /v1/approvals/ap-1/approve": () => {
        decided = true;
        return { body: {} };
      },
    });
    const list = await api.approvals.list();
    expect(list[0]).toMatchObject({ state: "pending", runSpend: null });
    const one = await api.approvals.get("ap-1");
    expect(one.runSpend?.spent.amount).toBe("0.001097");
    const result = await api.approvals.decide("ap-1", { decision: "approve", reason: "looked at the diff" });
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ reason: "looked at the diff" });
    const body = calls.find((c) => c.method === "POST")?.body as Record<string, unknown>;
    expect(body).not.toHaveProperty("acknowledge_solo");
    expect(body).not.toHaveProperty("action_digest");
    expect(result).toMatchObject({ ledgerEntry: "9", result: { state: "approved" } });
  });

  it("U10: reportUrl points at GET /v1/runs/{run_id}/report", () => {
    const { api } = client({});
    expect(api.runs.reportUrl?.("run 1")).toMatch(/\/v1\/runs\/run%201\/report$/);
  });

  it("a sole owner's approve carries acknowledge_solo and the digest; a deny never does (SA-5, SA-16)", async () => {
    const digest = "d".repeat(64);
    const soloPending = { ...pending, action_digest: digest, caller_terms: { mode: "solo", fresh_proof_needed: false, proofs: [] } };
    const { api, calls } = client({
      "GET /v1/approvals/ap-1": () => ({ body: soloPending }),
      "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }),
      "POST /v1/approvals/ap-1/approve": () => ({ body: {} }),
      "POST /v1/approvals/ap-1/reject": () => ({ body: {} }),
    });
    const one = await api.approvals.get("ap-1");
    expect(one.solo).toEqual({ actionDigest: digest, freshProofNeeded: false, proofs: [] });
    await api.approvals.decide("ap-1", { decision: "approve", reason: "mine", solo: { actionDigest: digest } });
    await api.approvals.decide("ap-1", { decision: "reject", reason: "no", solo: { actionDigest: digest } });
    const posts = calls.filter((c) => c.method === "POST").map((c) => c.body);
    expect(posts[0]).toEqual({ reason: "mine", acknowledge_solo: true, action_digest: digest });
    expect(posts[1]).toEqual({ reason: "no" });
  });

  it("a human gate decision posts the round and findings; the acknowledgement only when asked, never a digest (SA-19)", async () => {
    const path = "POST /v1/runs/run-1-abcd/tasks/t1/gates/review/decisions";
    const { api, calls } = client({ [path]: () => ({ body: { outcome: "passed", already_recorded: false } }) });
    const findings = { c1: { approved: true, reason: "ok" } };
    const first = await api.gates.decide("run-1-abcd", "t1", "review", { attempt: 1, submission: 2, findings });
    const second = await api.gates.decide("run-1-abcd", "t1", "review", { attempt: 1, submission: 2, findings, acknowledgeSolo: true });
    const posts = calls.filter((c) => c.method === "POST").map((c) => c.body);
    expect(posts[0]).toEqual({ attempt: 1, submission: 2, findings });
    expect(posts[1]).toEqual({ attempt: 1, submission: 2, findings, acknowledge_solo: true });
    expect(first).toEqual({ outcome: "passed", alreadyRecorded: false });
    expect(second.outcome).toBe("passed");
  });

  it("reads the proposals with their solo share and posts a grant with what the person read", async () => {
    // `Proposal.to_json()`: the status word for a cell that can be granted is `proposable`, with no reasons
    const item = { subject: { kind: "role", id: "writer" }, cell: { role: "writer", rule_key: 3 }, kind: "write_file", status: "proposable", reasons: [], evidence: { approved_asks: 5 }, evidence_hash: "h", solo: { approved_asks_solo: 3, solo_share: "0.6000" } };
    const { api, calls } = client({
      "GET /v1/autonomy/proposals": () => ({ body: { items: [item], ledger_position: 9, as_of: "2026-11-12T10:00:00Z" } }),
      "POST /v1/autonomy/grants": () => ({ body: {} }),
    });
    const got = await api.autonomy.proposals("writer");
    expect(calls[0]?.url.search).toBe("?role=writer");
    expect(got.items[0]).toMatchObject({ status: "proposable", role: "writer", kind: "write_file", approvedAsks: 5, ruleKey: 3, solo: { approvedAsksSolo: 3, soloShare: "0.6000" } });
    await api.autonomy.grant({ subject: { kind: "role", id: "writer" }, ruleKey: 3, asOf: got.asOf, ledgerPosition: got.ledgerPosition, evidenceHash: "h", reason: "r" });
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ subject: { kind: "role", id: "writer" }, rule_key: 3, as_of: "2026-11-12T10:00:00Z", ledger_position: 9, evidence_hash: "h", reason: "r" });
  });

  it("a peer approval has no solo terms", async () => {
    const peer = { ...pending, action_digest: "e".repeat(64), caller_terms: { mode: "peer", fresh_proof_needed: false, proofs: [] } };
    const { api } = client({ "GET /v1/approvals/ap-1": () => ({ body: peer }), "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }) });
    expect((await api.approvals.get("ap-1")).solo).toBeNull();
  });

  it("a sole decider's terms carry the proofs the server accepts, the agent's role, and whether they started the run (SA-7, SA-17)", async () => {
    const terms = { mode: "solo", fresh_proof_needed: true, proofs: ["password", "sign_in"] };
    const me = { id: "human:u_ann1", org_id: "demo", role: "owner", email: "ann@example.test", display_name: "Ann" };
    const routes = (startedBy: string): Record<string, Handler> => ({
      "GET /v1/auth/session": () => ({ body: { user: me, csrf_token: null } }),
      "GET /v1/approvals/ap-1": () => ({ body: { ...pending, action_digest: "d".repeat(64), caller_terms: terms } }),
      "GET /v1/runs/run-1-abcd": () => ({ body: { ...(rawDone as object), started_by: startedBy } }),
      "GET /v1/agents": () => ({ body: { agents: [{ id: "dana", name: "Dana", role: "developer", active_credentials: 1 }] } }),
    });
    const mine = client(routes("human:u_ann1"));
    await mine.api.auth?.load();
    expect(await mine.api.approvals.get("ap-1")).toMatchObject({ solo: { freshProofNeeded: true, proofs: ["password", "sign_in"] }, role: "developer", startedByYou: true });
    const other = client(routes("human:u_bob2"));
    await other.api.auth?.load();
    expect(await other.api.approvals.get("ap-1")).toMatchObject({ role: "developer", startedByYou: false });
  });

  it("the role is a courtesy: unknown when the agents cannot be read, never asked for a peer", async () => {
    const soloPending = { ...pending, action_digest: "d".repeat(64), caller_terms: { mode: "solo", fresh_proof_needed: false, proofs: "odd" } };
    const solo = client({ "GET /v1/approvals/ap-1": () => ({ body: soloPending }), "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }) });
    expect(await solo.api.approvals.get("ap-1")).toMatchObject({ role: null, startedByYou: false, solo: { proofs: [] } });
    const peer = client({ "GET /v1/approvals/ap-1": () => ({ body: { ...pending, caller_terms: { mode: "peer", fresh_proof_needed: false, proofs: [] } } }), "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }) });
    await peer.api.approvals.get("ap-1");
    expect(peer.calls.some((c) => c.url.pathname === "/v1/agents")).toBe(false);
  });

  it("a refused decision (already decided) is a NotAllowedError carrying the server's words", async () => {
    const { api } = client({ "POST /v1/approvals/ap-1/reject": () => problem(409, "approval_refused", "approval 'ap-1' is already decided") });
    const error = await api.approvals.decide("ap-1", { decision: "reject", reason: "no" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NotAllowedError);
    expect((error as Error).message).toContain("already decided");
  });
});

describe("approvals: the routes and the Idempotency-Key (W2, W3)", () => {
  const decided = { id: "ap-1", run_id: "run-1-abcd", task_id: null, agent_id: "dana", action: { kind: "run_command", target: "pytest", amount: null }, asked_because: "rule", status: "denied", requested_at: "2026-10-24T10:00:00Z", decided_by: "human:you", decided_at: "2026-10-24T10:05:00Z", decision_reason_hash: "b".repeat(64), decision_reason: null, ledger: [] };
  const keyOf = (c: { headers: Record<string, string> } | undefined) => c?.headers["idempotency-key"];

  it("a denial posts to /reject with the reason, and the app never calls the deprecated /decision", async () => {
    const { api, calls } = client({ "POST /v1/approvals/ap-1/reject": () => ({ body: {} }), "GET /v1/approvals/ap-1": () => ({ body: decided }), "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }) });
    await api.approvals.decide("ap-1", { decision: "reject", reason: "not today" });
    const post = calls.find((c) => c.method === "POST");
    expect(post?.url.pathname).toBe("/v1/approvals/ap-1/reject");
    expect(post?.body).toEqual({ reason: "not today" });
  });

  it("a decision carries an Idempotency-Key, and the repeat after a lost answer carries the same one", async () => {
    let attempts = 0;
    const { api, calls } = client({
      "POST /v1/approvals/ap-1/approve": () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError("network dropped after the server committed");
        return { body: {} };
      },
      "GET /v1/approvals/ap-1": () => ({ body: decided }),
      "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }),
    });
    await expect(api.approvals.decide("ap-1", { decision: "approve", reason: "ok" })).rejects.toMatchObject({ status: 0 });
    await api.approvals.decide("ap-1", { decision: "approve", reason: "ok" });
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(2);
    expect(keyOf(posts[0])).toMatch(/^[0-9a-f-]{36}$/);
    expect(keyOf(posts[1])).toBe(keyOf(posts[0]));
  });

  it("a new action after a delivered answer gets a new key", async () => {
    const { api, calls } = client({
      "POST /v1/approvals/ap-1/approve": () => ({ body: {} }),
      "GET /v1/approvals/ap-1": () => ({ body: decided }),
      "GET /v1/runs/run-1-abcd": () => ({ body: rawDone }),
    });
    await api.approvals.decide("ap-1", { decision: "approve", reason: "ok" });
    await api.approvals.decide("ap-1", { decision: "approve", reason: "ok" });
    const [a, b] = calls.filter((c) => c.method === "POST");
    expect(keyOf(a)).toBeDefined();
    expect(keyOf(b)).not.toBe(keyOf(a));
  });

  it("the kill switch sends no key (the idempotency notes 8.2) and a secret-bearing route sends none either", async () => {
    const raw = { org: "o", halted: false, halted_at: null, halted_by: null, reason: null, roles: [] };
    const { api, calls } = client({
      "POST /v1/org/halt": () => ({ body: { ...raw, stopped_runs: [] } }),
      "POST /v1/org/roles/dev/pause": () => ({ body: raw }),
      "POST /v1/org/resume": () => ({ body: raw }),
      "POST /v1/agents/dana/credentials": () => ({ body: {} }),
    });
    await api.org.halt();
    await api.org.pauseRole("dev").catch(() => undefined);
    await api.org.resume();
    await api.agentCredentials.issue("dana", { scopes: [], days: 1 }).catch(() => undefined);
    const byPath = Object.fromEntries(calls.map((c) => [c.url.pathname, keyOf(c)]));
    expect(byPath["/v1/org/halt"]).toBeUndefined();
    expect(byPath["/v1/org/roles/dev/pause"]).toBeUndefined();
    expect(byPath["/v1/agents/dana/credentials"]).toBeUndefined();
    expect(byPath["/v1/org/resume"]).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("org status (W3)", () => {
  it("reads /v1/org/status, not the deprecated GET /v1/org/halt", async () => {
    const raw = { org: "o", halted: false, halted_at: null, halted_by: null, reason: null, roles: [] };
    const { api, calls } = client({ "GET /v1/org/status": () => ({ body: raw }) });
    expect((await api.org.status()).halted).toBe(false);
    expect(calls[0]?.url.pathname).toBe("/v1/org/status");
  });
});

describe("budgets", () => {
  const raw = (over: object) => ({ id: "org-daily", scope: "org", scope_id: "demo", limit: { amount: "30", currency: "USD" }, spent: { amount: "4", currency: "USD" }, reserved: { amount: "0", currency: "USD" }, available: { amount: "26", currency: "USD" }, blocked: false, window: "day", on_breach: "reset", window_start: "2026-10-24T00:00:00.000000Z", version: 1, ...over });
  const routes = (budgets: object[]): Record<string, Handler> => ({
    "GET /v1/budgets": () => ({ body: budgets }),
    "GET /v1/runs": () => ({ body: rawRunList }),
    [`GET /v1/runs/${RUN}`]: () => ({ body: rawDone }),
    "GET /v1/runs/run-20261023-193348-ef9e": () => ({ body: rawNeedsHuman }),
    "GET /v1/budgets/dana-daily": () => ({ body: raw({ id: "dana-daily", history: [{ ledger_seq: 3, type: "budget.defined", at: "x", actor: "a", payload: {} }, { ledger_seq: 8, type: "budget.breached", at: "2026-10-24T09:00:00Z", actor: "system", payload: {} }] }) }),
  });

  it("lists budgets with their window and breach mode", async () => {
    const { api } = client(routes([raw({}), raw({ id: "dana-daily", scope: "agent", scope_id: "dana", on_breach: "hold" })]));
    const rows = await api.budgets.list();
    expect(rows.map((r) => [r.window, r.onBreach])).toEqual([["day", "reset"], ["day", "hold"]]);
  });

  it("says what the averages cover and that refusals are not known; keeps the server's currency", async () => {
    const { api } = client(routes([raw({})]));
    const s = await api.budgets.summary();
    expect(s.unknownPriceRefusals).toBeNull();
    expect(s.coverage).toBe("all 2 runs");
  });

  it("the summary uses the org's daily budget, counts runs and finds the breach with its window wording", async () => {
    const { api } = client(routes([raw({ spent: { amount: "1.262", currency: "USD" } }), raw({ id: "dana-daily", scope: "agent", scope_id: "dana", limit: { amount: "0.01", currency: "USD" }, blocked: true, on_breach: "hold" })]));
    const s = await api.budgets.summary();
    expect(s.orgWindow).toBe("day");
    expect(s.orgMonth).toEqual({ spent: { amount: "1.262", currency: "USD" }, limit: { amount: "30", currency: "USD" } });
    expect(s.runs).toEqual({ total: 2, finished: 2, running: 0, blocked: 1 });
    expect(s.verifiedTasks).toBe(3);
    expect(s.costPerVerifiedTask.amount).toBe("0.000366"); // 0.0010965 / 3 = 0.0003655 from the exact amounts, rounded half up
    expect(s.breach).toMatchObject({ budgetId: "dana-daily", window: "day", onBreach: "hold", resetsAt: null, ledgerEntry: "8", since: "2026-10-24T09:00:00Z", nextCall: null });
  });

  it("a reset-mode breach knows when it clears; no org budget at all is null, not zero", async () => {
    const { api } = client(routes([raw({ id: "dana-daily", scope: "agent", scope_id: "dana", blocked: true, on_breach: "reset" })]));
    const s = await api.budgets.summary();
    expect(s.orgMonth).toBeNull();
    expect(s.breach?.resetsAt).toBe("2026-10-25T00:00:00.000Z");
  });

  it("spend by model adds the runs up exactly", async () => {
    const { api } = client(routes([]));
    const [row] = await api.spend.byModel();
    expect(row).toMatchObject({ name: "azure-openai/gpt-5.4-mini", meta: "4 calls", sharePercent: 100 });
    expect(row?.cost.amount).toBe("0.002192"); // 0.0010965 + 0.001095, each rounded to six places first
  });

  it("unblock posts reason and new_limit, then reads the ledger entry", async () => {
    const { api, calls } = client({
      "POST /v1/budgets/dana-daily/unblock": () => ({ body: { id: "dana-daily", blocked: false, limit: {} } }),
      "GET /v1/budgets/dana-daily": () => ({ body: raw({ id: "dana-daily", history: [{ ledger_seq: 21, type: "budget.unblocked", at: "x", actor: "human:you", payload: {} }] }) }),
    });
    const result = await api.budgets.unblock("dana-daily", { reason: "raise it", newLimit: "60" });
    expect(calls[0]?.body).toEqual({ reason: "raise it", new_limit: "60" });
    expect(result.ledgerEntry).toBe("21");
  });
});

describe("ledger and verification", () => {
  it("lists entries newest first across streams, filters by category and search, and opens one entry", async () => {
    const { api } = client({
      "GET /v1/ledger": () => ({ body: { streams: [{ stream: "run:run-20261023-193348-ef9e", seq: 32, head: "h", updated: "2026-10-23T19:33:52Z" }] } }),
      "GET /v1/ledger/run%3Arun-20261023-193348-ef9e": (url) => {
        const after = Number(url.searchParams.get("after_seq"));
        const events = (rawLedger as { events: { seq: number }[] }).events.filter((e) => e.seq > after).slice(0, Number(url.searchParams.get("limit")));
        return { body: { stream: "run:run-20261023-193348-ef9e", events, head_seq: 32, head: "h", next_after_seq: after } };
      },
    });
    const all = await api.ledger.list();
    expect(all).toHaveLength(32);
    expect(all[0]?.label).toBe("32");
    const gates = await api.ledger.list({ category: "gates" });
    expect(gates.length).toBeGreaterThan(0);
    expect(gates.every((e) => e.category === "gates")).toBe(true);
    expect((await api.ledger.list({ search: "run.ended" })).map((e) => e.kind)).toEqual(["run.ended"]);
    expect((await api.ledger.get("run:run-20261023-193348-ef9e@16")).kind).toBe("gate.completed");
    await expect(api.ledger.get("1043")).rejects.toBeInstanceOf(NotFoundError);
    await expect(api.ledger.get("run:run-20261023-193348-ef9e@99")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("verifies everything and follows 'truncated' pages", async () => {
    const first = { ...(rawVerifyAnchored as object), truncated: true, next_after: "run:run-20261023-193348-ef9e", streams: (rawVerifyAnchored as { streams: unknown[] }).streams.slice(0, 1) };
    const second = { ...(rawVerifyAnchored as object), streams: (rawVerifyAnchored as { streams: unknown[] }).streams.slice(1) };
    const { api, calls } = client({ "GET /v1/ledger/verify": (url) => ({ body: url.searchParams.has("after") ? second : first }) });
    const status = await api.ledger.verifyChain();
    expect(calls.map((c) => c.url.searchParams.get("after"))).toEqual([null, "run:run-20261023-193348-ef9e"]);
    expect(status).toMatchObject({ verified: true, anchorState: "anchored", entries: 73 });
    expect(status.streams).toHaveLength(2);
  });

  it("a page that is unanchored makes the whole answer not verified", async () => {
    const un = { ok: true, chain_ok: true, verified: false, streams: [{ stream: "run:a", ok: true, checked: 4, problem: null, anchors: { status: "unanchored", anchored: false, count: 0, unanchored_events: 4, problems: [] } }], anchors: { configured: true, checked: true } };
    const { api } = client({ "GET /v1/ledger/verify": () => ({ body: un }), "GET /v1/runs/a/ledger/verify": () => ({ body: { ...un.streams[0], ok: true, verified: false, chain_ok: true, anchors: un.streams[0]?.anchors, anchor_status: un.anchors } }) });
    expect((await api.ledger.verifyChain()).verified).toBe(false);
    const one = await api.ledger.verifyRun("a");
    expect(one).toMatchObject({ verified: false, anchorState: "unanchored" });
  });
});

describe("info", () => {
  it("names the organization and whether people can act", async () => {
    const { api } = client({ "GET /v1/health": () => ({ body: { status: "ok", streams: 2, org: "demo", humans_enabled: false } }) }, "http://127.0.0.1:8000");
    expect(await api.info()).toEqual({ name: "demo", detail: "127.0.0.1:8000 · read-only" });
  });
});

describe("not wired on purpose", () => {
  it("organization storage, templates and presets reject as not available (never sample data)", async () => {
    const { api } = client({});
    await expect(api.orgs.get()).rejects.toBeInstanceOf(NotAvailableError);
    await expect(api.templates.list()).rejects.toBeInstanceOf(NotAvailableError);
    await expect(api.policyPresets.list()).rejects.toBeInstanceOf(NotAvailableError);
    expect(api.source).toBe("server");
  });
});

describe("agent credentials (E2)", () => {
  const raw = (over: Record<string, unknown> = {}) => ({
    id: "agc_" + "a".repeat(24), agent: "dana", scopes: ["read", "run"], active: true,
    created_at: "2026-10-26T12:00:00+00:00", expires_at: "2026-11-25T12:00:00+00:00", revoked_at: null,
    issuer_erased: false, in_config: true, ...over,
  });

  it("maps the six routes and never keeps a secret in a list", async () => {
    const secret = "nma_" + "f".repeat(43);
    const { api, calls } = client({
      "GET /v1/agents/dana/credentials": () => ({ body: { credentials: [raw(), raw({ active: false, revoked_at: "2026-10-27T00:00:00+00:00" })] } }),
      "GET /v1/agent-credentials": () => ({ body: { credentials: [raw({ issuer_erased: true })] } }),
      "POST /v1/agents/dana/credentials": () => ({ status: 201, body: { ...raw(), credential: secret } }),
      [`POST /v1/agents/dana/credentials/agc_${"a".repeat(24)}/rotate`]: () => ({ status: 201, body: { ...raw(), credential: secret } }),
      [`POST /v1/agents/dana/credentials/agc_${"a".repeat(24)}/revoke`]: () => ({ body: raw({ active: false, revoked_at: "2026-10-27T00:00:00+00:00" }) }),
      "POST /v1/agents/dana/credentials/revoke-all": () => ({ body: { revoked: 2 } }),
    });
    const listed = await api.agentCredentials.list({ agent: "dana" });
    expect(listed.map((c) => c.state)).toEqual(["active", "revoked"]);
    expect(JSON.stringify(listed)).not.toContain("nma_");
    expect((await api.agentCredentials.list({ issuerErased: true }))[0]?.issuerErased).toBe(true);
    expect(calls.at(-1)?.url.search).toBe("?issuer_erased=true");
    expect((await api.agentCredentials.issue("dana", { scopes: ["read", "run"], days: 7 })).secret).toBe(secret);
    expect(calls.at(-1)?.body).toEqual({ scopes: ["read", "run"], expires_in_days: 7 });
    await api.agentCredentials.rotate("dana", "agc_" + "a".repeat(24), { days: 30, graceMinutes: 5 });
    expect(calls.at(-1)?.body).toEqual({ expires_in_days: 30, grace_seconds: 300 });
    expect((await api.agentCredentials.revoke("dana", "agc_" + "a".repeat(24))).state).toBe("revoked");
    expect(await api.agentCredentials.revokeAll("dana")).toBe(2);
  });

  it("reads the agent list and the caller's role; no session is an unknown role", async () => {
    const { api } = client({
      "GET /v1/agents": () => ({ body: { agents: [{ id: "dana", name: "Dana", role: "developer", active_credentials: 2 }] } }),
      "GET /v1/auth/session": () => ({ body: { user: { id: "u_1", role: "approver", email: "a@example.org", display_name: "A" } } }),
    });
    expect(await api.agentCredentials.agents()).toEqual([{ id: "dana", name: "Dana", role: "developer", activeCredentials: 2 }]);
    expect(await api.agentCredentials.callerRole()).toBe("approver");
    const none = client({ "GET /v1/auth/session": () => problem(401, "authentication_required", "sign in") });
    expect(await none.api.agentCredentials.callerRole()).toBeNull();
  });

  it("turns the owner-only refusal and the 409s into the typed errors", async () => {
    const { api } = client({
      "GET /v1/agents/dana/credentials": () => problem(403, "forbidden", "owner only"),
      "POST /v1/agents/dana/credentials": () => problem(409, "agent_suspended", "halted"),
    });
    await expect(api.agentCredentials.list({ agent: "dana" })).rejects.toBeInstanceOf(NotAllowedError);
    await expect(api.agentCredentials.issue("dana", { scopes: ["read"], days: 1 })).rejects.toMatchObject({ code: "agent_suspended" });
  });
});

describe("runs.taskAudit", () => {
  const path = "/v1/runs/run%2F1/tasks/t%201/audit";
  it("encodes both ids in the URL and maps the body", async () => {
    const { taskAuditF1 } = await import("./fixtures/task-audit");
    const { api, calls } = client({ [`GET /v1/runs/run%2F1/tasks/t%201/audit`]: () => ({ body: taskAuditF1() }) });
    const a = await api.runs.taskAudit("run/1", "t 1");
    expect(calls[0]?.url.pathname).toBe(path);
    expect(a.task.title).toBe("Draft summary");
  });

  it("turns 404 into NotFoundError and 403 forbidden into NotAllowedError", async () => {
    const missing = client({ [`GET ${path}`]: () => problem(404, "not_found", "no such task") });
    await expect(missing.api.runs.taskAudit("run/1", "t 1")).rejects.toBeInstanceOf(NotFoundError);
    const denied = client({ [`GET ${path}`]: () => problem(403, "forbidden", "not allowed") });
    await expect(denied.api.runs.taskAudit("run/1", "t 1")).rejects.toBeInstanceOf(NotAllowedError);
    await expect(denied.api.runs.taskAudit("run/1", "t 1")).rejects.toMatchObject({ code: "forbidden" });
    const other = client({ [`GET ${path}`]: () => problem(403, "csrf_invalid", "token") });
    await expect(other.api.runs.taskAudit("run/1", "t 1")).rejects.toMatchObject({ status: 403 });
  });
});

describe("first-run reads", () => {
  it("asks for one run, fifty approvals and one invite, and hands the raw body back", async () => {
    const { api, calls } = client({
      "GET /v1/runs": () => ({ body: { runs: [] } }),
      "GET /v1/approvals": () => ({ body: { items: [], next_cursor: null } }),
      "GET /v1/auth/invites": () => ({ body: { invites: [], next_cursor: null } }),
    });
    expect(await api.firstRun?.runs()).toEqual({ runs: [] });
    expect(await api.firstRun?.approvals()).toEqual({ items: [], next_cursor: null });
    await api.firstRun?.invites();
    expect(calls.map((c) => `${c.url.pathname}${c.url.search}`)).toEqual([
      "/v1/runs?limit=1",
      "/v1/approvals?limit=50",
      "/v1/auth/invites?limit=1",
    ]);
  });
});
