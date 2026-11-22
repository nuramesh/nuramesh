/**
 * The contract every `ApiClient` must keep. `runClientContract` registers the tests for a given
 * client factory: the fixture client runs them now, and the server client will run the same ones
 * against a test server loaded with the same seed data (`fixtures/seed.ts`), so the two can't
 * drift apart. The tests rely on that seed (run-128, the pending approvals, the blocked run-127).
 */
import { describe, expect, it } from "vitest";

import {
  type ApiClient,
  ConflictError,
  InvalidRequestError,
  NotAllowedError,
  NotFoundError,
} from "./client";
import { addMoney, compareMoney, usd } from "./money";

export function runClientContract(name: string, make: () => ApiClient): void {
  describe(`ApiClient contract: ${name}`, () => {
    it("lists runs and shows one in detail", async () => {
      const api = make();
      const runs = await api.runs.list();
      expect(runs.length).toBeGreaterThan(1);
      const detail = await api.runs.get(runs[0]!.id);
      expect(detail.graph.nodes.length).toBeGreaterThan(0);
      await expect(api.runs.get("nope")).rejects.toBeInstanceOf(NotFoundError);
    });

    it("pauses, stops and starts runs, each with a ledger entry", async () => {
      const api = make();
      const paused = await api.runs.pause("run-128");
      expect(paused.result.status).toBe("paused");
      expect(paused.ledgerEntry).toMatch(/^\d+$/);
      await expect(api.runs.pause("run-128")).rejects.toBeInstanceOf(NotAllowedError);
      expect((await api.runs.stop("run-128")).result.status).toBe("stopped");
      await expect(api.runs.stop("run-128")).rejects.toBeInstanceOf(NotAllowedError);
      const started = await api.runs.start({ template: "Dev team", budget: usd("10.00") });
      expect(started.result.status).toBe("running");
      await expect(api.runs.start({ template: "Dev team", budget: usd("0") })).rejects.toBeInstanceOf(
        InvalidRequestError,
      );
    });

    it("reads events after a cursor and pushes new ones to subscribers until they stop", async () => {
      const api = make();
      const first = await api.runs.events("run-128");
      expect(first.events.length).toBeGreaterThan(3);
      expect(first.events.map((e) => e.seq)).toEqual([...first.events.map((e) => e.seq)].sort((a, b) => a - b));
      expect((await api.runs.events("run-128", first.nextSeq)).events).toEqual([]);
      const seen: string[] = [];
      const stop = api.runs.subscribe("run-128", (e) => seen.push(e.kind));
      await api.runs.pause("run-128");
      stop();
      await api.runs.stop("run-128");
      expect(seen).toEqual(["run.paused"]);
      const rest = await api.runs.events("run-128", first.nextSeq);
      expect(rest.events.map((e) => e.kind)).toEqual(["run.paused", "run.cancelled"]);
    });

    it("requires a reason to decide an approval, decides once, and links the ledger entry", async () => {
      const api = make();
      const pending = (await api.approvals.list()).filter((a) => a.state === "pending");
      expect(pending.length).toBeGreaterThan(0);
      const id = pending[0]!.id;
      await expect(api.approvals.decide(id, { decision: "approve", reason: "   " })).rejects.toMatchObject({
        name: "InvalidRequestError",
        fields: [{ path: "reason" }],
      });
      expect((await api.approvals.get(id)).state).toBe("pending"); // the refusal changed nothing
      const decided = await api.approvals.decide(id, { decision: "approve", reason: "  Looks right  " });
      expect(decided.result.state).toBe("approved");
      expect(decided.result.decision).toMatchObject({ by: "You" });
      expect(JSON.stringify(decided.result.decision)).not.toContain("Looks right"); // the words are never echoed back (R1.27-H)
      expect(decided.ledgerEntry).toBe(decided.result.decision?.ledgerEntry);
      const entry = await api.ledger.get(decided.ledgerEntry!);
      expect(entry.kind).toBe("approval.decided");
      await expect(api.approvals.decide(id, { decision: "reject", reason: "changed my mind" })).rejects.toBeInstanceOf(
        NotAllowedError,
      );
      await expect(api.approvals.decide("nope", { decision: "reject", reason: "x" })).rejects.toBeInstanceOf(
        NotFoundError,
      );
    });

    it("unblocks a blocked budget only with a reason, raising the limit above what is spent", async () => {
      const api = make();
      const blocked = (await api.budgets.list()).find((b) => b.status === "blocked")!;
      expect((await api.budgets.summary()).breach?.budgetId).toBe(blocked.id);
      await expect(api.budgets.unblock(blocked.id, { reason: "" })).rejects.toBeInstanceOf(InvalidRequestError);
      await expect(
        api.budgets.unblock(blocked.id, { reason: "ok", newLimit: blocked.spent.amount }),
      ).rejects.toBeInstanceOf(InvalidRequestError);
      const done = await api.budgets.unblock(blocked.id, { reason: "Approved by finance", newLimit: "60.00" });
      expect(done.result.status).not.toBe("blocked");
      expect(compareMoney(done.result.limit, blocked.limit)).toBe(1);
      expect((await api.budgets.summary()).breach).toBeNull();
      expect((await api.ledger.get(done.ledgerEntry!)).kind).toBe("budget.unblocked");
      await expect(api.budgets.unblock(blocked.id, { reason: "again" })).rejects.toBeInstanceOf(NotAllowedError);
    });

    it("unblocks without a new limit: no longer blocked, the breach is cleared, and a second unblock is refused", async () => {
      const api = make();
      const blocked = (await api.budgets.list()).find((b) => b.status === "blocked")!;
      const done = await api.budgets.unblock(blocked.id, { reason: "Carry on" });
      expect(done.result.status).not.toBe("blocked");
      expect(done.result.limit).toEqual(blocked.limit);
      expect((await api.budgets.summary()).breach).toBeNull();
      expect((await api.budgets.list()).find((b) => b.id === blocked.id)?.status).not.toBe("blocked");
      await expect(api.budgets.unblock(blocked.id, { reason: "again" })).rejects.toBeInstanceOf(NotAllowedError);
      // the same limit isn't a raise
      const other = make();
      await expect(
        other.budgets.unblock(blocked.id, { reason: "x", newLimit: blocked.limit.amount }),
      ).rejects.toBeInstanceOf(InvalidRequestError);
    });

    it("shows every run in full detail, not just the first", async () => {
      const api = make();
      for (const run of await api.runs.list()) {
        const detail = await api.runs.get(run.id);
        expect(detail.id).toBe(run.id);
        expect(detail.graph.nodes.length).toBeGreaterThan(0);
        expect(typeof detail.needsYou).toBe("number");
        expect(detail.title.length).toBeGreaterThan(0);
      }
    });

    it("rejects, never throws, whatever the input", async () => {
      const api = make();
      const bad = { amount: "1e3", currency: "USD" } as const;
      await expect(api.runs.start({ template: "Dev team", budget: bad })).rejects.toBeInstanceOf(InvalidRequestError);
      await expect(api.config.validate({} as never)).rejects.toBeInstanceOf(Error);
      await expect(api.orgs.update({} as never, "x")).rejects.toBeInstanceOf(Error);
      await expect(
        api.policyPresets.create(null as never),
      ).rejects.toBeInstanceOf(Error);
      const pending = api.runs.get("nope");
      expect(pending).toBeInstanceOf(Promise);
      await expect(pending).rejects.toBeInstanceOf(NotFoundError);
    });

    it("links every decided approval to a ledger entry that exists", async () => {
      const api = make();
      for (const a of (await api.approvals.list()).filter((x) => x.decision !== null)) {
        const entry = await api.ledger.get(a.decision!.ledgerEntry);
        expect(entry.kind).toBe("approval.decided");
      }
    });

    it("re-checks the chain links when verifying", async () => {
      const api = make();
      const status = await api.ledger.verifyChain();
      expect(status).toMatchObject({ verified: true, gaps: 0 });
      expect(status.anchor?.commit).toBe("7d41e09");
    });

    it("moves a run on when its budget is unblocked or its approval is decided", async () => {
      const api = make();
      expect((await api.runs.get("run-127")).status).toBe("blocked");
      const before = (await api.budgets.summary()).runs;
      await api.budgets.unblock("run-127", { reason: "Carry on" });
      expect((await api.runs.get("run-127")).status).toBe("running");
      const after = (await api.budgets.summary()).runs;
      expect(after.blocked).toBe(before.blocked - 1);
      expect(after.running).toBe(before.running + 1);
      await api.approvals.decide("a2", { decision: "reject", reason: "Not this release" });
      expect((await api.runs.get("run-126")).status).toBe("stopped");
      expect((await api.runs.get("run-126")).needsYou).toBe(0);
    });

    it("counts a reason's length as sent, not trimmed", async () => {
      const api = make();
      const padded = `${"x".repeat(1000)}     `;
      await expect(api.approvals.decide("a1", { decision: "approve", reason: padded })).rejects.toBeInstanceOf(
        InvalidRequestError,
      );
      expect((await api.approvals.get("a1")).state).toBe("pending");
      expect((await api.ledger.get("988")).n).toBe("0988"); // numbers resolve with or without padding
    });

    it("describes a template and its versions", async () => {
      const api = make();
      expect((await api.templates.get("dev-team")).versions).toEqual([]);
      await api.templates.saveAs({ id: "mine", name: "Mine", description: " d " });
      const mine = await api.templates.get("mine");
      expect(mine).toMatchObject({ origin: "mine", latestVersion: 1, description: "d" });
      await expect(api.templates.get("nope")).rejects.toBeInstanceOf(NotFoundError);
      // the same description with stray spaces is not a change
      await expect(api.templates.saveAs({ id: "mine", name: "Mine", description: "d" })).rejects.toBeInstanceOf(
        InvalidRequestError,
      );
    });

    it("keeps money exact", async () => {
      const api = make();
      const models = await api.spend.byModel();
      const total = models.reduce((sum, m) => addMoney(sum, m.cost), usd("0"));
      expect(total.amount).toBe("42.10");
    });

    it("filters the ledger, shows one entry and re-verifies the chain", async () => {
      const api = make();
      const all = await api.ledger.list();
      expect(Number(all[0]!.n)).toBeGreaterThan(Number(all.at(-1)!.n)); // newest first
      expect((await api.ledger.list({ category: "gates" })).every((e) => e.category === "gates")).toBe(true);
      expect((await api.ledger.list({ search: "WEBHOOK" })).length).toBeGreaterThan(0);
      expect(await api.ledger.list({ search: "zzz-no-such" })).toEqual([]);
      const status = await api.ledger.chainStatus();
      expect(status).toMatchObject({ verified: true, gaps: 0 });
      const checked = await api.ledger.verifyChain();
      expect(Date.parse(checked.checkedAt)).toBeGreaterThan(Date.parse(status.checkedAt));
      await expect(api.ledger.get("0")).rejects.toBeInstanceOf(NotFoundError);
    });

    it("chains every new ledger entry to the one before it", async () => {
      const api = make();
      const before = (await api.ledger.list())[0]!;
      await api.runs.pause("run-128");
      const [newest, previous] = await api.ledger.list();
      expect(previous!.n).toBe(before.n);
      expect(newest!.prevHash).toBe(before.hash);
      expect(newest!.hash).not.toBe(before.hash);
    });

    it("saves templates as numbered versions, refuses an unchanged save, and starts new orgs from them", async () => {
      const api = make();
      const input = { id: "my-team", name: "My team", description: "Mine" };
      const v1 = await api.templates.saveAs(input);
      expect(v1.result.version).toBe(1);
      await expect(api.templates.saveAs(input)).rejects.toBeInstanceOf(InvalidRequestError);
      const snapshot = await api.orgs.get();
      await api.orgs.update(
        { ...snapshot.org, name: "Changed" },
        snapshot.versionHash,
      );
      const v2 = await api.templates.saveAs(input);
      expect(v2.result.version).toBe(2);
      expect((await api.templates.listVersions("my-team")).map((v) => v.version)).toEqual([1, 2]);
      const mine = (await api.templates.list()).find((t) => t.id === "my-team");
      expect(mine).toMatchObject({ origin: "mine", latestVersion: 2 });
      expect((await api.templates.use("my-team", 1)).name).not.toBe("Changed");
      expect((await api.templates.versionYaml("my-team", 1))).toContain("version: 1");
      await expect(api.templates.saveAs({ ...input, id: "Bad Id" })).rejects.toBeInstanceOf(InvalidRequestError);
      await expect(api.templates.saveAs({ ...input, id: "dev-team" })).rejects.toBeInstanceOf(InvalidRequestError);
      await expect(api.templates.use("nope")).rejects.toBeInstanceOf(NotFoundError);
    });

    it("refuses a stale organization edit and an invalid one", async () => {
      const api = make();
      const snapshot = await api.orgs.get();
      const edited = await api.orgs.update({ ...snapshot.org, name: "Renamed" }, snapshot.versionHash);
      expect(edited.result.org.name).toBe("Renamed");
      expect(edited.result.versionHash).not.toBe(snapshot.versionHash);
      await expect(api.orgs.update(snapshot.org, snapshot.versionHash)).rejects.toBeInstanceOf(ConflictError);
      const broken = { ...edited.result.org, agents: [] };
      await expect(api.orgs.update(broken, edited.result.versionHash)).rejects.toBeInstanceOf(InvalidRequestError);
      expect((await api.config.validate(broken)).valid).toBe(false);
      expect((await api.config.validate(edited.result.org)).valid).toBe(true);
    });

    it("edits presets, says which roles they affect, and records it", async () => {
      const api = make();
      const before = await api.policyPresets.list();
      const preset = before.policy.find((p) => p.usedByRoles.length > 0)!;
      const edited = await api.policyPresets.update(preset.id, { rules: [] });
      expect(edited.result.usedByRoles).toEqual(preset.usedByRoles);
      expect((await api.ledger.get(edited.ledgerEntry!)).fields.some((f) => f.value.includes(preset.usedByRoles[0]!))).toBe(true);
      await expect(api.policyPresets.update(preset.id, { name: "  " })).rejects.toBeInstanceOf(InvalidRequestError);
      await expect(api.policyPresets.update("nope", {})).rejects.toBeInstanceOf(NotFoundError);
      const created = await api.policyPresets.create({ id: "mine", name: "Mine", description: "", rules: [] });
      expect(created.result.usedByRoles).toEqual([]);
      await expect(
        api.policyPresets.create({ id: "mine", name: "Again", description: "", rules: [] }),
      ).rejects.toBeInstanceOf(InvalidRequestError);
      const snapshot = await api.orgs.get();
      const role = snapshot.org.roles[0]!.id;
      const assigned = await api.policyPresets.assign(role, "mine", "policy");
      expect(assigned.result.policy.find((p) => p.id === "mine")?.usedByRoles).toEqual([role]);
      await expect(api.policyPresets.assign("ghost", "mine", "policy")).rejects.toBeInstanceOf(NotFoundError);
      await expect(api.policyPresets.assign(role, "ghost", "policy")).rejects.toBeInstanceOf(NotFoundError);
    });

    it("gives each client its own state", async () => {
      const a = make();
      const b = make();
      await a.runs.pause("run-128");
      expect((await b.runs.get("run-128")).status).not.toBe("paused");
    });

    it("says where its data comes from", () => {
      expect(["fixtures", "server"]).toContain(make().source);
    });
  });
}
