import { describe, expect, it } from "vitest";

import type { ApiClient } from "./client";
import { createServerClient } from "./server-client";

/**
 * The Idempotency-Key guard (the idempotency notes section 8), by behaviour: every method of the
 * server client is driven against a fake fetch and the header the request carries is recorded. The
 * kill switch (halt, role pause) and the secret-bearing routes (credential issue and rotate, every
 * /v1/auth/ call, model keys) never send a key; the listed writes always do. The method list is read
 * from the client itself, so a new method fails here until it is classified.
 */

type Kind = "keyed" | "keyless-write" | "read";
interface Case {
  kind: Kind;
  args: () => unknown[];
}

const pw = "obviously-fake-test-password";
const entry = { provider: "p", model: "m", reason: "r" };
const reason = "r";
const read = (...args: unknown[]): Case => ({ kind: "read", args: () => args });
const write = (...args: unknown[]): Case => ({ kind: "keyless-write", args: () => args });
const keyed = (...args: unknown[]): Case => ({ kind: "keyed", args: () => args });

const CASES: Record<string, Case> = {
  info: read(),
  "orgs.get": read(),
  "orgs.update": read({}, "h"),
  "config.validate": read({}),
  "config.export": read("yaml"),
  "config.import": read("", "yaml"),
  "templates.list": read(),
  "templates.get": read("t"),
  "templates.use": read("t"),
  "templates.saveAs": read({ id: "t", name: "n", description: "d" }),
  "templates.listVersions": read("t"),
  "templates.versionYaml": read("t", 1),
  "policyPresets.list": read(),
  "policyPresets.get": read("p"),
  "policyPresets.create": read({}),
  "policyPresets.update": read("p", {}),
  "policyPresets.assign": read("r", "p", "policy"),
  "runs.list": read(),
  "runs.listPage": read(),
  "runs.get": read("run-1"),
  "runs.taskAudit": read("run-1", "t"),
  "runs.start": read({ template: "t", budget: { amount: "1", currency: "USD" } }),
  "runs.pause": read("run-1"),
  "runs.reportUrl": read("run-1"),
  "runs.stop": keyed("run-1", "why"),
  "runs.events": read("run-1"),
  "runs.subscribe": read("run-1", () => undefined),
  "approvals.list": read(),
  "approvals.get": read("a1"),
  "approvals.decide": keyed("a1", { decision: "approve", reason }),
  "gates.decide": keyed("run-1", "t", "g", { attempt: 1, submission: "s", findings: [] }),
  "autonomy.proposals": read("dev"),
  "autonomy.grant": keyed({ subject: "dev", ruleKey: "k", asOf: "2026-01-01T00:00:00Z", ledgerPosition: 1, evidenceHash: "h", reason }),
  "budgets.list": read(),
  "budgets.summary": read(),
  "budgets.unblock": keyed("b1", { reason }),
  "spend.byModel": read(),
  "reports.downgrade": read(),
  "savings.tokens": read(),
  "dashboards.approvals": read(),
  "dashboards.costLatency": read(),
  "dashboards.verification": read(),
  "dashboards.verifiers": read(),
  "ledger.list": read(),
  "ledger.get": read("1"),
  "ledger.verifyChain": read(),
  "ledger.chainStatus": read(),
  "ledger.verifyRun": read("run-1"),
  "org.status": read(),
  "org.halt": write(),
  "org.resume": keyed(),
  "org.pauseRole": write("dev"),
  "org.resumeRole": keyed("dev"),
  "breakers.list": read(),
  "breakers.reset": keyed("b"),
  "breakers.disable": keyed("b", "other"),
  "breakers.enable": keyed("b"),
  "agentCredentials.agents": read(),
  "agentCredentials.callerRole": read(),
  "agentCredentials.list": read(),
  "agentCredentials.issue": write("a", { scopes: [], days: 1 }),
  "agentCredentials.rotate": write("a", "c1", { days: 1, graceMinutes: 1 }),
  "agentCredentials.revoke": keyed("a", "c1"),
  "agentCredentials.revokeAll": keyed("a"),
  "models.catalog": read(),
  "models.events": read(),
  "models.connectionHistory": read({}),
  "models.assignments": read(),
  "models.connections": read(),
  "models.resolution": read("internal"),
  "models.addEntry": write({ ...entry, family: "f", processing: "p" }),
  "models.editEntry": write(entry),
  "models.approve": write(entry),
  "models.pause": write(entry),
  "models.retire": write(entry),
  "models.remove": write(entry),
  "models.setPrice": write({ ...entry, inputPerMillion: "1", outputPerMillion: "1", effectiveFrom: "2026-01-01", source: "s" }),
  "models.setRegions": write({ customerRegions: [], reason }),
  "models.assign": write({ scope: "role", id: "dev", model: null, reason }),
  "models.putConnection": write("p", { endpoint: "e" }),
  "models.removeConnection": write("p", reason),
  "models.testConnection": write("p"),
  "models.keyState": read({ provider: "p" }),
  "models.keysHealth": read(),
  "models.setKey": write({ provider: "p", value: "obviously-fake-test-key-not-a-secret" }),
  "models.revokeKey": write({ provider: "p", reason }),
  "session.current": read(),
  "auth.state": read(),
  "auth.subscribe": read(() => undefined),
  "auth.load": read(),
  "auth.csrf": read(),
  "auth.recover": read(),
  "auth.refreshCsrf": read(),
  "auth.login": write("a@example.com", pw),
  "auth.completeMfa": write("c", { code: "000000" }),
  "auth.logout": write(),
  // The multi-factor routes honour no Idempotency-Key (a code or a recovery code is single use: a replay must never be offered).
  "auth.hold": read(),
  "auth.held": read(),
  "auth.mfaStatus": read(),
  "auth.mailEnabled": read(),
  "auth.mfaEnroll": write(pw),
  "auth.mfaConfirm": write({ factorId: "f", code: "000000" }),
  "auth.mfaRegenerate": write({ password: pw, code: "000000" }),
  "auth.mfaDisable": write({ password: pw, code: "000000" }),
  "auth.confirmPassword": write(pw),
  "auth.loginEnroll": write("nmc_challenge"),
  "auth.loginEnrollConfirm": write({ challenge: "nmc_challenge", factorId: "f", code: "000000" }),
  "auth.setupStatus": read(),
  "auth.redeemSetup": write({ token: "t", email: "a@example.com", displayName: "n", password: pw }),
  "emailChange.start": write({ newEmail: "a@example.com", password: pw }),
  "emailChange.verify": write("t"),
  "firstRun.runs": read(),
  "firstRun.approvals": read(),
  "firstRun.invites": read(),
  "invites.list": read(),
  "invites.create": write({ email: "a@example.com", role: "member" }),
  "invites.revoke": write("i1"),
  "invites.resend": write("i1"),
  "invites.inspect": read("t"),
  "invites.accept": write({ token: "t", password: pw }),
  "passwordReset.request": write("a@example.com"),
  "passwordReset.redeem": write({ token: "t", password: pw }),
  "passwordReset.revokeByLink": write("t"),
};

/** Every function the client exposes, as "group.method" (or "method" at the top). */
function methodsOf(api: unknown, prefix = ""): string[] {
  if (typeof api !== "object" || api === null) return [];
  return Object.entries(api).flatMap(([name, value]) => {
    const path = `${prefix}${name}`;
    if (typeof value === "function") return [path];
    return methodsOf(value, `${path}.`);
  });
}

function resolve(api: ApiClient, path: string): (...args: unknown[]) => unknown {
  let node: unknown = api;
  for (const part of path.split(".")) node = (node as Record<string, unknown>)[part];
  const parent = path.includes(".") ? path.slice(0, path.lastIndexOf(".")).split(".").reduce<unknown>((n, p) => (n as Record<string, unknown>)[p], api) : api;
  return (...args) => (node as (...a: unknown[]) => unknown).apply(parent, args);
}

function newClient() {
  const sent: { method: string; path: string; key: string | null }[] = [];
  const fetchFn = (input: string, init?: RequestInit): Promise<Response> => {
    const headers = new Headers(init?.headers);
    sent.push({ method: init?.method ?? "GET", path: new URL(input, "http://x").pathname, key: headers.get("idempotency-key") });
    return Promise.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
  };
  const api = createServerClient({
    fetch: fetchFn,
    tabSync: { notify: () => undefined, listen: () => () => undefined },
    socket: () => {
      throw new Error("no sockets in this test");
    },
  });
  return { api, sent };
}

describe("Idempotency-Key per client method", () => {
  it("classifies every method the client has, and nothing that is gone", () => {
    const found = methodsOf(newClient().api).sort();
    expect(found).toEqual(Object.keys(CASES).sort());
  });

  for (const [path, spec] of Object.entries(CASES)) {
    it(`${path}: ${spec.kind === "keyed" ? "sends a key on every write" : "sends no key"}`, async () => {
      const { api, sent } = newClient();
      try {
        await resolve(api, path)(...spec.args());
      } catch {
        // the fake answers `{}`: a method may fail to read it, the request it sent is what counts
      }
      const writes = sent.filter((c) => c.method !== "GET");
      if (spec.kind === "keyed") {
        expect(writes.length, `${path} sent no write`).toBeGreaterThan(0);
        for (const c of writes) expect(c.key, `${c.method} ${c.path}`).toMatch(/^[0-9a-f-]{36}$/);
      } else {
        if (spec.kind === "keyless-write") expect(writes.length, `${path} sent no write`).toBeGreaterThan(0);
        for (const c of sent) expect(c.key, `${c.method} ${c.path}`).toBeNull();
      }
    });
  }

  it("the first-run probes are plain GETs and carry no key", async () => {
    const { api, sent } = newClient();
    await api.firstRun?.runs().catch(() => undefined);
    await api.firstRun?.approvals().catch(() => undefined);
    await api.firstRun?.invites().catch(() => undefined);
    expect(sent.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /v1/runs",
      "GET /v1/approvals",
      "GET /v1/auth/invites",
    ]);
    expect(sent.every((c) => c.key === null)).toBe(true);
  });

  it("the keyed set is exactly the spec's list", () => {
    const keyedNames = Object.entries(CASES).filter(([, c]) => c.kind === "keyed").map(([n]) => n).sort();
    expect(keyedNames).toEqual([
      "agentCredentials.revoke",
      "agentCredentials.revokeAll",
      "approvals.decide",
      "autonomy.grant",
      "breakers.disable",
      "breakers.enable",
      "breakers.reset",
      "budgets.unblock",
      "gates.decide",
      "org.resume",
      "org.resumeRole",
      "runs.stop",
    ]);
  });

  it("approve and reject both carry a key, halt and role pause hit their route without one", async () => {
    for (const decision of ["approve", "reject"] as const) {
      const { api, sent } = newClient();
      await api.approvals.decide("a1", { decision, reason }).catch(() => undefined);
      const post = sent.find((c) => c.method === "POST");
      expect(post?.path).toMatch(decision === "approve" ? /\/approve$/ : /\/reject$/);
      expect(post?.key).not.toBeNull();
    }
    const { api, sent } = newClient();
    await api.org.halt().catch(() => undefined);
    await api.org.pauseRole("dev").catch(() => undefined);
    expect(sent.map((c) => c.path)).toEqual(["/v1/org/halt", "/v1/org/roles/dev/pause"]);
    expect(sent.every((c) => c.key === null)).toBe(true);
  });
});
