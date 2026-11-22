/**
 * The server's audit answer for fixture F1 of spec/r127 (run `r1`, task `t1`): 21 entries, a gate
 * that failed once, three calls (one of unknown price), one approval. The fixture client maps it
 * with the same mapper as the server client, so both clients return the same shapes.
 */
import type { RawEvent, RawTaskAudit } from "../server/raw";

const usd = (amount: string) => ({ amount, currency: "USD" });
const H = (c: string) => c.repeat(64);

type Row = [type: string, actor: string, payload: Record<string, unknown>, cost?: string];

const move = (from: string, to: string, before: number, after: number, extra: Record<string, unknown> = {}): Record<string, unknown> => ({
  from, to, attempt_before: before, attempt_after: after, ...extra,
});

const ROWS: Row[] = [
  ["task.created", "system", { title: "Draft summary" }],
  ["task.transitioned", "system", move("proposed", "assigned", 0, 0)],
  ["task.transitioned", "system", move("assigned", "in_progress", 0, 1)],
  ["policy.decided", "system", { agent: "writer", decision: "allow", action: "call_model" }],
  ["budget.reserved", "system", { agent: "writer", budget: "b1" }, "0.0400"],
  ["model.called", "system", { agent: "writer", model: "m", outcome: "ok", latency_ms: 40 }, "0.0400"],
  ["budget.settled", "system", { agent: "writer", budget: "b1" }, "0.0400"],
  ["task.transitioned", "system", move("in_progress", "submitted", 1, 1)],
  ["task.transitioned", "system", move("submitted", "verifying", 1, 1)],
  ["gate.completed", "system", { gate: "g-rule", outcome: "passed", attempt: 1, submission: 1, checked_by: "gate:rule" }],
  ["model.called", "system", { agent: "verifier", model: "m", outcome: "ok", latency_ms: 20 }, "0.0200"],
  ["gate.completed", "system", { gate: "g-ver", outcome: "failed", attempt: 1, submission: 1, checked_by: "agent:verifier" }],
  ["task.transitioned", "system", move("verifying", "in_progress", 1, 2, { submission: 1, max_attempts: 3, reason: "gate failed: g-ver", feedback_hash: H("a") })],
  ["model.called", "system", { agent: "writer", model: "m", outcome: "ok", latency_ms: 30 }],
  ["task.transitioned", "system", move("in_progress", "submitted", 2, 2)],
  ["task.transitioned", "system", move("submitted", "verifying", 2, 2)],
  ["approval.requested", "agent:writer", { approval: "a1", agent: "writer" }],
  ["approval.decided", "human:u1", { approval: "a1", status: "approved" }],
  ["gate.completed", "system", { gate: "g-rule", outcome: "passed", attempt: 2, submission: 2, checked_by: "gate:rule" }],
  ["gate.completed", "system", { gate: "g-ver", outcome: "passed", attempt: 2, submission: 2, checked_by: "agent:verifier" }],
  ["task.transitioned", "system", move("verifying", "done", 2, 2, { submission: 2 })],
];

const entry = (row: Row, i: number): RawEvent => ({
  stream: "run:r1",
  seq: i + 2,
  type: row[0],
  actor: row[1],
  at: `2026-10-25T10:00:${String(i + 2).padStart(2, "0")}Z`,
  run_id: "r1",
  task_id: "t1",
  payload: row[2],
  cost: row[3] === undefined ? null : usd(row[3]),
  // a served call with no cost is a call of unknown cost: the server records `cost_basis: unknown` for it
  cost_basis: row[3] !== undefined ? "actual" : row[0] === "model.called" && row[2].outcome === "ok" ? "unknown" : null,
  hash: H("1"),
  prev_hash: H("0"),
});

const verdicts = (second: string) => [
  { criterion: "c1", text: "First thing", verdict: "met" },
  { criterion: "c2", text: "Second thing", verdict: second },
];
const gate = (id: string, kind: string, outcome: string, by: string, seq: number, v: ReturnType<typeof verdicts>) => ({
  gate: id, kind, required: true, outcome, checked_by: by, ledger_seq: seq, at: "2026-10-25T10:00:00Z", verdicts: v,
});

const none = { calls: 0, unknown_cost_calls: 0, price_unknown_calls: 0, other_currency_calls: 0, failed_calls_no_cost: 0 };

export function taskAuditF1(): RawTaskAudit {
  return {
    run_id: "r1",
    task: { id: "t1", title: "Draft summary", assignee: "agent:writer", status: "done", attempt: 2, max_attempts: 3, submission: 2 },
    chain: { ok: true, head_seq: 24, head: H("f") },
    truncated: false,
    total_entries: 21,
    entries: ROWS.map(entry),
    attempts: [
      { attempt: 1, submission: 1, moved_to: "in_progress", reason: "gate failed: g-ver", reason_hash: null, gates: [gate("g-rule", "rule", "passed", "gate:rule", 11, []), gate("g-ver", "verifier", "failed", "agent:verifier", 13, verdicts("not_met"))] },
      { attempt: 2, submission: 2, moved_to: "done", reason: null, reason_hash: null, gates: [gate("g-rule", "rule", "passed", "gate:rule", 20, []), gate("g-ver", "verifier", "passed", "agent:verifier", 21, verdicts("met"))] },
    ],
    spend: { total: usd("0.0600"), calls: 3, unknown_cost_calls: 1, price_unknown_calls: 1, cost_unknown_calls: 1, at_least: usd("0.0600"), other_currency_calls: 0, failed_calls_no_cost: 0 },
    spend_by_attempt: [
      { attempt: 0, total: usd("0"), ...none },
      { attempt: 1, total: usd("0.0600"), ...none, calls: 2 },
      { attempt: 2, total: usd("0"), ...none, calls: 1, unknown_cost_calls: 1, price_unknown_calls: 1, cost_unknown_calls: 1, at_least: usd("0") },
    ],
    approvals: [{ id: "a1", agent_id: "writer", asked_because: "", status: "approved", decided_by: "human:u1", decided_at: "2026-10-25T10:00:19Z", decision_reason_hash: H("b"), reason_available: true }],
    decisions: [],
    rework: [{ ledger_seq: 14, at: "2026-10-25T10:00:14Z", from: "verifying", to: "in_progress", attempt_before: 1, attempt_after: 2, max_attempts: 3, by: "gate", reason: "gate failed: g-ver", reason_hash: null, feedback_hash: H("a"), actor: "system" }],
  };
}

// ---- variants for the other states of the page (spec fixtures F2, F3, F5, F6, F8, F9, F11) ----

const zero = { amount: "0", currency: "USD" };
const sp = (total: string, extra: Partial<RawTaskAudit["spend"]> = {}): RawTaskAudit["spend"] => ({ total: usd(total), ...none, ...extra });

/** A bare audit of one task: the given rows as entries, everything derived empty unless `over` says. */
function bare(over: Partial<RawTaskAudit> & { rows?: Row[]; taskId?: string; status?: string; attempt?: number; maxAttempts?: number } = {}): RawTaskAudit {
  const { rows = [["task.created", "system", { title: "Draft summary" }]], taskId = "t1", status = "done", attempt = 0, maxAttempts = 3, ...rest } = over;
  return {
    run_id: "r1",
    task: { id: taskId, title: "Draft summary", assignee: "agent:writer", status, attempt, max_attempts: maxAttempts, submission: 0 },
    chain: { ok: true, head_seq: rows.length + 1, head: H("f") },
    truncated: false,
    total_entries: rows.length,
    entries: rows.map(entry).map((e) => ({ ...e, task_id: taskId })),
    attempts: [],
    spend: sp("0"),
    spend_by_attempt: [{ attempt: 0, total: zero, ...none }],
    approvals: [],
    decisions: [],
    rework: [],
    ...rest,
  };
}

/** F2: a call before work started, one in attempt 1 and one still running in attempt 2 (no gate round yet). */
export function taskAuditF2(): RawTaskAudit {
  return bare({
    status: "in_progress",
    attempt: 2,
    rows: [["task.created", "system", { title: "Draft summary" }], ["model.called", "system", { agent: "writer", model: "m", outcome: "ok" }, "0.0100"]],
    attempts: [{ attempt: 1, submission: 1, moved_to: "in_progress", reason: null, reason_hash: null, gates: [gate("g1", "rule", "failed", "gate:rule", 10, [])] }],
    spend: sp("0.0450", { calls: 3 }),
    spend_by_attempt: [
      { attempt: 0, total: usd("0.0100"), ...none, calls: 1 },
      { attempt: 1, total: usd("0.0300"), ...none, calls: 1 },
      { attempt: 2, total: usd("0.0050"), ...none, calls: 1 },
    ],
  });
}

/** F3: two submissions in one attempt, two calls: one spend line for the attempt. */
export function taskAuditF3(): RawTaskAudit {
  return bare({
    attempt: 1,
    attempts: [
      { attempt: 1, submission: 1, moved_to: "needs_human", reason: "required gate failed", reason_hash: null, gates: [gate("g1", "rule", "errored", "gate:rule", 8, [])] },
      { attempt: 1, submission: 2, moved_to: "done", reason: null, reason_hash: null, gates: [gate("g1", "rule", "passed", "gate:rule", 15, [])] },
    ],
    spend: sp("0.0300", { calls: 2 }),
    spend_by_attempt: [
      { attempt: 0, total: zero, ...none },
      { attempt: 1, total: usd("0.0300"), ...none, calls: 2 },
    ],
  });
}

/** F5: a person sent the task back; their text is not in the answer at all. */
export function taskAuditF5(): RawTaskAudit {
  return bare({
    taskId: "t5",
    status: "assigned",
    attempt: 1,
    maxAttempts: 2,
    rows: [["task.created", "system", { title: "Draft summary" }], ["task.transitioned", "human:u1", move("needs_human", "assigned", 1, 1, { max_attempts: 2, reason: "please try again with the new data" })]],
    rework: [{ ledger_seq: 10, at: "2026-10-25T10:00:10Z", from: "needs_human", to: "assigned", attempt_before: 1, attempt_after: 1, max_attempts: 2, by: "person", reason: null, reason_hash: null, feedback_hash: null, actor: "human:u1" }],
    decisions: [{ ledger_seq: 8, at: "2026-10-25T10:00:08Z", actor: "human:u1", kind: "human_decision", gate: "g-human", attempt: 1, submission: 1, outcome: "rejected", reason_hash: H("c") }],
  });
}

/** F6: eight decision rows, one of each kind (and one without a kind); the incident carries `note_present`, never the note. */
export function taskAuditF6(): RawTaskAudit {
  const at = "2026-10-25T10:00:00Z";
  const d = (ledger_seq: number, actor: string, kind: string, more: Record<string, unknown> = {}) => ({ ledger_seq, at, actor, kind, ...more });
  return bare({
    status: "needs_human",
    decisions: [
      d(3, "human:u2", "human_decision_refused", { attempt: 1, why: "input_expired" }),
      d(4, "system", "engine_incident", { task_status: "needs_human", human_required: true, note_present: true, note: "NOTE-TEXT-FIXTURE" }),
      d(5, "system", "step_restarted", { step: "verify", attempt: 1, epoch: 2 }),
      d(6, "service:cli", "scripted_approval", { gate: "g1", approver: "human:u3", human_review: false }),
      d(7, "system", "unknown"),
      d(8, "system", "human_wait_warning", { attempt: 1, days_left: 7 }),
      d(9, "system", "human_wait_expired", { attempt: 1, days_left: 0 }),
      d(10, "system", "dependency_waiting", { waiting_on: "t-wait", attempt: 1 }),
    ],
  });
}

/** F8: 1001 entries, the first 1000 listed, every number from all of them. */
export function taskAuditF8(): RawTaskAudit {
  const rows: Row[] = Array.from({ length: 1000 }, () => ["model.called", "system", { agent: "writer", model: "m", outcome: "ok" }, "0.0010"] as Row);
  return bare({ taskId: "t4", rows, truncated: true, total_entries: 1001, spend: sp("1.0000", { calls: 1000 }), spend_by_attempt: [{ attempt: 0, total: usd("1.0000"), ...none, calls: 1000 }] });
}

/** F9: a task with only `task.created`. */
export function taskAuditF9(): RawTaskAudit {
  return bare({ taskId: "t3", status: "proposed" });
}

/** F11: one priced call and two failed ones without a cost (one with no outcome at all). */
export function taskAuditF11(): RawTaskAudit {
  return bare({
    taskId: "t12",
    attempt: 1,
    rows: [
      ["task.created", "system", { title: "Draft summary" }],
      ["model.called", "system", { agent: "writer", model: "m", outcome: "ok" }, "0.0100"],
      ["model.called", "system", { agent: "writer", model: "m", outcome: "error" }],
      ["model.called", "system", { agent: "writer", model: "m" }],
    ],
    spend: sp("0.0100", { calls: 3, unknown_cost_calls: 2, failed_calls_no_cost: 2 }),
    spend_by_attempt: [{ attempt: 0, total: zero, ...none }, { attempt: 1, total: usd("0.0100"), ...none, calls: 3, unknown_cost_calls: 2, failed_calls_no_cost: 2 }],
  });
}

/** F1 with the chain reported broken (the stored payload of entry 7 was changed); everything else is still given. */
export function taskAuditBroken(): RawTaskAudit {
  return { ...taskAuditF1(), chain: { ok: false, head_seq: 24, head: H("f") } };
}

// ---- the seeded run `run-128` (fixture client): three tasks, so every "Audit trail" link leads somewhere ----

/** The ids of the seeded run's tasks, in the order the run page lists them. */
export const RUN_128_TASKS = ["t-plan", "t-retries", "t-retest"] as const;

const inRun128 = (a: RawTaskAudit, taskId: string, title: string): RawTaskAudit => ({
  ...a,
  run_id: "run-128",
  task: { ...a.task, id: taskId, title },
  entries: a.entries.map((e) => ({ ...e, stream: "run:run-128", run_id: "run-128", task_id: taskId })),
});

/**
 * The audit of one task of `run-128`, or null for an id the run does not have. `t-retries` is the rework
 * task: a gate that failed, a call of unknown price, a failed call, an approval with a decision and an
 * engine incident whose note text must never reach the page.
 */
export function taskAuditRun128(taskId: string): RawTaskAudit | null {
  if (taskId === "t-plan") return inRun128(bare({ taskId, status: "done", attempt: 1, maxAttempts: 3 }), taskId, "Plan the work");
  if (taskId === "t-retest") {
    return inRun128(
      bare({
        taskId,
        status: "in_progress",
        attempt: 1,
        rows: [["task.created", "system", { title: "Re-run the full suite" }], ["model.called", "system", { agent: "tester", model: "m", outcome: "ok" }, "0.0100"]],
        spend: sp("0.0100", { calls: 1 }),
        spend_by_attempt: [{ attempt: 0, total: usd("0.0100"), ...none, calls: 1 }],
      }),
      taskId,
      "Re-run the full suite",
    );
  }
  if (taskId !== "t-retries") return null;
  const rows: Row[] = [...ROWS, ["model.called", "system", { agent: "writer", model: "m", outcome: "error" }]];
  const base = taskAuditF1();
  const failed = { calls: 2, unknown_cost_calls: 2, price_unknown_calls: 1, failed_calls_no_cost: 1 };
  return inRun128(
    {
      ...base,
      chain: { ok: true, head_seq: 25, head: H("f") },
      total_entries: rows.length,
      entries: rows.map(entry),
      spend: { ...base.spend, calls: 4, unknown_cost_calls: 2, failed_calls_no_cost: 1 },
      spend_by_attempt: [
        { attempt: 0, total: usd("0"), ...none },
        { attempt: 1, total: usd("0.0600"), ...none, calls: 2 },
        { attempt: 2, total: usd("0"), ...none, ...failed },
      ],
      decisions: [{ ledger_seq: 24, at: "2026-10-25T10:00:24Z", actor: "system", kind: "engine_incident", task_status: "in_progress", human_required: false, note_present: true, note: "NOTE-TEXT-FIXTURE" }],
    },
    taskId,
    "Add retries to the payment webhook",
  );
}
