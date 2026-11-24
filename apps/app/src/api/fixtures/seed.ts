/**
 * Sample data for the fixture client, taken from the app design files. It is **sample data**:
 * nothing here is real, and the app says so wherever the fixture client is in use. Times are fixed so tests and screenshots never drift.
 */
import { usd } from "../money";
import type {
  Approval,
  BudgetRow,
  BudgetSummary,
  GatePreset,
  LedgerEntry,
  ModelSpend,
  PolicyPreset,
  RunDetail,
  RunEvent,
  RunSummary,
  TemplateSummary,
} from "../types";

/** "Now" in the sample world: a few minutes after run #128 started. */
export const FIXTURE_NOW = "2026-10-22T19:44:10Z";
const at = (time: string, date = "2026-10-22"): string => `${date}T${time}Z`;
const hash = (seed: string): string => {
  let h = 0x811c9dc5;
  const out: string[] = [];
  for (let round = 0; round < 8; round += 1) {
    for (const ch of `${seed}:${String(round)}`) h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193) >>> 0;
    out.push(h.toString(16).padStart(8, "0"));
  }
  return out.join("");
};

// ---- runs ---------------------------------------------------------------------------------

export const RUN_128: RunDetail = {
  id: "run-128",
  label: "#128",
  title: "Fix issue #142: add retries to the payment webhook",
  status: "running",
  template: "Dev team",
  startedAt: at("19:39:18"),
  startedBy: "You",
  tasks: { done: 1, total: 3 },
  gates: { passed: 4, failed: 0, total: 5 },
  spend: { spent: usd("1.84"), limit: usd("50.00") },
  canonRef: "a1b9f03",
  sandbox: "podman",
  elapsedSeconds: 192,
  needsYou: 1,
  graph: {
    nodes: [
      { id: "owner", name: "Owner", abbr: "You", status: "waiting", statusText: "1 approval waiting", isHuman: true, isVerifier: false },
      { id: "cos", name: "Chief of Staff", abbr: "CoS", status: "done", statusText: "planned 3 tasks · $0.18", isHuman: false, isVerifier: false },
      { id: "dev", name: "Developer", abbr: "DEV", status: "done", statusText: "gates 3/3 · $1.42", isHuman: false, isVerifier: false },
      { id: "tester", name: "Tester", abbr: "TST", status: "running", statusText: "running · 0m 41s", isHuman: false, isVerifier: false },
      { id: "ver", name: "Code reviewer", abbr: "REV", status: "done", statusText: "passed 1 · $0.06", isHuman: false, isVerifier: true },
    ],
    edges: [
      { from: "owner", to: "cos", kind: "approval", state: "active" },
      { from: "cos", to: "dev", kind: "reports", state: "done" },
      { from: "cos", to: "tester", kind: "reports", state: "active" },
      { from: "dev", to: "ver", kind: "checks", state: "done" },
      { from: "tester", to: "ver", kind: "checks", state: "idle" },
    ],
  },
  taskList: [
    { id: "t-plan", title: "Plan the work", assignee: "chief", status: "done", attempt: 1, maxAttempts: 3, note: "", gates: [], spend: usd("0.18"), calls: 1 },
    { id: "t-retries", title: "Add retries to the payment webhook", assignee: "dana", status: "done", attempt: 2, maxAttempts: 3, note: "", gates: [], spend: usd("1.60"), calls: 4 },
    { id: "t-retest", title: "Re-run the full suite", assignee: "theo", status: "in_progress", attempt: 1, maxAttempts: 3, note: "", gates: [], spend: usd("0.06"), calls: 1 },
  ],
  latestGate: {
    gate: "Independent review",
    verdict: "passed",
    criteria: [
      { text: "Webhook retries 3 times with backoff", met: true },
      { text: "New tests cover timeout and 5xx cases", met: true },
      { text: "No change to the public API", met: true },
    ],
    verifierModel: "GPT",
    differentFamily: true,
    saw: ["task", "done-definition", "diff", "canon"],
    notShared: ["developer reasoning"],
    cost: usd("0.06"),
    ledgerEntry: "1043",
  },
};

const otherRun = (
  n: string,
  title: string,
  status: RunSummary["status"],
  template: string,
  startedAt: string,
  spent: string,
  limit: string,
  tasks: [number, number],
  gates: [number, number, number],
  needsYou = 0,
): RunDetail => {
  const nodeStatus = (ownStatus: "done" | "running" | "idle") =>
    status === "done" ? "done" : status === "failed" || status === "stopped" ? "idle" : ownStatus;
  return {
    id: `run-${n}`,
    label: `#${n}`,
    title,
    status,
    template,
    startedAt,
    startedBy: "You",
    tasks: { done: tasks[0], total: tasks[1] },
    gates: { passed: gates[0], failed: gates[1], total: gates[2] },
    spend: { spent: usd(spent), limit: usd(limit) },
    canonRef: "a1b9f03",
    sandbox: "podman",
    elapsedSeconds: 0,
    needsYou,
    graph: {
      nodes: RUN_128.graph.nodes.map((node) => ({
        ...node,
        status: node.isHuman ? (needsYou > 0 ? "waiting" : "idle") : nodeStatus("idle"),
        statusText: node.isHuman ? (needsYou > 0 ? "1 approval waiting" : "no one needed") : status,
      })),
      edges: RUN_128.graph.edges.map((edge) => ({ ...edge, state: status === "done" ? "done" : "idle" })),
    },
    latestGate: null,
  };
};

export const RUNS: RunDetail[] = [
  RUN_128,
  otherRun("127", "Migrate the invoice exporter", "blocked", "Dev team", at("17:02:00"), "50.00", "50.00", [3, 4], [8, 0, 9]),
  otherRun("126", "Release v1.2.0-rc1 to staging", "waiting", "Dev team", at("18:10:00"), "6.30", "50.00", [4, 4], [4, 0, 4], 1),
  otherRun("121", "Refactor the billing module", "done", "Dev team", at("16:05:00", "2026-10-21"), "31.40", "50.00", [9, 9], [14, 0, 14]),
  otherRun("119", "Review pull request #88", "failed", "Code review", at("11:32:00", "2026-10-20"), "2.10", "10.00", [1, 2], [2, 1, 3]),
];

export const RUN_EVENTS_128: RunEvent[] = [
  { seq: 1, at: at("19:39:18"), kind: "run.started", category: "tasks", text: "Run #128 started by You", tone: "muted" },
  { seq: 2, at: at("19:39:20"), kind: "canon.loaded", category: "tasks", text: "Rules loaded from Git commit a1b9f03", tone: "muted" },
  { seq: 3, at: at("19:39:22"), kind: "budget.checked", category: "spend", text: "Run budget $50.00 available · org month $157.90 left", tone: "muted" },
  { seq: 4, at: at("19:39:30"), kind: "task.assigned", category: "tasks", text: "Chief of Staff assigns the fix to Developer", tone: "muted" },
  { seq: 5, at: at("19:40:12"), kind: "model.call", category: "spend", text: "Developer · Claude Sonnet · reserved $0.50, settled $0.31", tone: "muted" },
  { seq: 6, at: at("19:41:40"), kind: "submission", category: "tasks", text: "Developer submits the change: +18 −4 in 2 files", tone: "muted" },
  { seq: 7, at: at("19:41:49"), kind: "gate.passed", category: "gates", text: "Rule check: lint and format clean", tone: "accent" },
  { seq: 8, at: at("19:41:51"), kind: "gate.passed", category: "gates", text: "Tests in sandbox: 48 passed, 0 failed", tone: "accent" },
  { seq: 9, at: at("19:41:58"), kind: "gate.passed", category: "gates", text: "Independent review passed · GPT · $0.06", tone: "accent" },
  { seq: 10, at: at("19:42:05"), kind: "approval.requested", category: "approvals", text: "Developer asks to merge to main (policy: merge = ask)", tone: "warm" },
  { seq: 11, at: at("19:42:08"), kind: "task.started", category: "tasks", text: "Tester re-runs the full suite in the sandbox", tone: "warm" },
];

// ---- approvals ----------------------------------------------------------------------------

export const APPROVALS: Approval[] = [
  {
    id: "a1",
    title: "Merge to main",
    requester: "Developer",
    action: { kind: "merge", target: "main", amount: null },
    runId: "run-128",
    runLabel: "#128",
    taskId: "t-retries",
    requestedAt: at("19:42:05"),
    state: "pending",
    effect:
      "NuraMesh merges branch fix/142-webhook-retries into main once. The change: +18 −4 in 2 files (payments/webhook.py, tests/test_webhook.py).",
    rule: "merge: ask",
    runSpend: { spent: usd("1.84"), limit: usd("50.00") },
    evidence: [
      { state: "ok", text: "Rule check: lint and format clean" },
      { state: "ok", text: "Tests in sandbox: 48 passed, 0 failed" },
      { state: "ok", text: "Independent review passed (GPT, different family)" },
      { state: "wait", text: "Tester re-run still in progress" },
    ],
    decision: null,
  },
  {
    id: "a2",
    title: "Deploy to staging",
    requester: "Chief of Staff",
    action: { kind: "run_command", target: "deploy staging", amount: null },
    runId: "run-126",
    runLabel: "#126",
    requestedAt: at("18:10:00"),
    state: "pending",
    effect: "NuraMesh runs the staging deploy command once for release v1.2.0-rc1, inside the sandbox.",
    rule: "deploy: ask",
    runSpend: { spent: usd("6.30"), limit: usd("50.00") },
    evidence: [
      { state: "ok", text: "All 4 task gates passed" },
      { state: "ok", text: "Release notes reviewed by verifier" },
      { state: "ok", text: "Budget well within limit" },
    ],
    decision: null,
  },
  {
    id: "a3",
    title: "Spend over $20.00",
    requester: "Chief of Staff",
    action: { kind: "spend", target: null, amount: usd("35.00") },
    runId: "run-121",
    runLabel: "#121",
    requestedAt: at("16:05:00", "2026-10-21"),
    state: "approved",
    effect: "Raised the run spend allowed without asking from $20.00 to $35.00, once.",
    rule: "spend over $20: ask",
    runSpend: { spent: usd("31.40"), limit: usd("50.00") },
    evidence: [{ state: "ok", text: "Plan listed 9 tasks with definitions of done" }],
    decision: {
      by: "You",
      at: at("16:09:00", "2026-10-21"),
      reasonHash: "a".repeat(64), // the words are kept in the content store, never shown (R1.27-H)
      ledgerEntry: "0988",
    },
  },
  {
    id: "a4",
    title: "Send external email",
    requester: "Developer",
    action: { kind: "external_message", target: "a customer address", amount: null },
    runId: "run-119",
    runLabel: "#119",
    requestedAt: at("11:32:00", "2026-10-20"),
    state: "denied",
    effect: "Would have sent one email to a customer address (address stored as a hash).",
    rule: "external_message: ask",
    runSpend: { spent: usd("2.10"), limit: usd("10.00") },
    evidence: [{ state: "wait", text: "No support ticket linked" }],
    decision: {
      by: "You",
      at: at("11:40:00", "2026-10-20"),
      reasonHash: null, // an old decision with no kept reason
      ledgerEntry: "0912",
    },
  },
];

// ---- budgets and spend ------------------------------------------------------------------

const budget = (
  id: string,
  scope: string,
  period: string,
  limit: string,
  spent: string,
  percentUsed: number,
  status: BudgetRow["status"],
  spentIsPeak = false,
): BudgetRow => ({
  id,
  scope,
  period,
  limit: usd(limit),
  spent: usd(spent),
  spentIsPeak,
  percentUsed,
  status,
  window: period === "monthly" ? "month" : "none",
  onBreach: "hold",
  windowStart: period === "monthly" ? "2026-10-01T00:00:00.000Z" : null,
  resetsAt: period === "monthly" ? "2026-11-01T00:00:00.000Z" : null,
  available: null,
});

export const BUDGETS: BudgetRow[] = [
  budget("org-month", "Organization", "monthly", "200.00", "42.10", 21, "ok"),
  budget("run-128", "Run #128", "per run", "50.00", "1.84", 4, "ok"),
  budget("run-127", "Run #127", "per run", "50.00", "50.00", 100, "blocked"),
  budget("dev-task", "Developer", "per task", "5.00", "3.90", 78, "near", true),
  budget("cos-run", "Chief of Staff", "per run", "20.00", "6.30", 32, "ok"),
  budget("tester-task", "Tester", "per task", "1.00", "0.12", 12, "ok", true),
];

export const BUDGET_SUMMARY: BudgetSummary = {
  orgMonth: { spent: usd("42.10"), limit: usd("200.00") },
  orgWindow: "month",
  costPerVerifiedTask: usd("0.61"),
  verifiedTasks: 62,
  spentOnFailedAttempts: usd("3.20"),
  runs: { total: 23, finished: 21, running: 1, blocked: 1 },
  unknownPriceRefusals: 0,
  breach: {
    budgetId: "run-127",
    scope: "Run #127",
    spent: usd("50.00"),
    limit: usd("50.00"),
    since: at("18:31:00"),
    ledgerEntry: "1012",
    nextCall: { actor: "Developer", model: "Claude Sonnet", wouldReach: usd("50.42") },
  },
};

export const MODEL_SPEND: ModelSpend[] = [
  { name: "Claude Sonnet", meta: "mid · 214 calls", cost: usd("21.40"), sharePercent: 51, selfHosted: false },
  { name: "GPT", meta: "large · 96 calls", cost: usd("9.80"), sharePercent: 23, selfHosted: false },
  { name: "Gemini Pro", meta: "large · 41 calls", cost: usd("7.10"), sharePercent: 17, selfHosted: false },
  { name: "Sandbox compute", meta: "test runs", cost: usd("3.80"), sharePercent: 9, selfHosted: false },
  { name: "Gemma", meta: "open-weight · self-hosted · 388 calls", cost: usd("0.00"), sharePercent: 0, selfHosted: true },
];

// ---- ledger ---------------------------------------------------------------------------

type Seed = [string, string, string, LedgerEntry["category"], string, string, string | null, string, string, [string, string][]];
const L: Seed[] = [
  ["1051", "19:42:10", "approval.requested", "decisions", "Developer", "merge to main, waiting for Owner", null, "T-3", "1050", [["Action", "merge"], ["Target", "hmac:9c1e…"], ["Rule", "merge: ask"]]],
  ["1050", "19:42:08", "task.started", "tasks", "Tester", "re-run suite in sandbox", null, "T-2", "1049", [["Sandbox", "podman, no network"]]],
  ["1049", "19:41:58", "gate.result", "gates", "Code reviewer", "independent review: passed", "0.06", "T-1", "1048", [["Verdict", "passed"], ["Verifier model", "gpt"], ["Saw", "task, done-def, output, canon"]]],
  ["1048", "19:41:57", "model.call", "spend", "Code reviewer", "gpt · 3,120 in / 410 out tokens", "0.06", "T-1", "1047", [["Price version", "p-2026-10-01"], ["Reserved", "$0.10"]]],
  ["1047", "19:41:51", "gate.result", "gates", "Gate: tests", "tests in sandbox: 48 passed", null, "T-1", "1046", [["Command", "hmac:44d0…"], ["Exit code", "0"], ["Duration", "9.4 s"]]],
  ["1046", "19:41:49", "gate.result", "gates", "Gate: lint", "rule check: passed", null, "T-1", "1045", [["Rule", "ruff lint + format"]]],
  ["1045", "19:41:40", "submission", "tasks", "Developer", "change submitted, +18 −4", null, "T-1", "1044", [["Attempt", "1 of 3"], ["Files", "2"]]],
  ["1044", "19:40:12", "model.settled", "spend", "Developer", "claude-sonnet settled", "0.31", "T-1", "1043", [["Reserved", "$0.50"], ["Released", "$0.19"]]],
  ["1043", "19:40:01", "model.reserved", "spend", "Developer", "reserve before call", "0.50", "T-1", "1042", [["Budget left", "$49.32 of $50.00"]]],
  ["1042", "19:39:30", "task.assigned", "tasks", "Chief of Staff", "fix webhook retries → Developer", null, "T-1", "1041", [["Policy", "delegate: allow"]]],
  ["1041", "19:39:22", "budget.checked", "spend", "Engine", "run budget $50.00 available", null, "—", "1040", [["Org month left", "$157.90"]]],
  ["1040", "19:39:20", "canon.loaded", "tasks", "Engine", "rules from commit a1b9f03", null, "—", "1039", [["Files", "12 blobs re-hashed"]]],
  ["1039", "19:39:18", "run.started", "tasks", "You", "run #128 started", null, "—", "1038", [["Template", "Dev team"]]],
];

/** The design's short hashes, by entry number: the first eight digits of each full hash. */
const SHORT: Record<string, string> = {
  "1051": "3f9a07c2",
  "1050": "b81d44e0",
  "1049": "6e2a91f3",
  "1048": "0d7c5b18",
  "1047": "e44f0a92",
  "1046": "a90b3c71",
  "1045": "5c3e8d26",
  "1044": "91f6b4a0",
  "1043": "2a7d1e5f",
  "1042": "c3b90f44",
  "1041": "7e18d2a6",
  "1040": "f05a3c91",
  "1039": "84c2e7b0",
  "1038": "d19e6a35",
};
const fullHash = (n: string): string => (SHORT[n] ?? "") + hash(`entry-${n}`).slice((SHORT[n] ?? "").length);

/** Newest first. Each entry's `prevHash` is the hash of the entry before it (the next in the array). */
export const LEDGER: LedgerEntry[] = L.map(([n, time, kind, category, actor, summary, cost, task, prev, extra]) => ({
  n,
  label: n,
  at: at(time),
  kind,
  category,
  actor,
  summary,
  cost: cost === null ? null : usd(cost),
  hash: fullHash(n),
  prevHash: fullHash(prev),
  stream: "run/128",
  taskId: task === "—" ? null : task,
  fields: extra.map(([label, value]) => ({ label, value })),
}));

LEDGER.push(
  {
    n: "0988", label: "0988", at: at("16:09:00", "2026-10-21"), kind: "approval.decided", category: "decisions", actor: "You",
    summary: "Spend over $20.00: approved", cost: null, hash: hash("entry-0988"), prevHash: hash("entry-0987"),
    stream: "run/121", taskId: null,
    fields: [{ label: "Outcome", value: "approved" }, { label: "Reason", value: "Large refactor, expected to cost more. Cap stays at $50." }],
  },
  {
    n: "0912", label: "0912", at: at("11:40:00", "2026-10-20"), kind: "approval.decided", category: "decisions", actor: "You",
    summary: "Send external email: denied", cost: null, hash: hash("entry-0912"), prevHash: hash("entry-0911"),
    stream: "run/119", taskId: null,
    fields: [{ label: "Outcome", value: "denied" }, { label: "Reason", value: "Agents do not email customers. Route through support." }],
  },
);

/** The entry number the next change gets. */
export const LEDGER_HEAD = 1051;
export const LEDGER_ANCHOR = { commit: "7d41e09", at: at("19:30:00") };
export const LEDGER_CHECKED_AT = at("19:42:10");

// ---- presets ----------------------------------------------------------------------------

export const POLICY_PRESETS: PolicyPreset[] = [
  {
    id: "read-only",
    name: "Read-only",
    description: "Can read and delegate; can't write, run or send anything.",
    rules: [
      { action: "delegate", decision: "allow", target: "*" },
      { action: "write_file", decision: "deny", target: "**" },
      { action: "run_command", decision: "deny", target: "*" },
      { action: "merge", decision: "deny", target: "*" },
    ],
    usedByRoles: ["reviewer"],
  },
  {
    id: "write-code-no-merge",
    name: "Write code, no merge",
    description: "May change files under src and tests; merging always asks.",
    rules: [
      { action: "write_file", decision: "allow", target: "src/**" },
      { action: "write_file", decision: "allow", target: "tests/**" },
      { action: "merge", decision: "ask", target: "*" },
    ],
    usedByRoles: ["developer"],
  },
  {
    id: "customer-facing-ask",
    name: "Customer-facing: ask before sending",
    description: "Anything that reaches a customer goes to a person first.",
    rules: [{ action: "external_message", decision: "ask", target: "*" }],
    usedByRoles: [],
  },
];

export const GATE_PRESETS: GatePreset[] = [
  {
    id: "python-tests-sandbox",
    name: "Python tests in sandbox",
    description: "Lint, then the test suite in the sandbox, then an independent review.",
    gates: [
      { id: "lint", kind: "rule", required: true },
      { id: "tests", kind: "test_command", required: true },
      { id: "review", kind: "verifier", required: true },
    ],
    evals: [],
    usedByRoles: ["developer"],
  },
  {
    id: "security-review",
    name: "Security review",
    description: "A rule check for secrets, then a security verifier and a human sign-off.",
    gates: [
      { id: "secrets", kind: "rule", required: true },
      { id: "security", kind: "verifier", required: true },
      { id: "signoff", kind: "human", required: false },
    ],
    evals: [],
    usedByRoles: [],
  },
];

// ---- templates --------------------------------------------------------------------------

export const TEMPLATE_SUMMARIES: TemplateSummary[] = [
  {
    id: "dev-team",
    name: "Dev team",
    description:
      "Fixes issues end to end: plan, code, test in the sandbox, independent review, then a human approves the merge. Ships with a sample canon and a reference run.",
    availability: "available",
    origin: "built-in",
    roles: ["Chief of Staff", "Developer", "Tester", "Code reviewer"],
    meta: "4 roles · 5 gates · $50 per run",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "code-review",
    name: "Code review",
    description:
      "Reviews pull requests against your coding rules, with a security pass and a verifier from a different model family.",
    availability: "available",
    origin: "built-in",
    roles: ["Review lead", "Code reviewer", "Security reviewer"],
    meta: "3 roles · 3 gates · $10 per pull request",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "scrum",
    name: "Scrum team",
    description: "A product owner, a scrum master, two developers and a tester run a sprint; an independent reviewer checks every story.",
    availability: "available",
    origin: "built-in",
    roles: ["Product owner", "Scrum master", "Developer", "Tester", "Independent reviewer"],
    meta: "5 roles · 3 gates · $5 per developer",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "kanban",
    name: "Kanban team",
    description: "Cards flow one at a time from doing to testing to done, with an independent reviewer on every card.",
    availability: "available",
    origin: "built-in",
    roles: ["Flow lead", "Developer", "Tester", "Independent reviewer"],
    meta: "4 roles · 3 gates · one card at a time",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "review-board",
    name: "Review board",
    description: "A change summarizer and three independent reviewers on different models: correctness, security and maintainability. The packaged team needs all three to pass a change; the app form has a rule gate and one verifier gate, both on the change summarizer.",
    availability: "available",
    origin: "built-in",
    roles: ["Chair", "Change summarizer", "Correctness reviewer", "Security reviewer", "Maintainability reviewer"],
    meta: "5 roles · 2 gates · 3 reviewer models",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "support",
    name: "Support team",
    description: "A support lead, a triager and a responder handle each ticket; an independent reply checker verifies the triage. No customer message goes out without a human.",
    availability: "available",
    origin: "built-in",
    roles: ["Support lead", "Triager", "Responder", "Reply checker"],
    meta: "4 roles · 3 gates · $5 per worker",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "sales",
    name: "Sales team",
    description: "A sales lead, a qualifier and an outreach writer work each lead; an independent claims checker verifies the qualification. No message goes out and the CRM is never written without a human.",
    availability: "available",
    origin: "built-in",
    roles: ["Sales lead", "Qualifier", "Outreach writer", "Claims checker"],
    meta: "4 roles · 3 gates · $5 per worker",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "qa",
    name: "QA and testing team",
    description: "A QA lead, a test designer and a test runner cover each feature; an independent test reviewer checks the test plan. The runner may only run tests.",
    availability: "available",
    origin: "built-in",
    roles: ["QA lead", "Test designer", "Test runner", "Test reviewer"],
    meta: "4 roles · 3 gates · $5 per worker",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "devops",
    name: "DevOps team",
    description: "An ops lead, a change planner and a staging checker handle each change; an independent change reviewer verifies the plan. A deploy or infrastructure change always asks a human.",
    availability: "available",
    origin: "built-in",
    roles: ["Ops lead", "Change planner", "Staging checker", "Change reviewer"],
    meta: "4 roles · 3 gates · $5 per worker",
    latestVersion: null,
    updatedAt: null,
  },
  {
    id: "blank",
    name: "Blank organization",
    description: "An empty canvas with default-deny policies and an org budget, ready for your own roles.",
    availability: "available",
    origin: "built-in",
    roles: ["Owner"],
    meta: "Start from nothing",
    latestVersion: null,
    updatedAt: null,
  },
];
