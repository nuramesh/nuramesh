import { describe, expect, it } from "vitest";

import { fixtureBackedFetch, fixtureBackedServerClient } from "@/test/fixture-backed-server";

import { NotAllowedError, NotFoundError, ProblemError } from "./client";
import { createFixtureClient } from "./fixture-client";
import { fixtureOrgSafety } from "./index";
import { runOrgSafetyContract } from "./org-safety-contract";
import { createServerClient } from "./server-client";

runOrgSafetyContract("fixtures", (scenario) => createFixtureClient(scenario === undefined ? {} : { orgSafety: scenario }));
runOrgSafetyContract("server client over the org routes", (scenario) => fixtureBackedServerClient(scenario));

describe("the answers of the organization safety notes section 11, mapped", () => {
  const halt = { halted: false, by: null, at: null, cause: null, rule: null, ledger_seq: null, phase: null, error_class: null, failing_rule: null, failed_ticks: null, roles: [], can_act: true };
  const rule = {
    name: "spend-spike", signal: "spend", action: "halt_org", role: null, window_seconds: 3600, dry_run: false, limit: "25", observed: "3.5", observed_as: "live",
    unknown_cost_calls: 0, window_start: "2026-11-01T11:00:00+00:00", state: "ok", tripped_at: null, tripped_seq: null, tripped_by: null, manual: false,
    last_reset_at: null, disabled_at: null, disabled_by: null, disabled_reason: null, in_config: true,
  };
  const evaluation = { state: "ok", tick_seconds: 10, halt_after_failed_ticks: 3, last_evaluated_at: "2026-11-01T12:00:00+00:00", last_error_at: null, last_error_phase: null, last_error_class: null, last_failing_rule: null, consecutive_failures: 0 };
  const serve = (breakers: unknown[], over: object = {}) =>
    createServerClient({ fetch: () => Promise.resolve(new Response(JSON.stringify({ org: "acme", halt, evaluation: { ...evaluation, ...over }, breakers }), { status: 200 })) });

  it("keeps decimal strings exactly as sent", async () => {
    const view = await serve([{ ...rule, limit: "25.000000", observed: "3.50000001" }]).breakers.list();
    expect(view.breakers[0]).toMatchObject({ limit: "25.000000", observed: "3.50000001", windowSeconds: 3600, observedAs: "live" });
    expect(view.evaluation).toMatchObject({ state: "ok", tickSeconds: 10, haltAfterFailedTicks: 3 });
  });

  it("maps a disabled orphan whose signal, action, window and limit are null", async () => {
    const orphan = { ...rule, name: "old", signal: null, action: null, window_seconds: null, dry_run: null, limit: null, observed: null, observed_as: null, window_start: null, unknown_cost_calls: null, state: "disabled", disabled_at: "2026-11-01T11:30:00+00:00", disabled_by: "human:ann", disabled_reason: "other", manual: null, in_config: false };
    const row = (await serve([orphan]).breakers.list()).breakers[0]!;
    expect(row).toMatchObject({ signal: null, action: null, windowSeconds: null, limit: null, observed: null, observedAs: null, dryRun: false, unknownCostCalls: 0, state: "disabled", disabledReason: "other", inConfig: false });
  });

  it("refuses a state it does not know instead of showing it as healthy", async () => {
    await expect(serve([{ ...rule, state: "armed" }]).breakers.list()).rejects.toThrow(/unknown breaker state/);
  });

  it("shows an evaluation state it does not know as stopped, never as ok", async () => {
    expect((await serve([rule], { state: "mystery" }).breakers.list()).evaluation.state).toBe("stopped");
  });
});

describe("the fixture's scenario in the address", () => {
  it("picks a known scenario from ?orgSafety= and ignores anything else", () => {
    expect(fixtureOrgSafety("?orgSafety=halted")).toBe("halted");
    expect(fixtureOrgSafety("?x=1&orgSafety=poison")).toBe("poison");
    expect(fixtureOrgSafety("?orgSafety=nope")).toBeUndefined();
    expect(fixtureOrgSafety("")).toBeUndefined();
  });
});

describe("the fixture client's records", () => {
  it("writes one ledger entry per safety action, by the sample person", async () => {
    const api = createFixtureClient();
    const before = (await api.ledger.list()).length;
    await api.org.halt();
    await api.org.resume();
    await api.org.pauseRole("writer");
    await api.breakers.disable("spend-spike", "other");
    const entries = await api.ledger.list();
    expect(entries.length - before).toBe(4);
    expect(entries.slice(0, 4).map((e) => e.kind)).toEqual(["breaker.disabled", "org.role_paused", "org.resumed", "org.halted"]);
    expect(entries[0]?.actor).toBe("human:ann");
  });

  it("writes nothing when a write is refused", async () => {
    const api = createFixtureClient({ orgSafety: "viewer" });
    const before = (await api.ledger.list()).length;
    await expect(api.org.halt()).rejects.toBeInstanceOf(NotAllowedError);
    expect((await api.ledger.list()).length).toBe(before);
  });
});

describe("the server client's wire format", () => {
  it("calls the nine routes with the right method, path and body", async () => {
    const { fetchFn, calls } = fixtureBackedFetch("tripped");
    const api = createServerClient({ fetch: fetchFn });
    await api.org.status();
    await api.breakers.list();
    await api.breakers.reset("denied-loop");
    await api.breakers.disable("spend-spike", "false_positive");
    await api.breakers.enable("spend-spike");
    await api.org.pauseRole("writer");
    await api.org.resumeRole("writer");
    await api.org.resume();
    await api.org.halt();
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "GET /v1/org/status",
      "GET /v1/org/breakers",
      "POST /v1/org/breakers/denied-loop/reset",
      "POST /v1/org/breakers/spend-spike/disable",
      "POST /v1/org/breakers/spend-spike/enable",
      "POST /v1/org/roles/writer/pause",
      "POST /v1/org/roles/writer/resume",
      "POST /v1/org/resume",
      "POST /v1/org/halt",
    ]);
    expect(calls[3]?.body).toEqual({ reason: "false_positive" });
    expect(calls[2]?.body).toEqual({});
    expect(calls[0]?.body).toBeUndefined();
  });

  it("encodes names in the path", async () => {
    const { fetchFn, calls } = fixtureBackedFetch();
    const api = createServerClient({ fetch: fetchFn });
    await api.breakers.reset("a b/c").catch(() => undefined);
    expect(calls[0]?.path).toBe("/v1/org/breakers/a%20b%2Fc/reset");
  });

  it("maps 403 human_required and forbidden to NotAllowedError with the code, 404 to NotFoundError", async () => {
    const answer = (status: number, title: string) => () =>
      Promise.resolve(new Response(JSON.stringify({ title, status, detail: `detail ${title}` }), { status, headers: { "content-type": "application/problem+json" } }));
    const human = createServerClient({ fetch: answer(403, "human_required") });
    await expect(human.org.halt()).rejects.toMatchObject({ name: "NotAllowedError", code: "human_required" });
    const forbidden = createServerClient({ fetch: answer(403, "forbidden") });
    await expect(forbidden.breakers.reset("x")).rejects.toMatchObject({ name: "NotAllowedError", code: "forbidden" });
    const missing = createServerClient({ fetch: answer(404, "no_such_breaker") });
    await expect(missing.breakers.enable("x")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("keeps other problems as ProblemError (authentication_required, no_config, store_error, unreachable)", async () => {
    const answer = (status: number, title: string) => () =>
      Promise.resolve(new Response(JSON.stringify({ title, status, detail: "d" }), { status, headers: { "content-type": "application/problem+json" } }));
    for (const [status, title] of [[401, "authentication_required"], [403, "authentication_required"], [409, "no_config"], [500, "store_error"]] as const) {
      const api = createServerClient({ fetch: answer(status, title) });
      await expect(api.org.halt()).rejects.toMatchObject({ name: "ProblemError", title, status });
    }
    const down = createServerClient({ fetch: () => Promise.reject(new TypeError("offline")) });
    await expect(down.org.status()).rejects.toBeInstanceOf(ProblemError);
  });

  it("does not turn a missing can_act into permission (fails closed)", async () => {
    const api = createServerClient({
      fetch: () =>
        Promise.resolve(new Response(JSON.stringify({ halted: false, by: null, at: null, cause: null, rule: null, ledger_seq: null, phase: null, error_class: null, failing_rule: null, failed_ticks: null, roles: [] }), { status: 200 })),
    });
    expect((await api.org.status()).canAct).toBe(false);
  });
});
