import { describe, expect, it } from "vitest";

import { NotAvailableError, NotFoundError } from "../client";
import { createFixtureClient } from "../fixture-client";
import { taskAuditF1 } from "../fixtures/task-audit";
import { taskAudit } from "./map";
import type { RawTaskAudit } from "./raw";

describe("taskAudit mapper (M7, M8, M12)", () => {
  it("maps the S1 body to camel case with exact money text and no content fields (M7)", () => {
    const a = taskAudit(taskAuditF1());
    expect(a.spend.total).toEqual({ amount: "0.0600", currency: "USD" });
    expect(a.spendByAttempt[2]?.unknownCostCalls).toBe(1);
    expect(a.spendByAttempt[0]?.total).toEqual({ amount: "0", currency: "USD" });
    expect(a.rework[0]?.feedbackHash).toBe("a".repeat(64));
    expect(a.chain).toEqual({ ok: true, headSeq: 24, head: "f".repeat(64) });
    expect(a.entries).toHaveLength(21);
    const text = JSON.stringify(a);
    expect(text).not.toMatch(/"(text|output)"\s*:\s*"(?!First|Second)/);
  });

  it("drops a key it was not written to show (M8) and a note on a decision row (M12)", () => {
    const raw = { ...taskAuditF1(), surprise: "SHOULD-NOT-SHOW" } as RawTaskAudit;
    raw.decisions = [
      { ledger_seq: 4, at: "t", actor: "system", kind: "engine_incident", note_present: true, note: "NOTE-TEXT-FIXTURE", reason: "R" },
    ];
    const a = taskAudit(raw);
    expect(JSON.stringify(a)).not.toContain("SHOULD-NOT-SHOW");
    expect(JSON.stringify(a)).not.toContain("NOTE-TEXT-FIXTURE");
    expect(a.decisions[0]?.fields).toEqual({ note_present: true });
  });

  it("the fixture client serves the same shape and rejects an unknown task", async () => {
    const api = createFixtureClient();
    const a = await api.runs.taskAudit("r1", "t1");
    expect(a.task.title).toBe("Draft summary");
    expect(a.attempts.map((x) => [x.attempt, x.submission])).toEqual([[1, 1], [2, 2]]);
    await expect(api.runs.taskAudit("r1", "nope")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("the fixture client serves every task of run-128 and the unavailable state rejects", async () => {
    const api = createFixtureClient();
    for (const id of ["t-plan", "t-retries", "t-retest"]) {
      const a = await api.runs.taskAudit("run-128", id);
      expect(a.task.id).toBe(id);
      expect(a.entries.every((e) => e.taskId === id)).toBe(true);
    }
    const rework = await api.runs.taskAudit("run-128", "t-retries");
    expect(rework.spend.failedCallsNoCost).toBe(1);
    expect(rework.decisions[0]?.fields).toMatchObject({ note_present: true });
    expect(JSON.stringify(rework)).not.toContain("NOTE-TEXT-FIXTURE");
    await expect(api.runs.taskAudit("run-128", "nope")).rejects.toBeInstanceOf(NotFoundError);
    await expect(createFixtureClient({ taskAuditUnavailable: true }).runs.taskAudit("r1", "t1")).rejects.toBeInstanceOf(NotAvailableError);
  });
});
