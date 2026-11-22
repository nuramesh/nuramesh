import { describe, expect, it } from "vitest";

import { rawDone, rawLedger, rawNeedsHuman, rawRunList } from "@/test/contract";

import {
  approval,
  budgetAlert,
  budgetRow,
  chainStatus,
  entryId,
  eventText,
  ledgerEntry,
  money,
  orgStatus,
  parseEntryId,
  runDetail,
  proposalList,
  runEvent,
  runStatus,
  runTask,
  runSummary,
  sumExact,
  windowEnd,
} from "./map";
import { body } from "@/test/contract";
import type { ChainStatus } from "../types";
import type { RawApproval, RawBudget, RawBudgetAlert, RawEvent, RawProposals, RawRunDetail, RawVerify } from "./raw";

const NOW = new Date("2026-10-24T12:00:00Z");

function firstAlert(run: RawRunDetail): RawBudgetAlert {
  const alert = run.budget_alerts.at(0);
  if (alert === undefined) throw new Error("the recording has no budget alert");
  return alert;
}

/** The first entry that matches; the test fails here, with a clear message, if there is none. */
function pick(events: RawEvent[], match: (e: RawEvent) => boolean): RawEvent {
  const found = events.find(match);
  if (found === undefined) throw new Error("no such entry in the recorded ledger");
  return found;
}

describe("money from the server", () => {
  it("keeps up to six places, drops zero padding and rounds the rest half up", () => {
    expect(money({ amount: "0.00109500", currency: "USD" }).amount).toBe("0.001095");
    expect(money({ amount: "0", currency: "USD" }).amount).toBe("0");
    expect(money({ amount: "0.00210375", currency: "USD" }).amount).toBe("0.002104"); // 0.00210375: ...0375 rounds up
    expect(money({ amount: "0.0000004", currency: "USD" }).amount).toBe("0.00");
    expect(money({ amount: "2.00", currency: "USD" }).amount).toBe("2.00");
  });
  it("keeps the server's currency instead of calling everything dollars", () => {
    expect(money({ amount: "2.50", currency: "EUR" })).toEqual({ amount: "2.50", currency: "EUR" });
    expect(money({ amount: "0.00000051", currency: "EUR" }).currency).toBe("EUR");
  });
  it("adds from the exact amounts and rounds only at the end", () => {
    // each rounds to 0.000001 alone (six places), but together they are 0.0000015 -> 0.000002
    const parts = ["0.00000050", "0.00000050", "0.00000050"].map((amount) => ({ amount, currency: "USD" }));
    expect(sumExact(parts)?.amount).toBe("0.000002");
    expect(sumExact([{ amount: "1", currency: "USD" }, { amount: "1", currency: "EUR" }])).toBeNull();
  });
  it("refuses something that is not an amount", () => {
    expect(() => money({ amount: "1e3", currency: "USD" })).toThrow(RangeError);
  });
});

describe("run status words", () => {
  it("maps the server's states to the app's", () => {
    expect(runStatus("needs_human")).toBe("waiting");
    expect(runStatus("aborted")).toBe("failed");
    expect(runStatus("stopping")).toBe("stopping");
    expect(runStatus("running")).toBe("running");
    expect(runStatus("done")).toBe("done");
    expect(runStatus("something-new")).toBe("unknown"); // never active, never success
  });
});

describe("the two real runs", () => {
  it("summarises the list rows", () => {
    const [first, second] = (rawRunList as unknown as { runs: Parameters<typeof runSummary>[0][] }).runs.map(runSummary);
    expect(first).toMatchObject({ id: "run-20261023-193432-f5e5", label: "#f5e5", status: "done", ended: true, org: "demo" });
    expect(first?.tasks).toEqual({ done: 3, total: 3 });
    expect(first?.gates).toEqual({ passed: 5, failed: 0, total: 5 });
    expect(first?.spend.limit).toBeNull(); // the list doesn't carry the run budget
    expect(second).toMatchObject({ status: "waiting", ended: true });
    expect(second?.gates).toEqual({ passed: 1, failed: 1, total: 2 });
    expect(second?.tasks.done).toBe(0);
  });

  it("builds the detail of the finished run: graph, latest gate, tasks, budgets", () => {
    const d = runDetail(rawDone, NOW);
    expect(d.status).toBe("done");
    expect(d.title).toMatch(/\(\+2 more\)$/);
    expect(d.spend.limit).toEqual({ amount: "2.00", currency: "USD" }); // the run's own budget
    expect(d.elapsedSeconds).toBe(4);
    expect(d.needsYou).toBe(0);
    const ids = d.graph.nodes.map((n) => n.id);
    expect(ids).toContain("chief");
    expect(d.graph.nodes.find((n) => n.isVerifier)?.name).toBe("rhea");
    expect(d.graph.nodes.find((n) => n.isHuman)?.name).toBe("demo");
    expect(d.graph.edges.some((e) => e.kind === "approval")).toBe(true);
    expect(d.latestGate?.verdict).toBe("passed");
    expect(d.latestGate?.verifierModel).toBeNull(); // the ledger keeps no model name per gate: unknown, not guessed
    expect(d.taskList).toHaveLength(3);
    expect(d.taskList?.[0]?.gates.length).toBeGreaterThan(0);
    expect(d.ledger).toMatchObject({ chainOk: true });
    expect(d.spendByModel?.[0]?.name).toBe("azure-openai/gpt-5.4-mini");
  });

  it("the run that needed a person ends 'waiting' with a failed gate and cancelled tasks", () => {
    const d = runDetail(rawNeedsHuman, NOW);
    expect(d).toMatchObject({ status: "waiting", ended: true });
    expect(d.latestGate).toMatchObject({ gate: "tests", verdict: "failed" });
    expect(d.latestGate?.criteria.some((c) => !c.met)).toBe(true);
    expect(d.taskList?.map((t) => t.status)).toEqual(["needs_human", "cancelled", "cancelled"]);
    expect(d.graph.nodes.find((n) => n.id === "dana")?.status).toBe("waiting");
  });

  it("shows the fixed words for a stop whose reason could not be kept, and no reason text", () => {
    const stop = (payload: Record<string, unknown>): RawEvent => ({ stream: "run:r1", seq: 5, type: "run.stop_requested", actor: "human:ann", at: "2026-10-26T10:00:00Z", run_id: "r1", task_id: null, payload, cost: null, cost_basis: null, hash: "h", prev_hash: "p" });
    expect(eventText(stop({ via: "api", reason_hash: "abc" }))).toBe("Stop requested by human:ann");
    expect(eventText(stop({ via: "api", reason_withheld: "org_key_destroyed" }))).toBe("Stop requested by human:ann (reason withheld: org key destroyed)");
    expect(eventText(stop({ via: "api", reason_withheld: "org_key_missing" }))).toBe("Stop requested by human:ann (reason withheld: org key unavailable)");
    expect(eventText(stop({ via: "api", reason_withheld: "key_tombstone_unavailable" }))).toContain("org key unavailable");
  });

  it("turns ledger entries into sentences without any content", () => {
    const events = (rawLedger as unknown as { events: RawEvent[] }).events;
    expect(events.map(eventText).every((t) => t.length > 0)).toBe(true);
    const gate = pick(events, (e) => e.type === "gate.completed" && e.payload.outcome === "failed");
    expect(runEvent(gate)).toMatchObject({ category: "gates", tone: "warm", kind: "gate.completed" });
    expect(eventText(pick(events, (e) => e.type === "run.ended"))).toBe("Run ended (2 cancelled, 1 needs human)");
    const spend = pick(events, (e) => e.type === "budget.settled");
    expect(runEvent(spend).category).toBe("spend");
    expect(ledgerEntry(spend)).toMatchObject({ n: "run:run-20261023-193348-ef9e@12", label: "12", category: "spend" });
  });
});

describe("what the server does not say stays unknown", () => {
  it("no start time is not the last-updated time, and no elapsed time is not zero seconds", () => {
    const d = runDetail({ ...rawNeedsHuman, started_at: null }, NOW);
    expect(d.startedAt).toBeNull();
    expect(d.elapsedSeconds).toBeNull();
  });
  it("an unknown run state is 'unknown': not active, not offered Stop", () => {
    const d = runDetail({ ...rawNeedsHuman, status: "brand_new_state", ended_at: null }, NOW);
    expect(d.status).toBe("unknown");
  });
});

describe("ledger entry ids", () => {
  it("round-trip the stream and number", () => {
    expect(entryId("run:r1", 7)).toBe("run:r1@7");
    expect(parseEntryId("run:r1@7")).toEqual({ stream: "run:r1", seq: 7 });
    expect(parseEntryId("1043")).toBeNull();
    expect(parseEntryId("run:r1@0")).toBeNull();
    expect(parseEntryId("@5")).toBeNull();
  });
});

const budget = (over: Partial<RawBudget>): RawBudget => ({
  id: "b1",
  scope: "org",
  scope_id: "demo",
  limit: { amount: "30", currency: "USD" },
  spent: { amount: "4", currency: "USD" },
  reserved: { amount: "0", currency: "USD" },
  available: { amount: "26", currency: "USD" },
  blocked: false,
  window: "day",
  on_breach: "reset",
  window_start: "2026-10-24T00:00:00.000000Z",
  version: 3,
  ...over,
});

describe("budget windows", () => {
  it("computes when a window ends, in UTC", () => {
    expect(windowEnd("day", "2026-10-24T00:00:00.000000Z")).toBe("2026-10-25T00:00:00.000Z");
    expect(windowEnd("month", "2026-12-01T00:00:00.000000Z")).toBe("2027-01-01T00:00:00.000Z");
    expect(windowEnd("month", "2026-10-01T00:00:00.000000Z")).toBe("2026-11-01T00:00:00.000Z");
    expect(windowEnd("none", null)).toBeNull();
    expect(windowEnd("day", null)).toBeNull();
  });

  it("reads window, on_breach and the reset time into the row", () => {
    const row = budgetRow(budget({}));
    expect(row).toMatchObject({ scope: "Organization", period: "Daily (UTC)", window: "day", onBreach: "reset", status: "ok", percentUsed: 13 });
    expect(row.resetsAt).toBe("2026-10-25T00:00:00.000Z");
    expect(row.windowStart).toBe("2026-10-24T00:00:00.000000Z");
    const held = budgetRow(budget({ id: "b2", scope: "agent", scope_id: "dana", on_breach: "hold", blocked: true }));
    expect(held).toMatchObject({ scope: "Agent: dana", onBreach: "hold", status: "blocked" });
    const lifetime = budgetRow(budget({ window: "none", window_start: null, on_breach: "hold", spent: { amount: "28", currency: "USD" } }));
    expect(lifetime).toMatchObject({ period: "Lifetime", resetsAt: null, windowStart: null, status: "near" });
  });

  it("reads the real alerts: held has no reset time, a reset budget has one, from the server only", () => {
    const run = body<RawRunDetail>("windows", "run_alerts");
    const [held, reset] = run.budget_alerts.map((a) => budgetAlert(a, "run:run-demo-window"));
    expect(held).toMatchObject({ budget: "dana-daily", held: true, resetsAt: null, kind: "limit_passed", window: "day" });
    expect(held?.limit).toEqual({ amount: "0.01", currency: "USD" }); // the server sends plain text here
    expect(held?.spent).toEqual({ amount: "0.02", currency: "USD" });
    expect(reset).toMatchObject({ budget: "rhea-daily", held: false, resetsAt: "2026-10-25T00:00:00.000000Z" });
  });

  it("never computes a reset time the server did not send", () => {
    const found = body<RawRunDetail>("windows", "run_alerts").budget_alerts.find((x) => x.budget === "rhea-daily");
    if (found === undefined) throw new Error("the reset budget's alert is missing from the recording");
    const alert: RawBudgetAlert = found;
    expect(alert.resets_at).toBeTruthy();
    const without: RawBudgetAlert = { ...alert, resets_at: null };
    expect(budgetAlert(without, "s").resetsAt).toBeNull(); // window_start is there, the end is not guessed
    expect(budgetAlert({ ...alert, limit: "oops" }, "s").limit).toBeNull();
  });

  it("reads the real closed-window breach: window_closed, no reset time, the overspent window's numbers", () => {
    const run = body<RawRunDetail>("windows", "run_closed_window");
    const raw = firstAlert(run);
    expect(raw).toMatchObject({ window_closed: true, held: false, on_breach: "reset", spent: "0.03", over_by: "0.02", limit: "0.01" });
    expect("resets_at" in raw).toBe(false);
    expect(budgetAlert(raw, "run:run-closed-window")).toMatchObject({ windowClosed: true, resetsAt: null, held: false, spent: { amount: "0.03" } });
  });

  it("pins the open question: a settlement after the window ended, with nothing rolling it, has a reset time before its own event", () => {
    const run = body<RawRunDetail>("windows", "run_unrolled_window");
    const alert = budgetAlert(firstAlert(run), "run:run-unrolled-window");
    expect(alert).toMatchObject({ resetsAt: "2026-10-25T00:00:00.000000Z", held: false });
    expect(alert.windowClosed).toBeUndefined();
    const ledger = body<{ events: { seq: number; type: string; at: string }[] }>("windows", "run_unrolled_window_ledger");
    const event = ledger.events.find((e) => e.type === "budget.breached");
    expect(Date.parse(alert.resetsAt ?? "")).toBeLessThanOrEqual(Date.parse(event?.at ?? ""));
  });

  it("maps the budgets of the real windows store", () => {
    const rows = body<RawBudget[]>("windows", "budgets").map(budgetRow);
    const held = rows.find((r) => r.id === "dana-daily");
    expect(held).toMatchObject({ window: "day", onBreach: "hold", status: "blocked", scope: "Agent: dana" });
    // the day after the breaches: the reset budget started fresh by itself, the held one stays blocked
    expect(rows.find((r) => r.id === "rhea-daily")).toMatchObject({ onBreach: "reset", status: "ok", windowStart: "2026-10-25T00:00:00.000000Z", resetsAt: "2026-10-26T00:00:00.000Z" });
    expect(held?.windowStart).toBe("2026-10-25T00:00:00.000000Z");
    expect(rows.find((r) => r.id === "org-daily")).toMatchObject({ scope: "Organization", period: "Daily (UTC)", status: "ok" });
  });
});

const verify = (file: "demo" | "tamper", name: string): ChainStatus => chainStatus(body<RawVerify>(file, name), NOW);

describe("verification answers of the real server (never 'verified' unless the server says so)", () => {
  it("both real runs are anchored and verified", () => {
    const s = verify("demo", "verify_all");
    expect(s).toMatchObject({ verified: true, chainOk: true, anchorState: "anchored", entries: 73, gaps: 0 });
    expect(s.streams?.map((x) => x.anchor)).toEqual(["anchored", "anchored"]);
    expect(verify("demo", "verify_run")).toMatchObject({ verified: true, anchorState: "anchored", entries: 41 });
    expect(verify("demo", "verify_stream")).toMatchObject({ verified: true, anchorState: "anchored" }); // the flattened single-stream answer
  });

  it("without anchors configured: intact, not verified", () => {
    expect(verify("demo", "noanchors_verify_all")).toMatchObject({ verified: false, chainOk: true, anchorState: "not_configured" });
    expect(verify("demo", "noanchors_verify_stream")).toMatchObject({ verified: false, anchorState: "not_configured" });
  });

  it("one stream with anchors and one without is unanchored, not verified", () => {
    const s = verify("tamper", "partial_verify_all");
    expect(s).toMatchObject({ verified: false, chainOk: true, anchorState: "unanchored" });
    expect(s.streams?.map((x) => [x.anchor, x.unanchoredEvents])).toEqual([["anchored", 0], ["unanchored", 41]]);
    expect(verify("tamper", "partial_verify_stream")).toMatchObject({ verified: false, anchorState: "unanchored" });
  });

  it("anchors that cannot be read are not clean", () => {
    expect(verify("tamper", "unreadable_verify_all")).toMatchObject({ verified: false, anchorState: "unreadable" });
  });

  it("an edited entry breaks the hash chain: chainProblem names the code and the entry, and anchors are still 'anchored'", () => {
    const s = verify("tamper", "chain_verify_all");
    expect(s).toMatchObject({ verified: false, chainOk: false, gaps: 1 });
    expect(s.streams?.find((x) => !x.chainOk)).toMatchObject({ chainProblem: "bad_hash at entry 38", anchorProblems: [] });
  });

  it("a cut-off run keeps an intact chain but no longer matches the anchor (stream_shorter)", () => {
    const s = verify("tamper", "truncated_verify_all");
    expect(s).toMatchObject({ verified: false, chainOk: true, anchorState: "problems" });
    const bad = s.streams?.find((x) => x.anchor === "problems");
    expect(bad?.chainOk).toBe(true); // an anchor problem is not a broken chain
    expect(bad?.anchorProblems[0]).toMatchObject({ code: "stream_shorter" });
    expect(typeof bad?.anchorProblems[0]?.detail).toBe("string");
  });

  it("a rewritten entry with a recomputed hash verifies as a chain but not against the anchor (head_mismatch)", () => {
    const s = verify("tamper", "rewritten_verify_all");
    expect(s).toMatchObject({ verified: false, chainOk: true, anchorState: "problems" });
    expect(s.streams?.find((x) => x.anchor === "problems")?.anchorProblems[0]).toMatchObject({ code: "head_mismatch" });
    expect(verify("tamper", "rewritten_verify_run")).toMatchObject({ verified: false, anchorState: "problems" });
  });

  it("the server's own 'verified' flag is required", () => {
    expect(chainStatus({ ...body<RawVerify>("demo", "verify_all"), verified: false }, NOW).verified).toBe(false);
  });
});


describe("approvals (real answers)", () => {
  const pending = body<RawApproval>("approvals", "pending");
  const decided = body<RawApproval>("approvals", "decided");
  it("shows a pending request with evidence and no decision", () => {
    const a = approval(pending, null);
    expect(a).toMatchObject({ state: "pending", title: "omar wants to merge main.", requester: "omar", runLabel: "#approvals", decision: null, runSpend: null });
    expect(a.rule).toBe("rule 0 says ask");
    expect(a.evidence.map((e) => e.state)).toEqual(["ok", "wait"]);
  });
  it("a decided request names who, and keeps the reason as a hash it does not have the text of", () => {
    const a = approval(decided, null);
    expect(a.state).toBe("approved");
    expect(a.decision).toMatchObject({ by: "human:ann" });
    expect(a.decision?.ledgerEntry).toMatch(/^run:run-approvals@\d+$/);
    expect(a.decision?.reasonHash).toBe(decided.decision_reason_hash);
    expect(JSON.stringify(a.decision)).not.toContain("reason\":\"");
  });
  it("an expired request is never shown as approved (S4 AD-46), whether or not a person had answered", () => {
    const unanswered = approval({ ...pending, status: "expired" }, null);
    expect(unanswered).toMatchObject({ state: "denied", decision: null });
    expect(unanswered.evidence.map((e) => e.text)).toContain("Expired: the deadline passed before it was used");
    const unused = approval({ ...decided, status: "expired" }, null);
    expect(unused).toMatchObject({ state: "denied", decision: null });
    expect(unused.evidence.map((e) => e.state)).toEqual(["ok", "ok"]);
  });
  it("builds the effect from the action, not by cutting the sentence (an agent id containing ' wants to ')", () => {
    const a = approval({ ...pending, agent_id: "ev wants to il", action: { kind: "merge", target: "main", amount: null } }, null);
    expect(a.effect).toBe("Lets ev wants to il do this one thing, once, in this run: merge main.");
  });
  it("spend approvals name the amount", () => {
    expect(approval({ ...pending, action: { kind: "spend", target: null, amount: { amount: "25", currency: "USD" } } }, null).title).toBe("omar wants to spend $25.00.");
  });
});

describe("orgStatus: solo approvals after a removal (solo approval SA-18 item 4)", () => {
  const halt = { halted: false, by: null, at: null, cause: null, rule: null, ledger_seq: null, phase: null, error_class: null, failing_rule: null, failed_ticks: null, roles: [], can_act: true };
  const solo = { solo_now: true, limit_per_hour: 30, window_days: 30, approved: 4, approved_solo: 3, solo_share: "0.7500", approved_solo_after_removal: 2 };

  it("reads the count from `solo_approval`", () => {
    expect(orgStatus({ ...halt, solo_approval: solo }).soloAfterRemoval).toBe(2);
  });

  it("reads `solo_now`, and leaves it out for an answer without the object", () => {
    expect(orgStatus({ ...halt, solo_approval: solo }).soloNow).toBe(true);
    expect(orgStatus({ ...halt, solo_approval: { ...solo, solo_now: false } }).soloNow).toBe(false);
    expect("soloNow" in orgStatus(halt)).toBe(false);
  });

  it("shows nothing for an answer without the object (a halt or role POST) or with a value that is not a whole count", () => {
    expect(orgStatus(halt).soloAfterRemoval).toBe(0);
    for (const odd of [-1, 1.5, Number.NaN, "2", null]) {
      expect(orgStatus({ ...halt, solo_approval: { ...solo, approved_solo_after_removal: odd as number } }).soloAfterRemoval).toBe(0);
    }
  });
});

describe("runTask: the round and the definition of done (SA-19)", () => {
  const base = { id: "t1", title: "T", assignee: "a", status: "verifying", attempt: 2, max_attempts: 3, note: null, attempts: [], latest_gates: [], spend: { total: { amount: "0", currency: "USD" }, calls: 0 } };

  it("carries the submission, the criteria and the human gates with their checks (the server's recorded answer)", () => {
    // the engine's reference task: three criteria, and the human gate `deploy` checks only `approved`
    const raw = body<RawRunDetail>("solo", "run_waiting").tasks[0];
    if (raw === undefined) throw new Error("the recorded run has no task");
    expect(raw.definition_of_done?.gates.find((g) => g.kind === "human")).toEqual({ id: "deploy", kind: "human", required: true, checks: ["approved"] });
    const t = runTask(raw, "run:r1");
    expect(t.submission).toBe(1);
    expect(t.criteria?.map((c) => c.id)).toEqual(["fixed", "reviewed", "approved"]);
    expect(t.humanGates).toEqual([{ id: "deploy", checks: ["approved"] }]);
    // every gate result carries its round, so a decided gate can be told from an open one
    expect(t.gates.map((g) => [g.gate, g.attempt, g.submission])).toEqual([["rule", 1, 1], ["verify", 1, 1]]);
  });

  it("a gate without `checks` (an older server) has none: no decision can be built for it", () => {
    const t = runTask({ ...base, submission: 4, definition_of_done: { criteria: [{ id: "c1", text: "Right" }], gates: [{ id: "g1", kind: "rule", required: true, checks: ["c1"] }, { id: "review", kind: "human", required: true }] } }, "run:r");
    expect(t.humanGates).toEqual([{ id: "review", checks: [] }]);
  });

  it("leaves them out when the server does not say", () => {
    const t = runTask(base, "run:r");
    expect("submission" in t).toBe(false);
    expect("humanGates" in t).toBe(false);
  });
});

describe("proposalList", () => {
  it("maps the server's recorded proposals: the cell, the status word, the reason codes and the solo object", () => {
    const raw = body<RawProposals>("solo", "proposals_not_enough");
    const list = proposalList(raw);
    expect(list.items).toEqual([
      {
        subject: { kind: "role", id: "dev" },
        ruleKey: 2,
        role: "dev",
        kind: "write_file",
        status: "not_enough_evidence",
        reasons: ["approved_below_min", "verdicts_below_min"],
        approvedAsks: 0,
        solo: { approvedAsksSolo: 0, soloShare: null },
        evidenceHash: raw.items[0]?.evidence_hash,
      },
    ]);
    expect([list.ledgerPosition, list.asOf]).toEqual([raw.ledger_position, raw.as_of]);
    expect(proposalList(body<RawProposals>("solo", "proposals_ceiling")).items[0]).toMatchObject({ status: "ceiling", reasons: ["ceiling_none", "approved_below_min", "verdicts_below_min"] });
  });

  it("a cell that counts nothing has no approved asks (null, not 0), and the share text stays as the server wrote it", () => {
    // `never_grantable` and `no_such_rule`: the evidence is the cell alone (`core/proposals.py`)
    const list = proposalList({
      items: [{ subject: { kind: "agent", id: "dana" }, cell: { role: "writer", rule_key: 1 }, kind: null, status: "no_such_rule", reasons: ["no_such_rule"], evidence: {}, evidence_hash: "h", solo: { approved_asks_solo: 0, solo_share: null } }],
      ledger_position: 3,
      as_of: "2026-11-12T00:00:00Z",
    });
    expect(list.items[0]).toMatchObject({ approvedAsks: null, solo: { approvedAsksSolo: 0, soloShare: null }, kind: null });
    expect(list.ledgerPosition).toBe(3);
  });
});
