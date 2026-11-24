/**
 * Pure mappers from the server's JSON (the API notes) to the shapes the screens use. Nothing here
 * does I/O, so each rule is unit-tested on its own. A rule: when the server doesn't say something,
 * the result says "unknown" (null) and never a guess.
 */
import { actionPhrase, approvalSentence, escapeInert } from "@/approvals/sentence";

import { addMoney, fromMicros, toMicros, usd } from "../money";
import type {
  AuditDecision,
  AuditSpend,
  TaskAudit,
  AnchorState,
  Approval,
  BreachMode,
  BreakerAction,
  BreakerRow,
  BreakerSignal,
  BreakerState,
  BreakersView,
  BudgetAlert,
  BudgetRow,
  BudgetWindow,
  ChainStatus,
  DisableReason,
  EvaluationState,
  EventCategory,
  GateOutcome,
  GateResultCard,
  GraphEdge,
  GraphNode,
  LedgerCategory,
  LedgerEntry,
  Money,
  NodeStatus,
  OrgStatus,
  ProposalList,
  RunDetail,
  RunEvent,
  RunStatus,
  RunSummary,
  RunTask,
  StreamCheck,
  Tone,
} from "../types";
import type {
  RawAnchorInfo,
  RawApproval,
  RawBudget,
  RawBreaker,
  RawBreakers,
  RawBudgetAlert,
  RawAuditSpend,
  RawEvent,
  RawGate,
  RawHalt,
  RawProposals,
  RawMoney,
  RawRunDetail,
  RawRunSummary,
  RawTaskAudit,
  RawVerify,
} from "./raw";

// ---- money and ids ----------------------------------------------------------------------------

/**
 * The server's decimal text as app money. The ledger keeps up to 8 places for reservations; the
 * app's money has 6, so extra places are dropped when they are zeros and rounded half-up
 * otherwise (display only; the ledger keeps the exact value).
 */
export function money(raw: RawMoney | null | undefined): Money {
  if (raw === null || raw === undefined) return usd("0");
  const currency = raw.currency;
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(raw.amount);
  if (match === null) throw new RangeError(`not a money amount: "${raw.amount}"`);
  const [, sign = "", whole = "0", fraction = ""] = match;
  if (fraction.length <= 6) return { ...usd(`${sign}${whole}${fraction === "" ? "" : `.${fraction}`}`), currency };
  const kept = BigInt(whole) * 1_000_000n + BigInt(fraction.slice(0, 6));
  const rounded = fraction.charCodeAt(6) >= 53 ? kept + 1n : kept; // '5' is code 53
  return fromMicros(sign === "-" ? -rounded : rounded, currency);
}

/**
 * The server's decimal text kept as it is, all places (the audit view shows the ledger's full 8,
 * design decision on R1.27). Validated, never rounded, never a float.
 */
export function exactMoney(raw: RawMoney | null | undefined): Money {
  if (raw === null || raw === undefined) return usd("0");
  if (!/^-?\d+(?:\.\d+)?$/.test(raw.amount)) throw new RangeError(`not a money amount: "${raw.amount}"`);
  return { amount: raw.amount, currency: raw.currency };
}

export const moneyOrNull = (raw: RawMoney | null | undefined): Money | null =>
  raw === null || raw === undefined ? null : money(raw);

/**
 * Exact sum of server amounts in one currency (no rounding until the end: the ledger keeps 8
 * places, the app shows 6). Null when the amounts are in different currencies.
 */
export function sumExact(values: RawMoney[], currency = "USD"): Money | null {
  if (values.some((v) => v.currency !== currency)) return null;
  const parsed = values.map((v) => {
    const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(v.amount);
    if (m === null) throw new RangeError(`not a money amount: "${v.amount}"`);
    return { negative: m[1] === "-", whole: m[2] ?? "0", fraction: m[3] ?? "" };
  });
  const places = Math.max(0, ...parsed.map((p) => p.fraction.length));
  const total = parsed.reduce((sum, p) => {
    const n = BigInt(p.whole) * 10n ** BigInt(places) + BigInt(p.fraction.padEnd(places, "0") || "0");
    return sum + (p.negative ? -n : n);
  }, 0n);
  return divideExact(total, 1n, places, currency);
}

/** `total` (scaled by 10^places) divided by `by`, rounded half up to the app's six places. */
export function divideExact(total: bigint, by: bigint, places: number, currency: string): Money {
  const scale = 10n ** BigInt(places);
  const micros6 = (total * 1_000_000n * 2n + by * scale) / (by * scale * 2n); // half up, for non-negative totals
  return fromMicros(micros6, currency);
}

/** "ef9e": the last part of a run id, enough to tell runs apart in a list. */
export function shortRunId(runId: string): string {
  const last = runId.split("-").pop() ?? runId;
  return last.length > 0 ? last : runId;
}

export const runLabel = (runId: string): string => `#${shortRunId(runId)}`;

/** The id a ledger entry has in the app's URLs: its stream and its number in that stream. */
export const entryId = (stream: string, seq: number): string => `${stream}@${String(seq)}`;

export function parseEntryId(id: string): { stream: string; seq: number } | null {
  const at = id.lastIndexOf("@");
  if (at <= 0) return null;
  const seq = Number(id.slice(at + 1));
  if (!Number.isInteger(seq) || seq < 1) return null;
  return { stream: id.slice(0, at), seq };
}

// ---- runs -------------------------------------------------------------------------------------

/**
 * The server's run states in the app's words. `needs_human` is a run that ended with a task
 * waiting for a person ("waiting"); `aborted` is the engine failing ("failed"); `stopping` is a
 * stop that was asked for and not yet honoured.
 */
export function runStatus(status: string): RunStatus {
  switch (status) {
    case "running":
    case "stopping":
    case "stopped":
    case "done":
    case "failed":
      return status;
    case "needs_human":
      return "waiting";
    case "aborted":
      return "failed";
    default:
      return "unknown"; // a state this app doesn't know: never shown as active or as success
  }
}

export function runSummary(raw: RawRunSummary): RunSummary {
  return {
    id: raw.run_id,
    label: runLabel(raw.run_id),
    title: `Run ${shortRunId(raw.run_id)}`,
    status: runStatus(raw.status),
    template: "",
    startedAt: raw.started_at,
    startedBy: raw.started_by ?? "unknown",
    tasks: { done: raw.tasks.by_status.done ?? 0, total: raw.tasks.total },
    gates: { passed: raw.gates.passed, failed: raw.gates.failed + raw.gates.error, total: raw.gates.total },
    spend: { spent: money(raw.spend.total), limit: null },
    ended: raw.ended_at !== null,
    stopRequestedBy: raw.stop?.requested_by ?? null,
    ...(raw.stop?.stop_reason_withheld ? { stopReasonWithheld: raw.stop.stop_reason_withheld } : {}),
    ...(raw.org === null ? {} : { org: raw.org }),
  };
}

const ACTIVE_TASK = new Set(["assigned", "in_progress", "verifying"]);
const WAITING_TASK = new Set(["needs_human", "blocked"]);

function gateOutcome(gate: RawGate, attempt: number, stream: string, submission?: number): GateOutcome {
  return {
    ...(typeof submission === "number" ? { submission } : {}),
    gate: gate.gate,
    kind: gate.kind,
    required: gate.required,
    outcome: gate.outcome,
    checkedBy: gate.checked_by,
    attempt,
    ledgerEntry: entryId(stream, gate.ledger_seq),
    verdicts: gate.verdicts.map((v) => ({ criterion: v.criterion, text: v.text ?? v.criterion, met: v.verdict === "met" })),
  };
}

function nodeStatusOf(statuses: string[], ended: boolean): { status: NodeStatus; text: string } {
  if (statuses.length === 0) return { status: "idle", text: "no tasks" };
  const done = statuses.filter((s) => s === "done").length;
  const count = `${String(done)} of ${String(statuses.length)} done`;
  if (!ended && statuses.some((s) => ACTIVE_TASK.has(s))) return { status: "running", text: `working · ${count}` };
  if (statuses.some((s) => WAITING_TASK.has(s))) return { status: "waiting", text: `waiting for a person · ${count}` };
  if (done === statuses.length) return { status: "done", text: count };
  return { status: "idle", text: count };
}

function abbr(name: string): string {
  const words = name.split(/[\s_.:-]+/).filter((w) => w !== "");
  return (words.length > 1 ? words.map((w) => w.charAt(0)).join("") : name.slice(0, 2)).slice(0, 2).toUpperCase();
}

/** The people and agents of a run as a graph: the chief of staff, who got which task, who checked. */
export function runGraph(raw: RawRunDetail, ended: boolean): RunDetail["graph"] {
  const byAgent = new Map<string, string[]>();
  for (const t of raw.tasks) {
    if (t.assignee === null) continue;
    byAgent.set(t.assignee, [...(byAgent.get(t.assignee) ?? []), t.status]);
  }
  const nodes = new Map<string, GraphNode>();
  const edges: GraphEdge[] = [];
  const chief = raw.chief_of_staff;
  if (chief !== null) {
    nodes.set(chief, { id: chief, name: chief, abbr: abbr(chief), status: "idle", statusText: "chief of staff", isHuman: false, isVerifier: false });
  }
  for (const [agent, statuses] of byAgent) {
    const { status, text } = nodeStatusOf(statuses, ended);
    const known = nodes.get(agent);
    nodes.set(agent, { id: agent, name: agent, abbr: abbr(agent), status, statusText: known === undefined ? text : `chief of staff · ${text}`, isHuman: false, isVerifier: false });
    if (chief !== null && chief !== agent) {
      edges.push({ from: chief, to: agent, kind: "reports", state: status === "done" ? "done" : status === "running" ? "active" : "idle" });
    }
  }
  // Checkers: another agent (a verifier) or a person, drawn under the agent whose work they checked.
  for (const t of raw.tasks) {
    for (const attempt of t.attempts) {
      for (const g of attempt.gates) {
        const [kind, who] = g.checked_by.split(":", 2);
        if (who === undefined || (kind !== "agent" && kind !== "human") || t.assignee === null) continue;
        const id = g.checked_by;
        if (!nodes.has(id)) {
          nodes.set(id, { id, name: who, abbr: abbr(who), status: "done", statusText: kind === "human" ? "person" : "verifier", isHuman: kind === "human", isVerifier: kind === "agent" });
        }
        if (!edges.some((e) => e.from === t.assignee && e.to === id)) {
          edges.push({ from: t.assignee, to: id, kind: kind === "human" ? "approval" : "checks", state: "done" });
        }
      }
    }
  }
  return { nodes: [...nodes.values()], edges };
}

function latestGate(raw: RawRunDetail): GateResultCard | null {
  let best: { gate: RawGate; task: RawRunDetail["tasks"][number] } | null = null;
  for (const task of raw.tasks) {
    for (const attempt of task.attempts) {
      for (const gate of attempt.gates) {
        if (best === null || gate.ledger_seq > best.gate.ledger_seq) best = { gate, task };
      }
    }
  }
  if (best === null) return null;
  const { gate } = best;
  return {
    gate: gate.gate,
    verdict: gate.outcome === "passed" ? "passed" : "failed",
    criteria: gate.verdicts.map((v) => ({ text: v.text ?? v.criterion, met: v.verdict === "met" })),
    verifierModel: null,
    differentFamily: null,
    saw: null,
    notShared: null,
    cost: null,
    ledgerEntry: entryId(raw.ledger.stream, gate.ledger_seq),
    checkedBy: gate.checked_by,
  };
}

export function runTask(raw: RawRunDetail["tasks"][number], stream: string): RunTask {
  return {
    id: raw.id,
    title: raw.title,
    assignee: raw.assignee ?? "unassigned",
    status: raw.status,
    attempt: raw.attempt,
    maxAttempts: raw.max_attempts,
    note: raw.note ?? "",
    gates: raw.attempts.flatMap((a) => a.gates.map((g) => gateOutcome(g, a.attempt, stream, a.submission))),
    spend: money(raw.spend.total),
    calls: raw.spend.calls,
    ...(typeof raw.submission === "number" ? { submission: raw.submission } : {}),
    ...(raw.definition_of_done === undefined
      ? {}
      : {
          criteria: raw.definition_of_done.criteria.map((c) => ({ id: c.id, text: c.text })),
          // A decision must cover exactly the gate's checks, so they travel with the gate's id.
          humanGates: raw.definition_of_done.gates.filter((g) => g.kind === "human").map((g) => ({ id: g.id, checks: Array.isArray(g.checks) ? [...g.checks] : [] })),
        }),
  };
}

export function proposalList(raw: RawProposals): ProposalList {
  return {
    items: raw.items.map((p) => ({
      subject: p.subject,
      ruleKey: p.cell.rule_key,
      role: p.cell.role,
      kind: p.kind,
      status: p.status,
      reasons: p.reasons,
      // A cell that counts nothing (never grantable, no such rule) has no `approved_asks`: null, not 0.
      approvedAsks: p.evidence.approved_asks === undefined ? null : wholeCount(p.evidence.approved_asks),
      solo: { approvedAsksSolo: wholeCount(p.solo.approved_asks_solo), soloShare: p.solo.solo_share },
      evidenceHash: p.evidence_hash,
    })),
    ledgerPosition: raw.ledger_position,
    asOf: raw.as_of,
  };
}

/** An amount given as money or as plain decimal text; null when it is neither (never throws). */
function looseMoney(value: RawMoney | string | null | undefined): Money | null {
  if (value === null || value === undefined) return null;
  try {
    return money(typeof value === "string" ? { amount: value, currency: "USD" } : value);
  } catch {
    return null;
  }
}

export function budgetAlert(raw: RawBudgetAlert, stream: string): BudgetAlert {
  const windowName = windowOf(raw.window);
  return {
    kind: raw.kind === "limit_passed" ? "limit_passed" : "hard_stop",
    ledgerEntry: entryId(stream, raw.ledger_seq),
    budget: raw.budget,
    scope: raw.scope,
    window: windowName,
    limit: looseMoney(raw.limit),
    spent: looseMoney(raw.spent),
    // Only what the server says: it leaves `resets_at` out for a held budget and for a breach in a closed window.
    resetsAt: raw.resets_at ?? null,
    held: raw.held,
    ...(raw.window_closed === true ? { windowClosed: true } : {}),
  };
}

export function runDetail(raw: RawRunDetail, now: Date): RunDetail {
  const summary = runSummary({ ...raw, tasks: raw.task_counts });
  const stream = raw.ledger.stream;
  const ended = summary.ended === true;
  const runBudget = raw.budgets.find((b) => b.scope === "run" && b.scope_id === raw.run_id);
  const started = raw.started_at === null ? Number.NaN : Date.parse(raw.started_at);
  const finished = raw.ended_at === null ? now.getTime() : Date.parse(raw.ended_at);
  const first = raw.tasks[0];
  return {
    ...summary,
    title: first === undefined ? summary.title : raw.tasks.length > 1 ? `${first.title} (+${String(raw.tasks.length - 1)} more)` : first.title,
    spend: { spent: summary.spend.spent, limit: moneyOrNull(runBudget?.limit) },
    canonRef: raw.canon_commit === null ? "none loaded" : raw.canon_commit.slice(0, 7),
    sandbox: "not recorded",
    elapsedSeconds: Number.isNaN(started) || Number.isNaN(finished) ? null : Math.max(0, Math.round((finished - started) / 1000)),
    needsYou: raw.approvals.filter((a) => a.status === "pending").length,
    graph: runGraph(raw, ended),
    latestGate: latestGate(raw),
    taskList: raw.tasks.map((t) => runTask(t, stream)),
    spendByModel: (raw.spend.by_model ?? []).map((m) => ({ name: m.model, spend: money(m.total), calls: m.calls })),
    spendByAgent: (raw.spend.by_agent ?? []).map((a) => ({ name: a.agent, spend: money(a.total), calls: a.calls })),
    unknownCostCalls: raw.spend.unknown_cost_calls ?? 0,
    costUnknownCalls: raw.spend.cost_unknown_calls ?? 0,
    atLeast: raw.spend.at_least == null ? null : exactMoney(raw.spend.at_least),
    budgetAlerts: raw.budget_alerts.map((a) => budgetAlert(a, stream)),
    ledger: { stream, entries: raw.ledger.seq, chainOk: raw.ledger.ok },
  };
}

// ---- events and ledger entries ----------------------------------------------------------------

function str(value: unknown): string {
  return typeof value === "string" ? value : value === undefined || value === null ? "" : JSON.stringify(value);
}

function shortTask(taskId: string | null, runId: string | null): string {
  if (taskId === null) return "";
  return runId !== null && taskId.startsWith(`${runId}-`) ? taskId.slice(runId.length + 1) : taskId;
}

/** The fixed words for a stop whose reason could not be kept (the same as the server's). */
export function withheldText(code: string): string {
  return code === "org_key_destroyed" ? "reason withheld: org key destroyed" : "reason withheld: org key unavailable";
}

/** One plain sentence for a ledger entry (hashes and ids only; the ledger holds no content). */
/** R1 (R1.27-H): only the exact actor `system` may show a reason or a note. Anything else is a person's or unknown text. */
export function reasonTextVisible(actor: string | null | undefined): boolean {
  return actor === "system";
}

export const WITHHELD_TEXT = "withheld (a person's text is not shown)";

const isIncident = (e: RawEvent): boolean => e.type === "decision.recorded" && e.payload.kind === "engine_incident";

const plain = (value: unknown): string => str(value).replace(/_/g, " ");

/**
 * One sentence for a `decision.recorded` of a kind (spec 3.3), from ids, numbers and fixed words only.
 * An `engine_incident` never uses its `note`; a kind that is not known keeps the common words.
 */
export function decisionSentence(kind: string | null, actor: string, get: (key: string) => unknown): string {
  switch (kind) {
    case "human_decision":
      return `${actor} decided gate ${str(get("gate"))}: ${str(get("outcome"))}`;
    case "human_decision_refused":
      return `${actor}'s decision was refused: ${plain(get("why"))}`;
    case "engine_incident":
      return `Engine incident: the task is stuck in ${str(get("task_status"))}${get("human_required") === true ? ", a person is needed" : ""}`;
    case "step_restarted":
      return `Step ${str(get("step"))} began again (attempt ${str(get("attempt"))}, epoch ${str(get("epoch"))})`;
    case "scripted_approval":
      return `Scripted approval of gate ${str(get("gate"))} as ${str(get("approver"))}, no human review`;
    case "human_wait_warning":
      return `A person is needed: ${str(get("days_left"))} days left before the wait for attempt ${str(get("attempt"))} ends`;
    case "human_wait_expired":
      return `The wait for a person ended after the time limit (attempt ${str(get("attempt"))})`;
    case "dependency_waiting":
      return `Held until ${str(get("waiting_on"))} is decided by a person`;
    case "tool_set_frozen":
      // S4 AD-16, AD-29: the first entry of a guarded run (names and hashes only)
      return `Tool set frozen for ${str(get("agent"))}: ${str(get("tool_count"))} tools (${str(get("adapter"))})`;
    case null:
    case "unknown":
      return `Decision recorded by ${actor}`;
    default:
      return `Decision recorded by ${actor} (${kind})`;
  }
}

export function eventText(e: RawEvent): string {
  const p = e.payload;
  const task = shortTask(e.task_id, e.run_id);
  switch (e.type) {
    case "run.started":
      return `Run started by ${e.actor}`;
    case "run.ended": {
      const counts = p.counts;
      const aborted = p.aborted === true;
      const how = aborted ? "aborted" : p.status === "stopped" ? "stopped" : "ended";
      return `Run ${how}${counts === undefined ? "" : ` (${Object.entries(counts as Record<string, number>).map(([k, n]) => `${String(n)} ${k.replace("_", " ")}`).join(", ")})`}`;
    }
    case "run.stop_requested":
      return `Stop requested by ${e.actor}${typeof p.reason_withheld === "string" ? ` (${withheldText(p.reason_withheld)})` : ""}`;
    case "task.created":
      return `Task created: ${str(p.title) || task}`;
    case "task.transitioned":
      return `${task}: ${str(p.from)} to ${str(p.to)}${str(p.reason) === "" || !reasonTextVisible(e.actor) ? "" : ` (${str(p.reason).slice(0, 120)})`}`;
    case "policy.decided":
      return `Policy ${str(p.decision)}: ${str(p.agent)} may ${str(p.action)}${str(p.target) === "" ? "" : ` ${str(p.target)}`}`;
    case "gate.completed":
      return `Gate ${str(p.gate)} ${str(p.outcome)} on ${task} (attempt ${str(p.attempt)}, checked by ${str(p.checked_by)})`;
    case "model.called":
      return `${str(p.agent)} called ${str(p.model)} (${str(p.outcome)}, ${str(p.latency_ms)} ms)`;
    case "budget.reserved":
      return `Reserved for ${str(p.agent)}'s call${e.cost === null ? "" : ` ($${exactMoney(e.cost).amount})`}`;
    case "budget.settled":
      return `Charged ${str(p.agent)}'s call${e.cost === null ? "" : ` $${exactMoney(e.cost).amount}`}`;
    case "budget.released":
      return "Reservation released";
    case "budget.exceeded":
      return str(p.reason) === "" || !reasonTextVisible(e.actor) ? "Budget stop" : `Budget stop: ${str(p.reason)}`;
    case "budget.breached":
      return "Budget limit passed";
    case "budget.unblocked":
      return `Budget unblocked by ${e.actor}`;
    case "budget.window_rolled":
      return `Budget window rolled: ${str(p.budget)}`;
    case "decision.recorded":
      return decisionSentence(typeof p.kind === "string" ? p.kind : null, e.actor, (k) => p[k]);
    case "model.blocked":
      return `${str(p.agent) || e.actor}'s model call was blocked: ${str(p.reason) || "no reason recorded"}`;
    case "tool.called": {
      // S4 AD-28: the body ended `ok`, `error` (it raised) or `cancelled`
      const how = p.outcome === "error" ? " (the tool failed)" : p.outcome === "cancelled" ? " (cancelled)" : "";
      return `${str(p.agent) || e.actor} used the tool ${str(p.tool) || str(p.name) || "(unnamed)"}${how}`;
    }
    case "eval.scored":
      return `Eval ${str(p.eval) || str(p.name) || "score"} recorded for ${str(p.agent) || e.actor}`;
    case "gate.started":
      return `Gate ${str(p.gate)} started on ${task}${p.attempt === undefined ? "" : ` (attempt ${str(p.attempt)})`}`;
    case "task.stop_requested":
      return `Stop requested for ${task} by ${e.actor}`;
    case "content.captured":
      return `${str(p.kind) || "Content"} kept in the content store${task === "" ? "" : ` for ${task}`} (hash only)`;
    case "content.expired":
      return `Kept ${str(p.kind) || "content"} expired${task === "" ? "" : ` for ${task}`}`;
    case "content.read":
      return `${e.actor} read kept content (hash only)`;
    case "breaker.tripped":
      return `Breaker ${str(p.rule) || str(p.name) || ""} tripped`.replace("  ", " ");
    case "breaker.reset":
      return `Breaker ${str(p.rule) || str(p.name) || ""} reset by ${e.actor}`.replace("  ", " ");
    case "org.halted":
      return `Organization halted by ${e.actor}`;
    case "org.resumed":
      return `Organization resumed by ${e.actor}`;
    case "org.role_paused":
      return `A role was paused by ${e.actor}`;
    case "canon.loaded":
      return `Canon loaded${str(p.commit) === "" ? "" : ` at ${str(p.commit).slice(0, 7)}`}`;
    case "approval.requested":
      return `Approval requested by ${str(p.agent) || e.actor}`;
    case "approval.decided":
      return `Approval ${str(p.status) || "decided"} by ${e.actor}`;
    default:
      return e.type;
  }
}

export function eventCategory(type: string): EventCategory {
  if (type.startsWith("gate.")) return "gates";
  if (type.startsWith("budget.") || type === "model.called" || type === "model.blocked") return "spend";
  if (type.startsWith("approval.") || type.startsWith("decision.")) return "approvals";
  return "tasks";
}

function eventTone(e: RawEvent): Tone {
  if (e.type === "gate.completed") return e.payload.outcome === "passed" ? "accent" : "warm";
  if (e.type === "budget.exceeded" || e.type === "budget.breached" || e.type === "run.stop_requested" || e.type.startsWith("approval.")) return "warm";
  if (e.type === "run.ended" || e.type === "task.transitioned" && e.payload.to === "done") return "accent";
  return "muted";
}

/** A ledger entry as a timeline event; `seq` is the entry's number in the run's stream. */
export function runEvent(e: RawEvent): RunEvent {
  return { seq: e.seq, at: e.at, kind: e.type, category: eventCategory(e.type), text: eventText(e), tone: eventTone(e), entry: entryId(e.stream, e.seq), taskId: e.task_id };
}

export function ledgerCategory(type: string): LedgerCategory {
  if (type.startsWith("gate.")) return "gates";
  if (type.startsWith("budget.") || type === "model.called" || type === "model.blocked") return "spend";
  if (type.startsWith("approval.") || type.startsWith("decision.") || type === "run.stop_requested" || type === "task.stop_requested" || type.startsWith("breaker.") || type.startsWith("org.") || type.startsWith("human.")) return "decisions";
  return "tasks";
}

function flat(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 240 ? `${text.slice(0, 237)}...` : text;
}

export function ledgerEntry(e: RawEvent): LedgerEntry {
  return {
    n: entryId(e.stream, e.seq),
    label: String(e.seq),
    at: e.at,
    kind: e.type,
    category: ledgerCategory(e.type),
    actor: e.actor,
    summary: eventText(e),
    cost: e.cost === null ? null : money(e.cost),
    hash: e.hash,
    prevHash: e.prev_hash,
    stream: e.stream,
    taskId: e.task_id,
    fields: [
      ...(e.cost_basis === null ? [] : [{ label: "cost basis", value: e.cost_basis }]),
      ...payloadFields(e),
    ],
  };
}

const TEXT_KEYS = new Set(["reason", "note"]);
const emptyText = (value: unknown): boolean => value === null || value === undefined || value === "";

/** The payload as `label: value` rows; a person's `reason` or `note` (R8) is never printed, nested keys get dotted labels. */
function payloadFields(e: RawEvent): { label: string; value: string }[] {
  const incident = isIncident(e);
  const visible = reasonTextVisible(e.actor);
  const listed = new Set(e.redacted ?? []);
  const rows: { label: string; value: string }[] = [];
  const walk = (label: string, key: string, value: unknown, top: boolean): void => {
    if (incident && top && key === "note_present") {
      rows.push({ label, value: value === true ? "yes" : "no" });
    } else if (TEXT_KEYS.has(key) && ((incident && top && key === "note" && !emptyText(value)) || (!emptyText(value) && !visible) || (listed.has(key) && (value === null || !visible)))) {
      rows.push({ label, value: WITHHELD_TEXT });
    } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      for (const [k, v] of Object.entries(value)) walk(`${label}.${k}`, k, v, false);
    } else if (Array.isArray(value) && value.some((item) => item !== null && typeof item === "object")) {
      value.forEach((item, i) => {
        if (item !== null && typeof item === "object" && !Array.isArray(item)) {
          for (const [k, v] of Object.entries(item as Record<string, unknown>)) walk(`${label}.${String(i)}.${k}`, k, v, false);
        } else {
          rows.push({ label: `${label}.${String(i)}`, value: flat(item) });
        }
      });
    } else {
      rows.push({ label, value: flat(value) });
    }
  };
  for (const [k, v] of Object.entries(e.payload)) walk(k, k, v, true);
  return rows;
}

// ---- approvals --------------------------------------------------------------------------------

export function approvalTitle(a: RawApproval): string {
  const { kind, target, amount } = a.action;
  return approvalSentence({ agent: a.agent_id, kind, target, amount: moneyOrNull(amount) });
}

export function approval(a: RawApproval, runSpend: Approval["runSpend"]): Approval {
  // S4 AD-46: an approval that expired (unanswered, or approved and never used) let nothing happen, so it is
  // never shown as approved and names no decider
  const expired = a.status === "expired";
  const state = a.status === "pending" ? "pending" : a.status === "denied" || expired ? "denied" : "approved";
  const ledger = a.ledger ?? [];
  const requested = ledger.find((l) => l.type === "approval.requested");
  const decided = ledger.find((l) => l.type === "approval.decided");
  const stream = `run:${a.run_id}`;
  return {
    id: a.id,
    title: approvalTitle(a),
    action: { kind: a.action.kind, target: a.action.target, amount: moneyOrNull(a.action.amount) },
    requester: a.agent_id,
    // #1458: only the server's plain `true` counts; no terms, an older server or any other value is "not known" (false).
    callerIsOriginator: a.caller_terms?.caller_is_originator === true,
    runId: a.run_id,
    runLabel: runLabel(a.run_id),
    taskId: a.task_id,
    requestedAt: a.requested_at,
    state,
    effect: `Lets ${escapeInert(a.agent_id)} do this one thing, once, in this run: ${actionPhrase({ kind: a.action.kind, target: a.action.target, amount: moneyOrNull(a.action.amount) })}`,
    rule: a.asked_because === "" ? "policy asks a person" : a.asked_because,
    runSpend,
    evidence: [
      { state: "ok", text: `Requested${requested === undefined ? "" : ` (ledger #${String(requested.ledger_seq)})`}` },
      ...(state === "pending" ? [{ state: "wait" as const, text: "Waiting for a person to decide" }] : []),
      ...(expired ? [{ state: "ok" as const, text: "Expired: the deadline passed before it was used" }] : []),
    ],
    decision:
      state === "pending" || expired || a.decided_by === null || a.decided_at === null
        ? null
        : {
            by: a.decided_by,
            at: a.decided_at,
            // R8: a person's reason is never shown, whatever the answer carries; only whether the content store keeps it.
            reasonHash: a.decision_reason_hash,
            ledgerEntry: decided === undefined ? "" : entryId(stream, decided.ledger_seq),
          },
    solo:
      a.caller_terms?.mode === "solo" && typeof a.action_digest === "string"
        ? {
            actionDigest: a.action_digest,
            freshProofNeeded: a.caller_terms.fresh_proof_needed,
            // What SA-7 would accept from this caller; anything but a list of words is "nothing helps".
            proofs: Array.isArray(a.caller_terms.proofs) ? a.caller_terms.proofs.filter((p): p is string => typeof p === "string") : [],
          }
        : null,
  };
}

// ---- budgets ----------------------------------------------------------------------------------

export function windowOf(value: string | null | undefined): BudgetWindow {
  return value === "day" || value === "month" ? value : "none";
}

/** When the window that started at `start` ends (UTC): the next midnight, or the first of next month. */
export function windowEnd(window: BudgetWindow, start: string | null): string | null {
  if (window === "none" || start === null) return null;
  const from = new Date(start);
  if (Number.isNaN(from.getTime())) return null;
  const end =
    window === "day"
      ? new Date(from.getTime() + 86_400_000)
      : new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth() + 1, 1));
  return end.toISOString();
}

const PERIOD: Record<BudgetWindow, string> = { none: "Lifetime", day: "Daily (UTC)", month: "Monthly (UTC)" };

export function budgetScope(b: Pick<RawBudget, "scope" | "scope_id">): string {
  return b.scope === "org" ? "Organization" : `${b.scope[0]?.toUpperCase() ?? ""}${b.scope.slice(1)}: ${b.scope_id}`;
}

export function budgetRow(b: RawBudget): BudgetRow {
  const window = windowOf(b.window);
  const limit = money(b.limit);
  const spent = money(b.spent);
  const cap = toMicros(limit);
  const percent = cap <= 0n ? 0 : Math.max(0, Math.min(100, Number((toMicros(spent) * 100n + cap / 2n) / cap)));
  return {
    id: b.id,
    scope: budgetScope(b),
    period: PERIOD[window],
    limit,
    spent,
    spentIsPeak: false,
    percentUsed: percent,
    status: b.blocked ? "blocked" : percent >= 90 ? "near" : "ok",
    window,
    onBreach: (b.on_breach === "reset" ? "reset" : "hold") satisfies BreachMode,
    windowStart: b.window_start,
    resetsAt: windowEnd(window, b.window_start),
    available: money(b.available),
  };
}

// ---- verification -----------------------------------------------------------------------------

function anchorState(info: RawAnchorInfo | null | undefined, configured: boolean, checked: boolean): AnchorState {
  if (!configured) return "not_configured";
  if (!checked) return "unreadable";
  if (info === null || info === undefined) return "unanchored";
  if (info.status === "anchored") return "anchored";
  if (info.status === "problems" || (info.problems?.length ?? 0) > 0) return "problems";
  return "unanchored";
}

/**
 * The verify answer in the app's terms. `verified` is the server's own flag (chains intact AND
 * signed anchors that check out); this never upgrades it. A stream with no anchors is
 * `unanchored`, never verified.
 */
export function chainStatus(raw: RawVerify, now: Date): ChainStatus {
  // The single-stream endpoint flattens its stream to the top level and moves the global
  // anchor flags to `anchor_status`.
  const single = raw.anchor_status !== undefined;
  const global = (single ? raw.anchor_status : raw.anchors) ?? { configured: false };
  const configured = global.configured === true;
  const checked = global.checked === true;
  const rawStreams: { stream: string; checked: number; problem: string | null; at_seq?: number | null; anchors: RawAnchorInfo | null }[] =
    raw.streams ?? (raw.stream === undefined ? [] : [{ stream: raw.stream, checked: raw.checked ?? 0, problem: raw.problem ?? null, at_seq: raw.at_seq ?? null, anchors: single ? (raw.anchors ?? null) : null }]);
  // `ok` on a stream is also false for an anchor problem, so the chain is judged by its own `problem`.
  const streams: StreamCheck[] = rawStreams.map((s) => ({
    stream: s.stream,
    chainOk: s.problem === null,
    entries: s.checked,
    anchor: anchorState(s.anchors, configured, checked),
    unanchoredEvents: s.anchors?.unanchored_events ?? 0,
    chainProblem: s.problem === null ? null : `${s.problem}${s.at_seq === null || s.at_seq === undefined ? "" : ` at entry ${String(s.at_seq)}`}`,
    anchorProblems: (s.anchors?.problems ?? []).map((p) => ({ code: p.code, detail: p.detail })),
  }));
  const states = new Set(streams.map((s) => s.anchor));
  const overall: AnchorState = !configured
    ? "not_configured"
    : !checked
      ? "unreadable"
      : states.has("problems")
        ? "problems"
        : states.has("unanchored") || streams.length === 0
          ? "unanchored"
          : "anchored";
  return {
    verified: raw.verified && overall === "anchored",
    chainOk: streams.length > 0 ? streams.every((s) => s.chainOk) : raw.chain_ok,
    anchorState: overall,
    streams,
    ...(raw.truncated === true ? { truncated: true } : {}),
    entries: streams.reduce((n, s) => n + s.entries, 0),
    gaps: streams.filter((s) => !s.chainOk).length,
    anchor: null,
    checkedAt: now.toISOString(),
  };
}

// ---- totals -----------------------------------------------------------------------------------

export function sumMoney(values: Money[]): Money {
  return values.reduce((sum, m) => addMoney(sum, m), usd("0"));
}

// ---- organization safety (E19) ----------------------------------------------------------------

/** A count from the server: a whole number of 0 or more, else 0 (an absent or odd value shows nothing). */
function wholeCount(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : 0;
}

/** The halt object as the app uses it. */
export function orgStatus(raw: RawHalt): OrgStatus {
  return {
    halted: raw.halted,
    by: raw.by,
    at: raw.at,
    cause: raw.cause,
    rule: raw.rule,
    ledgerSeq: raw.ledger_seq,
    phase: raw.phase,
    errorClass: raw.error_class,
    failingRule: raw.failing_rule,
    failedTicks: raw.failed_ticks,
    roles: raw.roles.map((r) => ({
      role: r.role,
      paused: r.paused,
      by: r.by,
      at: r.at,
      cause: r.cause,
      rule: r.rule,
      ledgerSeq: r.ledger_seq,
      inConfig: r.in_config,
    })),
    // Fail closed: anything but an explicit true hides the buttons.
    canAct: raw.can_act === true,
    soloAfterRemoval: wholeCount(raw.solo_approval?.approved_solo_after_removal),
    ...(raw.solo_approval === undefined ? {} : { soloNow: raw.solo_approval.solo_now }),
  };
}

const BREAKER_STATES: readonly BreakerState[] = ["ok", "tripped", "disabled"];
const EVALUATION_STATES: readonly EvaluationState[] = ["not_configured", "starting", "ok", "failing", "stopped"];

export function breakerRow(raw: RawBreaker): BreakerRow {
  const state = BREAKER_STATES.find((s) => s === raw.state);
  if (state === undefined) throw new RangeError(`unknown breaker state "${raw.state}"`);
  return {
    name: raw.name,
    signal: raw.signal as BreakerSignal | null,
    action: raw.action as BreakerAction | null,
    role: raw.role,
    windowSeconds: raw.window_seconds,
    dryRun: raw.dry_run === true,
    limit: raw.limit,
    observed: raw.observed,
    observedAs: raw.observed_as === "live" || raw.observed_as === "at_trip" ? raw.observed_as : null,
    unknownCostCalls: raw.unknown_cost_calls ?? 0,
    windowStart: raw.window_start,
    state,
    trippedAt: raw.tripped_at,
    trippedSeq: raw.tripped_seq,
    trippedBy: raw.tripped_by,
    manual: raw.manual === true,
    lastResetAt: raw.last_reset_at,
    disabledAt: raw.disabled_at,
    disabledBy: raw.disabled_by,
    disabledReason: raw.disabled_reason as DisableReason | null,
    inConfig: raw.in_config,
  };
}

export function breakersView(raw: RawBreakers): BreakersView {
  const e = raw.evaluation;
  // An unknown evaluation state is never shown as healthy.
  const state = EVALUATION_STATES.find((s) => s === e.state) ?? "stopped";
  return {
    org: raw.org,
    halt: orgStatus(raw.halt),
    evaluation: {
      state,
      tickSeconds: e.tick_seconds,
      haltAfterFailedTicks: e.halt_after_failed_ticks,
      lastEvaluatedAt: e.last_evaluated_at,
      lastErrorAt: e.last_error_at,
      lastErrorPhase: e.last_error_phase,
      lastErrorClass: e.last_error_class,
      lastFailingRule: e.last_failing_rule,
      consecutiveFailures: e.consecutive_failures,
    },
    breakers: raw.breakers.map(breakerRow),
  };
}

// -- the per-task audit (R1.27) --------------------------------------------------------------

const auditSpend = (raw: RawAuditSpend): AuditSpend => ({
  total: exactMoney(raw.total),
  calls: raw.calls,
  priceUnknownCalls: raw.price_unknown_calls,
  otherCurrencyCalls: raw.other_currency_calls,
  failedCallsNoCost: raw.failed_calls_no_cost,
  unknownCostCalls: raw.unknown_cost_calls,
  costUnknownCalls: raw.cost_unknown_calls ?? raw.price_unknown_calls,
  atLeast: raw.at_least == null ? null : exactMoney(raw.at_least),
});

const DECISION_COMMON = new Set(["ledger_seq", "at", "actor", "kind"]);

/** Only scalar fields of a decision row are kept; anything else (a stray `note`) is dropped (M8, M12). */
function decisionRow(raw: RawTaskAudit["decisions"][number]): AuditDecision {
  const fields: AuditDecision["fields"] = {};
  for (const [key, value] of Object.entries(raw)) {
    if (DECISION_COMMON.has(key) || key === "note" || key === "reason") continue;
    if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") fields[key] = value;
  }
  return { seq: raw.ledger_seq, at: raw.at, actor: raw.actor, kind: raw.kind, fields };
}

/** The server's audit answer as the page shows it. The mapper copies named fields only. */
export function taskAudit(raw: RawTaskAudit): TaskAudit {
  const stream = `run:${raw.run_id}`;
  return {
    runId: raw.run_id,
    task: { id: raw.task.id, title: raw.task.title, assignee: raw.task.assignee, status: raw.task.status, attempt: raw.task.attempt, maxAttempts: raw.task.max_attempts },
    chain: { ok: raw.chain.ok, headSeq: raw.chain.head_seq, head: raw.chain.head },
    truncated: raw.truncated,
    totalEntries: raw.total_entries,
    entries: raw.entries.map((e) => ({ ...ledgerEntry(e), cost: e.cost === null ? null : exactMoney(e.cost) })),
    attempts: raw.attempts.map((a) => ({
      attempt: a.attempt,
      submission: a.submission,
      gates: a.gates.map((g) => gateOutcome(g, a.attempt, stream)),
      movedTo: a.moved_to,
      reason: a.reason,
      reasonHash: a.reason_hash,
    })),
    spend: auditSpend(raw.spend),
    spendByAttempt: raw.spend_by_attempt.map((s) => ({ attempt: s.attempt, ...auditSpend(s) })),
    approvals: raw.approvals.map((a) => ({
      id: a.id,
      agent: a.agent_id,
      status: a.status,
      askedBecause: a.asked_because,
      decidedBy: a.decided_by,
      decidedAt: a.decided_at,
      reasonHash: a.decision_reason_hash,
      reasonAvailable: a.reason_available,
    })),
    decisions: raw.decisions.map(decisionRow),
    rework: raw.rework.map((r) => ({
      seq: r.ledger_seq,
      at: r.at,
      from: r.from,
      to: r.to,
      attemptBefore: r.attempt_before,
      attemptAfter: r.attempt_after,
      maxAttempts: r.max_attempts,
      by: r.by,
      reason: r.reason,
      reasonHash: r.reason_hash,
      feedbackHash: r.feedback_hash,
      actor: r.actor,
    })),
  };
}
