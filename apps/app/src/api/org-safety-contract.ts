/**
 * The contract of the organization-safety half of `ApiClient` (E19, ND106): the kill switch, role
 * pause and circuit breakers. The fixture client and the server client (over a transport that
 * answers like the server, `src/test/fixture-backed-server.ts`) both run it, so they cannot drift.
 * `make(scenario)` builds a client in one of the fixture states of `spec/e19/README.md` section 12.
 */
import { describe, expect, it } from "vitest";

import { ConflictError, NotAllowedError, NotFoundError, type ApiClient } from "./client";
import type { OrgSafetyScenario } from "./fixture-client";

export function runOrgSafetyContract(name: string, make: (scenario?: OrgSafetyScenario) => ApiClient): void {
  describe(`Org safety contract: ${name}`, () => {
    it("starts running, with three roles and three rules, for a person", async () => {
      const api = make();
      const status = await api.org.status();
      expect(status).toMatchObject({ halted: false, by: null, at: null, cause: null, canAct: true });
      expect(status.roles.map((r) => [r.role, r.paused, r.inConfig])).toEqual([
        ["researcher", false, true],
        ["writer", false, true],
        ["verifier", false, true],
      ]);
      const view = await api.breakers.list();
      expect(view.org).toBe("acme");
      expect(view.evaluation.state).toBe("ok");
      expect(view.halt.canAct).toBe(true);
      expect(view.breakers.map((b) => [b.name, b.signal, b.state, b.dryRun])).toEqual([
        ["spend-spike", "spend", "ok", false],
        ["denied-loop", "policy_denied", "ok", false],
        ["watch-gates", "gate_failed", "ok", true],
      ]);
      const spike = view.breakers[0]!;
      expect(spike).toMatchObject({ limit: "25", observed: "3.5", observedAs: "live", windowSeconds: 3600, action: "halt_org" });
    });

    it("halts once, then says it is already halted; resumes once, then says it is already running", async () => {
      const api = make();
      const halted = await api.org.halt();
      expect(halted).toMatchObject({ halted: true, cause: "human", canAct: true });
      expect(halted.by).toMatch(/^human:/);
      expect(halted.at).not.toBeNull();
      expect(typeof halted.stoppedRuns).toBe("number");
      expect((await api.org.status()).halted).toBe(true);
      await expect(api.org.halt()).rejects.toMatchObject({ name: "ConflictError", code: "already_halted" });
      const resumed = await api.org.resume();
      expect(resumed).toMatchObject({ halted: false, by: null, cause: null });
      expect((await api.org.status()).halted).toBe(false);
      await expect(api.org.resume()).rejects.toMatchObject({ name: "ConflictError", code: "already_running" });
    });

    it("pauses and resumes one role, other roles keep running, repeats are 409, unknown is 404", async () => {
      const api = make();
      const paused = await api.org.pauseRole("researcher");
      expect(typeof paused.stoppedTasks).toBe("number");
      expect(paused.halted).toBe(false);
      expect(paused.roles.find((r) => r.role === "researcher")).toMatchObject({ paused: true, cause: "human" });
      expect(paused.roles.find((r) => r.role === "writer")?.paused).toBe(false);
      await expect(api.org.pauseRole("researcher")).rejects.toMatchObject({ code: "already_paused" });
      await expect(api.org.pauseRole("ghost")).rejects.toBeInstanceOf(NotFoundError);
      const back = await api.org.resumeRole("researcher");
      expect(back.roles.find((r) => r.role === "researcher")?.paused).toBe(false);
      await expect(api.org.resumeRole("researcher")).rejects.toMatchObject({ code: "not_paused" });
      await expect(api.org.resumeRole("ghost")).rejects.toBeInstanceOf(NotFoundError);
    });

    it("resets only a tripped breaker, and a reset never resumes the organization", async () => {
      const api = make("tripped");
      const before = await api.breakers.list();
      expect(before.halt).toMatchObject({ halted: true, cause: "breaker", rule: "denied-loop" });
      const tripped = before.breakers.find((b) => b.name === "denied-loop")!;
      expect(tripped).toMatchObject({ state: "tripped", observed: "21", limit: "20", observedAs: "at_trip", trippedBy: "service:breaker", manual: false });
      expect(tripped.trippedAt).not.toBeNull();
      await expect(api.breakers.reset("spend-spike")).rejects.toMatchObject({ name: "ConflictError", code: "not_tripped" });
      await expect(api.breakers.reset("nope")).rejects.toBeInstanceOf(NotFoundError);
      const reset = await api.breakers.reset("denied-loop");
      expect(reset).toMatchObject({ name: "denied-loop", state: "ok", orgHalted: true });
      expect(reset.ledgerSeq).toBeGreaterThan(0);
      const after = await api.breakers.list();
      expect(after.breakers.find((b) => b.name === "denied-loop")?.state).toBe("ok");
      expect(after.halt.halted).toBe(true);
      await expect(api.breakers.reset("denied-loop")).rejects.toMatchObject({ code: "not_tripped" });
    });

    it("disables and enables a breaker with a reason code; repeats and a tripped rule are 409", async () => {
      const api = make();
      const off = await api.breakers.disable("spend-spike", "planned_work");
      expect(off).toMatchObject({ name: "spend-spike", state: "disabled", reason: "planned_work" });
      const row = (await api.breakers.list()).breakers.find((b) => b.name === "spend-spike")!;
      expect(row).toMatchObject({ state: "disabled", disabledReason: "planned_work" });
      expect(row.disabledBy).toMatch(/^human:/);
      expect(row.disabledAt).not.toBeNull();
      expect((await api.org.status()).halted).toBe(false); // disabling stops and resumes nothing
      await expect(api.breakers.disable("spend-spike", "other")).rejects.toMatchObject({ code: "already_disabled" });
      await expect(api.breakers.disable("nope", "other")).rejects.toBeInstanceOf(NotFoundError);
      const on = await api.breakers.enable("spend-spike");
      expect(on).toMatchObject({ name: "spend-spike", state: "ok" });
      expect((await api.breakers.list()).breakers.find((b) => b.name === "spend-spike")?.state).toBe("ok");
      await expect(api.breakers.enable("spend-spike")).rejects.toMatchObject({ code: "not_disabled" });
      await expect(api.breakers.enable("nope")).rejects.toBeInstanceOf(NotFoundError);
      const tripping = make("tripped");
      await expect(tripping.breakers.disable("denied-loop", "other")).rejects.toMatchObject({ code: "breaker_tripped" });
    });

    it("refuses every write to a caller that is not a person, and changes nothing", async () => {
      const api = make("viewer");
      const status = await api.org.status();
      expect(status.canAct).toBe(false);
      expect((await api.breakers.list()).halt.canAct).toBe(false);
      const writes: (() => Promise<unknown>)[] = [
        () => api.org.halt(),
        () => api.org.resume(),
        () => api.org.pauseRole("researcher"),
        () => api.org.resumeRole("researcher"),
        () => api.breakers.reset("denied-loop"),
        () => api.breakers.disable("spend-spike", "other"),
        () => api.breakers.enable("watch-gates"),
      ];
      for (const write of writes) {
        await expect(write()).rejects.toBeInstanceOf(NotAllowedError);
      }
      expect(await api.org.status()).toEqual(status);
      expect((await api.breakers.list()).breakers.every((b) => b.state === "ok")).toBe(true);
    });

    it("has the other fixture states: halted, poison, failing, role paused, disabled, empty", async () => {
      const halted = await make("halted").org.status();
      expect(halted).toMatchObject({ halted: true, by: "human:ann", cause: "human" });

      const poison = await make("poison").org.status();
      expect(poison).toMatchObject({
        halted: true,
        cause: "evaluation_unavailable",
        phase: "evaluate",
        errorClass: "KeyError",
        failingRule: "spend-spike",
        failedTicks: 3,
      });

      const failing = await make("failing").breakers.list();
      expect(failing.evaluation).toMatchObject({ state: "failing", consecutiveFailures: 3 });

      const rolePaused = await make("rolePaused").org.status();
      expect(rolePaused.roles.find((r) => r.role === "researcher")).toMatchObject({ paused: true, by: "human:ann", cause: "human" });

      const disabled = (await make("disabled").breakers.list()).breakers.find((b) => b.name === "watch-gates")!;
      expect(disabled).toMatchObject({ state: "disabled", disabledBy: "human:ann", disabledReason: "planned_work", dryRun: true });

      const empty = await make("empty").breakers.list();
      expect(empty.breakers).toEqual([]);
    });

    it("tells apart the error classes the screens branch on", async () => {
      const api = make();
      await api.org.halt();
      const error = await api.org.halt().catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ConflictError);
    });
  });
}
