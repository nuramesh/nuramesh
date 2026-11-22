import { describe, expect, it } from "vitest";

import { formatMoneyExact, usd } from "@/api/money";
import { answer, body } from "@/test/contract";

import { taskAudit } from "./map";
import type { RawTaskAudit } from "./raw";

/**
 * R1.27: the audit mapper on the REAL recorded answers of `GET /v1/runs/{id}/tasks/{id}/audit`
 * (the first real run, `scripts/app/gen_contract.py`), not on the hand-written fixtures of
 * `task-audit.test.ts`. If the server renames or retypes a field the mapper reads, this fails.
 */
const done = body<RawTaskAudit>("demo", "task_audit_done");
const sentBack = body<RawTaskAudit>("demo", "task_audit_sent_back");

describe("taskAudit on the recorded real answers", () => {
  it("a task that passed: three gates in one attempt, 21 entries, the chain verified, no approvals", () => {
    const a = taskAudit(done);
    expect(a.task).toMatchObject({ title: "Fix the failing add() test in calc.py", status: "done", attempt: 1, maxAttempts: 2 });
    expect(a.chain).toEqual({ ok: true, headSeq: 41, head: done.chain.head });
    expect(a.totalEntries).toBe(21);
    expect(a.entries).toHaveLength(21);
    expect(a.truncated).toBe(false);
    expect(a.attempts).toHaveLength(1);
    expect(a.attempts[0]?.gates.map((g) => [g.gate, g.outcome])).toEqual([
      ["has-fix", "passed"],
      ["tests", "passed"],
      ["review", "passed"],
    ]);
    expect(a.spend.total).toEqual({ amount: "0.00109650", currency: "USD" });
    expect(a.approvals).toEqual([]);
    expect(a.decisions).toEqual([]);
    expect(a.rework).toEqual([]);
  });

  it("a task sent back: two attempts, one rework round by a gate, spend split by attempt", () => {
    const a = taskAudit(sentBack);
    expect(a.task.status).toBe("needs_human");
    expect(a.task.attempt).toBe(2);
    expect(a.totalEntries).toBe(26);
    expect(a.attempts.map((x) => x.attempt)).toEqual([1, 2]);
    expect(a.rework).toHaveLength(1);
    expect(a.rework[0]).toMatchObject({ from: "verifying", to: "in_progress", by: "gate" });
    expect(a.spend.total).toEqual({ amount: "0.00109500", currency: "USD" });
    expect(a.spendByAttempt.map((s) => [s.attempt, s.total.amount])).toEqual([
      [0, "0"],
      [1, "0.00052050"],
      [2, "0.00057450"],
    ]);
  });

  it("the audit keeps the server's 8 places exactly (owner, R1.27): no rounding, so the attempts add up to the total", () => {
    expect(done.spend.total.amount).toBe("0.00109650");
    expect(taskAudit(done).spend.total.amount).toBe("0.00109650");
    expect(taskAudit(done).entries.filter((e) => e.cost !== null).map((e) => e.cost?.amount)).toContain("0.00222975");
    const sb = taskAudit(sentBack);
    expect(sb.spendByAttempt.slice(1).reduce((n, s) => n + BigInt(s.total.amount.replace("0.", "")), 0n)).toBe(BigInt(sb.spend.total.amount.replace("0.", "")));
  });

  it("the audit page text shows all 8 places", () => {
    expect(formatMoneyExact(taskAudit(done).spend.total)).toBe("$0.00109650");
    expect(formatMoneyExact(usd("1234.5"))).toBe("$1,234.50000000");
    expect(formatMoneyExact(usd("0"))).toBe("$0.00000000");
    expect(() => formatMoneyExact({ amount: "0.1e-3", currency: "USD" })).toThrow(RangeError);
  });

  it("the totals add up: the per-attempt spends sum to the task total", () => {
    for (const raw of [done, sentBack]) {
      const cents = (m: string): bigint => BigInt(m.replace(".", "").padEnd(m.includes(".") ? 0 : 0, ""));
      const dp = (m: string): number => (m.split(".")[1] ?? "").length;
      const width = Math.max(dp(raw.spend.total.amount), ...raw.spend_by_attempt.map((s) => dp(s.total.amount)));
      const scale = (m: string): bigint => cents(m) * 10n ** BigInt(width - dp(m));
      const sum = raw.spend_by_attempt.reduce((acc, s) => acc + scale(s.total.amount), 0n);
      expect(sum).toBe(scale(raw.spend.total.amount));
    }
  });

  it("the missing task is the server's plain 404, which the client turns into not-found", () => {
    const missing = answer<{ status: number; title: string; detail: string }>("demo", "task_audit_missing_task");
    expect(missing.status).toBe(404);
    expect(missing.body).toMatchObject({ status: 404, detail: "no such task" });
  });

  it("the recorded body has no text of any content, only hashes (ND52)", () => {
    for (const raw of [done, sentBack]) {
      expect(JSON.stringify(taskAudit(raw))).not.toMatch(/"(text|output|feedback)":"[^"]{200}/);
    }
  });
});
