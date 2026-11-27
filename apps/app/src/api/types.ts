/**
 * The data the app's screens use (SPEC-app-v1 §4). Names are camelCase and values are plain data;
 * the server client maps the REST API's snake_case JSON to these shapes, so screens never change
 * when the data source does.
 *
 * Money is a decimal **string** with a currency, never a JavaScript number: `money.ts` does the
 * arithmetic in integers.
 */
import type { Org } from "@/model/org";

export interface Money {
  /** Decimal text such as "42.10" (up to 6 places, as the ledger keeps them). */
  amount: string;
  /** The server's currency code; the app only does arithmetic on one currency at a time. */
  currency: string;
}

/** Any change the app makes returns the ledger entry that recorded it, when there is one. */
export interface Mutation<T> {
  result: T;
  /** The ledger entry number, such as "1052"; null when the change isn't a ledger event. */
  ledgerEntry: string | null;
}

// ---- organization and configuration --------------------------------------------------------

export interface OrgSnapshot {
  org: Org;
  /** A hash of the organization (the server's is SHA-256), so a stale edit can be refused. */
  versionHash: string;
  configFile: string;
  gitRef: string;
}

export interface ConfigIssue {
  path: string;
  message: string;
  severity: "error" | "warning";
}

export interface ValidationResult {
  valid: boolean;
  issues: ConfigIssue[];
}

export type ExportFormat = "json" | "yaml";

// ---- presets and templates -------------------------------------------------------------------

export type Decision = "allow" | "ask" | "deny";
export type ActionKind =
  | "delegate"
  | "call_model"
  | "use_tool"
  | "run_command"
  | "write_file"
  | "spend"
  | "external_message"
  | "merge"
  | "network";

export interface PolicyRule {
  action: ActionKind;
  decision: Decision;
  target: string;
}

export interface PolicyPreset {
  id: string;
  name: string;
  description: string;
  rules: PolicyRule[];
  /** Roles whose agents use this preset: editing it changes all of them. */
  usedByRoles: string[];
}

export interface GatePresetGate {
  id: string;
  kind: "rule" | "test_command" | "verifier" | "human" | "eval";
  required: boolean;
  /** Kind-specific settings (the core's `GateTemplate.config`); absent means none. */
  config?: Record<string, unknown>;
}

export interface GatePreset {
  id: string;
  name: string;
  description: string;
  /** Cheapest first. */
  gates: GatePresetGate[];
  evals: string[];
  usedByRoles: string[];
}

export interface PresetSet {
  policy: PolicyPreset[];
  gate: GatePreset[];
}

export interface TemplateSummary {
  id: string;
  name: string;
  description: string;
  availability: "available" | "planned";
  origin: "built-in" | "mine";
  roles: string[];
  meta: string;
  /** Saved templates only. */
  latestVersion: number | null;
  updatedAt: string | null;
}

export interface TemplateDetail extends TemplateSummary {
  versions: TemplateVersionInfo[];
}

export interface TemplateVersionInfo {
  templateId: string;
  version: number;
  name: string;
  description: string;
  savedAt: string;
  documentHash: string;
}

// ---- runs --------------------------------------------------------------------------------

export type RunStatus =
  | "running"
  | "stopping"
  | "waiting"
  | "paused"
  | "blocked"
  | "done"
  | "failed"
  | "stopped"
  /** A state this app does not know: shown as "Unknown", never as active or successful. */
  | "unknown";
export type NodeStatus = "done" | "running" | "waiting" | "idle";
export type EventCategory = "tasks" | "gates" | "spend" | "approvals";
export type Tone = "accent" | "warm" | "muted";

export interface RunSummary {
  id: string;
  /** "#128" */
  label: string;
  /** What the run is doing, for the page heading. */
  title: string;
  status: RunStatus;
  template: string;
  /** Null when the server doesn't say. */
  startedAt: string | null;
  startedBy: string;
  tasks: { done: number; total: number };
  gates: { passed: number; failed: number; total: number };
  /** `limit` is null when the source doesn't say (the server lists runs without their budgets). */
  spend: { spent: Money; limit: Money | null };
  /** True once the run has ended (nothing will change any more). Sources that don't say leave it out. */
  ended?: boolean;
  /** Who asked the run to stop, while it is `stopping` or after it was stopped. */
  stopRequestedBy?: string | null;
  /** Fixed words shown instead of the reason when it could not be kept ("reason withheld: org key destroyed"). */
  stopReasonWithheld?: string | null;
  /** The organization the run belongs to (server data). */
  org?: string;
}

export interface GraphNode {
  id: string;
  name: string;
  abbr: string;
  status: NodeStatus;
  statusText: string;
  isHuman: boolean;
  isVerifier: boolean;
}

export interface GraphEdge {
  from: string;
  to: string;
  kind: "reports" | "checks" | "approval";
  state: "done" | "active" | "idle";
}

export interface GateResultCard {
  gate: string;
  verdict: "passed" | "failed";
  criteria: { text: string; met: boolean }[];
  /** `null` when the source doesn't record it (the server's ledger keeps no model name per gate). */
  verifierModel: string | null;
  /** Verifier and worker models are from different families (D.4); null when unknown. */
  differentFamily: boolean | null;
  saw: string[] | null;
  notShared: string[] | null;
  cost: Money | null;
  ledgerEntry: string;
  /** Who decided: `gate:rule`, `gate:test_command`, `agent:<verifier>` or `human:<id>` (server data). */
  checkedBy?: string;
}

/** One gate outcome of a task, with the verifier's per-criterion verdicts (server data). */
export interface GateOutcome {
  gate: string;
  kind: string;
  required: boolean;
  outcome: string;
  checkedBy: string;
  attempt: number;
  /** The submission of the round this result belongs to; absent when the source doesn't say. */
  submission?: number;
  ledgerEntry: string;
  verdicts: { criterion: string; text: string; met: boolean }[];
}

/** A human gate of a task and the criteria it checks: a decision covers exactly these. */
export interface HumanGate {
  id: string;
  checks: string[];
}

/** A task of a live run with its attempts, gates and spend (server data). */
export interface RunTask {
  id: string;
  title: string;
  assignee: string;
  status: string;
  attempt: number;
  maxAttempts: number;
  note: string;
  gates: GateOutcome[];
  spend: Money;
  calls: number;
  /** The round a person would decide (server data); absent when the source doesn't say. */
  submission?: number;
  /** The criteria of the task's definition of done (a human gate checks some of them). */
  criteria?: { id: string; text: string }[];
  /** The task's human gates, each with the criteria it checks. */
  humanGates?: HumanGate[];
}

/** A budget stop or overspend a run hit, as the ledger recorded it. */
export interface BudgetAlert {
  kind: "hard_stop" | "limit_passed";
  ledgerEntry: string;
  budget: string;
  scope: string;
  window: BudgetWindow;
  limit: Money | null;
  /** What the budget had spent when the alert was recorded, if the server says. */
  spent?: Money | null;
  /** When a windowed budget starts fresh; null when only a person can lift it. */
  resetsAt: string | null;
  /** Overspent in a window that had already ended: nothing is blocked, a person is told to review it. */
  windowClosed?: boolean;
  /** True when the block stays until a person lifts it ("hold"). */
  held: boolean;
}

/** What the run's ledger stream says about itself. */
export interface RunLedgerState {
  stream: string;
  entries: number;
  chainOk: boolean;
}

export interface RunDetail extends RunSummary {
  canonRef: string;
  sandbox: string;
  /** Null when the start or end time isn't recorded. */
  elapsedSeconds: number | null;
  needsYou: number;
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  latestGate: GateResultCard | null;
  /** Server data only: the tasks with their gates, and what the run spent where. */
  taskList?: RunTask[];
  spendByModel?: { name: string; spend: Money; calls: number }[];
  spendByAgent?: { name: string; spend: Money; calls: number }[];
  unknownCostCalls?: number;
  /** Served calls whose cost is unknown (the reservation was charged); never a no-price call, those are blocked. */
  costUnknownCalls?: number;
  /** The known total only (a charged reservation is an upper bound, never added), as the server computed it; null when every cost is known. */
  atLeast?: Money | null;
  budgetAlerts?: BudgetAlert[];
  ledger?: RunLedgerState;
}

export interface RunEvent {
  /** Increases with time within a run; use it as the cursor for `runs.events`. */
  seq: number;
  at: string;
  kind: string;
  category: EventCategory;
  text: string;
  tone: Tone;
  /** The ledger entry's id as `entryHref` takes it (`run:<id>@<seq>`); absent on sample events. */
  entry?: string;
  /** The task the entry belongs to, or null for a run-level entry; absent on sample events. */
  taskId?: string | null;
}

/** How the live connection of a run is doing. */
/** `unreachable`: several attempts in a row never received anything (wrong address, or the server refuses this origin). */
export type LiveStatus = "connecting" | "live" | "reconnecting" | "unreachable" | "closed";

export interface StartRunInput {
  template: string;
  budget: Money;
}

/**
 * A run plan as `POST /v1/runs` takes it (the API notes): `data_class` and `tasks`. The app sends the
 * object as written; the server checks every field and refuses what it does not know.
 */
export type RunPlan = Record<string, unknown>;

/** The server's answer to an accepted plan: the run exists as a request; a runner begins or refuses it. */
export interface StartedRun {
  runId: string;
  /** `already_requested`: the same request was sent before (same run). */
  state: "accepted" | "already_requested";
  engine: string;
}

/**
 * Where a start request stands (`GET /v1/runs/{id}`): `requested` until a runner looks at it, then
 * `begun` (it is a run, with a page of its own) or `refused` with the runner's code.
 */
export interface StartStatus {
  state: "requested" | "begun" | "refused";
  refusalCode: string | null;
}

// ---- approvals ---------------------------------------------------------------------------

export type ApprovalState = "pending" | "approved" | "denied";

export interface Approval {
  id: string;
  title: string;
  requester: string;
  /**
   * The server says the signed-in caller is the run's originator: they started it in a session or with an API token
   * they issued (SA-16 `caller_terms.caller_is_originator`, #1458). Display only (SA-17 "You started this run.");
   * false or absent when the server does not say so. Never a reason to allow or refuse anything.
   */
  callerIsOriginator?: boolean;
  /** What the agent asked to do; the page builds its plain sentence from it (AE-5). */
  action?: { kind: string; target: string | null; amount: Money | null };
  runId: string;
  runLabel: string;
  /** The task the request is for (R1.27 G6), or null; the page links it to the task's audit trail. */
  taskId?: string | null;
  requestedAt: string;
  state: ApprovalState;
  /** What happens if the request is approved. */
  effect: string;
  rule: string;
  runSpend: { spent: Money; limit: Money | null } | null;
  evidence: { state: "ok" | "wait"; text: string }[];
  /** Set once decided. */
  decision: null | { by: string; at: string; reasonHash: string | null; ledgerEntry: string };
  /**
   * Solo approval (SA-5, SA-16): set only when the signed-in caller is the sole decider of a pending request.
   * `freshProofNeeded`: an approve sent now would be refused until a fresh proof is given (SA-7).
   * `proofs`: what the server accepts as that proof (`password`, `sign_in`, `mfa`; empty when nothing helps).
   */
  solo?: { actionDigest: string; freshProofNeeded: boolean; proofs?: string[] } | null;
  /** The requesting agent's role, for the solo confirm dialog (SA-17); null or absent when the source doesn't say. */
  role?: string | null;
  /** True when the run was started by the signed-in person themselves (SA-17: "You started this run."). */
  startedByYou?: boolean;
}

export interface DecideInput {
  decision: "approve" | "reject";
  /** Required: an empty reason is refused. */
  reason: string;
  /**
   * Set only by the confirm of the solo approve dialog (`approvals/solo-approve-dialog.tsx`, SA-17): the digest of
   * the action that dialog showed (SA-5). With an approve it is sent as `acknowledge_solo: true` plus
   * `action_digest`; a reject never sends either (SA-16).
   */
  solo?: { actionDigest: string };
}

// ---- budgets and spend -------------------------------------------------------------------

export type BudgetStatus = "ok" | "near" | "blocked";
/** How long a budget's balance lives: for ever, one UTC day, or one UTC month (T56). */
export type BudgetWindow = "none" | "day" | "month";
/** What a breach does at the next window: `reset` clears by itself, `hold` waits for a person. */
export type BreachMode = "reset" | "hold";

export interface BudgetRow {
  id: string;
  scope: string;
  period: string;
  limit: Money;
  spent: Money;
  /** Peak, for per-task budgets. */
  spentIsPeak: boolean;
  /** 0–100, for the bar. */
  percentUsed: number;
  status: BudgetStatus;
  window: BudgetWindow;
  onBreach: BreachMode;
  /** Start of the current window (UTC); null for a lifetime budget. */
  windowStart: string | null;
  /** When the current window ends and a fresh one starts; null for a lifetime budget. */
  resetsAt: string | null;
  /** What can still be spent (limit minus spent and reserved). */
  available: Money | null;
}

export interface BudgetSummary {
  /** The organization's budget in its current window (a month, a day or for good); null if none. */
  orgMonth: { spent: Money; limit: Money } | null;
  /** The window `orgMonth` is measured in. */
  orgWindow: BudgetWindow;
  costPerVerifiedTask: Money;
  spentOnFailedAttempts: Money;
  /** Tasks the cost per verified task is averaged over. */
  verifiedTasks: number;
  runs: { total: number; finished: number; running: number; blocked: number };
  /** Calls refused this month because a price was unknown (they are blocked, never guessed). */
  unknownPriceRefusals: number | null;
  /** What the verified-task averages cover, when it is not everything ("the newest 50 runs"). */
  coverage?: string;
  /** Calls recorded with no cost because their price was unknown (server data). */
  unknownCostCalls?: number;
  breach: null | {
    budgetId: string;
    scope: string;
    spent: Money;
    limit: Money;
    /** When the breach was recorded; null when the server's budget history doesn't say. */
    since: string | null;
    /** The ledger entry that recorded the breach ("" when the server doesn't name it). */
    ledgerEntry: string;
    /** What the refused call would have reached; null when the ledger doesn't say. */
    nextCall: { actor: string; model: string; wouldReach: Money } | null;
    window?: BudgetWindow;
    onBreach?: BreachMode;
    /** When the block clears by itself (`reset` windows); null otherwise. */
    resetsAt?: string | null;
  };
}

export interface ModelSpend {
  name: string;
  meta: string;
  cost: Money;
  /** 0–100 of the month's spend. */
  sharePercent: number;
  selfHosted: boolean;
}

// ---- ledger --------------------------------------------------------------------------------

export type LedgerCategory = "tasks" | "gates" | "spend" | "decisions";

export interface LedgerEntry {
  /** The entry's id in the URL (the fixtures' "1051"; the server's `stream@seq`). */
  n: string;
  /** What is shown after the `#`: "1051" or the entry's number within its stream. */
  label: string;
  at: string;
  kind: string;
  category: LedgerCategory;
  actor: string;
  summary: string;
  cost: Money | null;
  hash: string;
  prevHash: string;
  stream: string;
  taskId: string | null;
  /** Further fields for the detail panel; hashed targets stay hashes, never content (S2). */
  fields: { label: string; value: string }[];
}

export interface LedgerFilter {
  category?: LedgerCategory;
  search?: string;
}

/** The anchor state of one stream: "verified" needs signed anchors that check out, never just an intact chain. */
export type AnchorState = "anchored" | "unanchored" | "problems" | "not_configured" | "unreadable";

export interface StreamCheck {
  stream: string;
  chainOk: boolean;
  entries: number;
  anchor: AnchorState;
  unanchoredEvents: number;
  /** Why the hash chain failed (the server's code, with the entry number when it says), or null if it is intact. */
  chainProblem: string | null;
  /** What each anchor check found wrong: the server's code and sentence. */
  anchorProblems: { code: string; detail: string }[];
}

export interface ChainStatus {
  /** True only when the chains are intact AND signed anchors check out (never for "unanchored"). */
  verified: boolean;
  /** Chain intact? (separate from anchoring). Sources that don't say leave it out; then `verified` stands for it. */
  chainOk?: boolean;
  /** Overall anchor state; absent for sources without anchors. */
  anchorState?: AnchorState;
  streams?: StreamCheck[];
  /** More streams exist than this check covered. */
  truncated?: boolean;
  entries: number;
  gaps: number;
  /** Where the head is anchored in Git (ND22); null when no anchor exists yet. */
  anchor: null | { commit: string; at: string };
  checkedAt: string;
}

export interface SpendFilterMonth {
  month: string;
}

// ---- organization safety: halt, role pause, circuit breakers (E19, ND106) --------------------

/** Why an organization was halted. `null` when it is not. */
export type HaltCause = "human" | "breaker" | "evaluation_unavailable";

/** One role's pause state. `inConfig` is false for a role that was removed from the config while paused. */
export interface OrgRole {
  role: string;
  paused: boolean;
  by: string | null;
  at: string | null;
  cause: "human" | "breaker" | null;
  rule: string | null;
  ledgerSeq: number | null;
  inConfig: boolean;
}

/** The organization's halt state (`GET /v1/org/status`). `canAct` is the app's switch for showing buttons. */
export interface OrgStatus {
  halted: boolean;
  by: string | null;
  at: string | null;
  cause: HaltCause | null;
  rule: string | null;
  ledgerSeq: number | null;
  /** Diagnosis of an `evaluation_unavailable` halt: `read`, `evaluate`, `write` or `timeout`. */
  phase: string | null;
  errorClass: string | null;
  failingRule: string | null;
  failedTicks: number | null;
  roles: OrgRole[];
  canAct: boolean;
  /** Solo approvals of the last 30 days made within 7 days of an approver being removed (SA-18); 0 when the source doesn't say. */
  soloAfterRemoval: number;
  /** True when exactly one person can decide in this organization (SA-18 item 4); absent when the source doesn't say. */
  soloNow?: boolean;
}

/** A person's verdict on a human gate (SA-19). `acknowledgeSolo` is sent only by the organization's sole decider. */
export interface GateDecideInput {
  /** The round the person looked at. */
  attempt: number;
  submission: number;
  /** One finding per criterion the gate checks, no more and no fewer; a reason is required for each. */
  findings: Record<string, { approved: boolean; reason: string }>;
  acknowledgeSolo?: boolean;
}

export interface GateDecision {
  outcome: string;
  /** True when the server had already recorded this decision (a repeat). */
  alreadyRecorded: boolean;
}

/** One proposal item of `GET /v1/autonomy/proposals` (SA-18 item 2). */
export interface ProposalItem {
  subject: { kind: "agent" | "role"; id: string };
  ruleKey: number;
  role: string;
  /** The action kind the rule covers, or null. */
  kind: string | null;
  /** The server's word: `proposable` (can be granted), `blocked`, `not_enough_evidence`, `ceiling`, `already_granted`, `never_grantable` or `no_such_rule`. */
  status: string;
  /** The server's reason codes, in its order (empty for `proposable`). */
  reasons: string[];
  /** The approved asks counted as evidence; null for a cell that counts nothing (`never_grantable`, `no_such_rule`). */
  approvedAsks: number | null;
  /** How many of the approved asks were solo decisions, and their share (a 4-place decimal text, null with no approved ask). */
  solo: { approvedAsksSolo: number; soloShare: string | null };
  evidenceHash: string;
}

export interface ProposalList {
  items: ProposalItem[];
  ledgerPosition: number;
  asOf: string;
}

/** What a person read when they grant (the server refuses a grant if the evidence moved). */
export interface GrantInput {
  subject: { kind: "agent" | "role"; id: string };
  ruleKey: number;
  asOf: string;
  ledgerPosition: number;
  evidenceHash: string;
  reason: string;
  expiresInDays?: number;
}

export type BreakerSignal = "spend" | "policy_denied" | "gate_failed" | "model_blocked" | "model_called";
export type BreakerState = "ok" | "tripped" | "disabled";
export type BreakerAction = "halt_org" | "pause_role";

/** The four reason codes for disabling a breaker (spec 6.1). */
export const DISABLE_REASONS = ["false_positive", "planned_work", "rule_being_replaced", "other"] as const;
export type DisableReason = (typeof DISABLE_REASONS)[number];

export interface BreakerRow {
  name: string;
  /** Null only for a disabled rule that was removed from the config (nothing left to compute). */
  signal: BreakerSignal | null;
  action: BreakerAction | null;
  role: string | null;
  windowSeconds: number | null;
  dryRun: boolean;
  /** Decimal strings, exactly as the server sent them. */
  limit: string | null;
  observed: string | null;
  observedAs: "live" | "at_trip" | null;
  unknownCostCalls: number;
  windowStart: string | null;
  state: BreakerState;
  trippedAt: string | null;
  trippedSeq: number | null;
  trippedBy: string | null;
  manual: boolean;
  lastResetAt: string | null;
  disabledAt: string | null;
  disabledBy: string | null;
  disabledReason: DisableReason | null;
  inConfig: boolean;
}

export type EvaluationState = "not_configured" | "starting" | "ok" | "failing" | "stopped";

export interface BreakerEvaluation {
  state: EvaluationState;
  tickSeconds: number;
  haltAfterFailedTicks: number;
  lastEvaluatedAt: string | null;
  lastErrorAt: string | null;
  lastErrorPhase: string | null;
  lastErrorClass: string | null;
  lastFailingRule: string | null;
  consecutiveFailures: number;
}

export interface BreakersView {
  org: string;
  halt: OrgStatus;
  evaluation: BreakerEvaluation;
  breakers: BreakerRow[];
}

export interface BreakerResetResult {
  name: string;
  state: "ok";
  resetAt: string;
  ledgerSeq: number;
  /** The halt state after the reset: a reset never resumes the organization. */
  orgHalted: boolean;
}

export interface BreakerDisableResult {
  name: string;
  state: "disabled";
  disabledAt: string;
  reason: DisableReason;
  ledgerSeq: number;
}

export interface BreakerEnableResult {
  name: string;
  state: "ok";
  enabledAt: string;
  ledgerSeq: number;
}

/** What a credential may do: read the organization's runs, and/or ask a run it takes part in to stop. */
export type AgentScope = "read" | "run";

/** One agent credential as the server lists it (E2): never the secret, never the hash. */
export interface AgentCredential {
  id: string;
  agent: string;
  scopes: AgentScope[];
  state: "active" | "expired" | "revoked";
  createdAt: string;
  expiresAt: string;
  revokedAt: string | null;
  /** The person who made it was erased (Q4): the credential still works, an owner should re-own or revoke it. */
  issuerErased: boolean;
  /** The agent is in the server's configuration. */
  inConfig: boolean;
}

/** The caller's role in the organization (people only; `null` where the app cannot tell). */
export type CallerRole = "owner" | "approver" | "viewer";

/** One agent of the team for the Agents page (E2): no fingerprint, only what the screen shows. */
export interface AgentSummary {
  id: string;
  name: string;
  role: string;
  /** Credentials that work now. */
  activeCredentials: number;
}

/** A credential just made: `secret` is shown once and never read again. */
export interface IssuedAgentCredential extends AgentCredential {
  secret: string;
}

/** One spend figure of the audit page, with the split of calls that have no price (R1.27). */
export interface AuditSpend {
  total: Money;
  calls: number;
  /** Calls whose price was not known although an answer came back. */
  priceUnknownCalls: number;
  /** Defensive: a cost in another currency is counted, never added (0 today). */
  otherCurrencyCalls: number;
  /** Calls that failed and carry no cost. */
  failedCallsNoCost: number;
  unknownCostCalls: number;
  /** Served calls of unknown cost (the reservation was charged). */
  costUnknownCalls: number;
  /** The known total only (server field; the reservation charge is never added); null when every cost is known. */
  atLeast: Money | null;
}

export interface AuditDecision {
  seq: number;
  at: string;
  actor: string;
  kind: string;
  /** Only the fields the server gave for this kind (never free text). */
  fields: Record<string, string | number | boolean | null>;
}

export interface AuditRework {
  seq: number;
  at: string;
  from: string | null;
  to: string | null;
  attemptBefore: number | null;
  attemptAfter: number | null;
  maxAttempts: number | null;
  by: "gate" | "person" | "other";
  /** A gate's own wording; null for a person or any other actor (R12). */
  reason: string | null;
  reasonHash: string | null;
  feedbackHash: string | null;
  actor: string;
}

export interface AuditApproval {
  id: string;
  agent: string;
  status: string;
  askedBecause: string;
  decidedBy: string | null;
  decidedAt: string | null;
  reasonHash: string | null;
  reasonAvailable: boolean;
}

export interface AuditAttempt {
  attempt: number;
  submission: number;
  gates: GateOutcome[];
  movedTo: string | null;
  reason: string | null;
  reasonHash: string | null;
}

/** Everything that happened to one task, in order, with evidence (R1.27). */
export interface TaskAudit {
  runId: string;
  task: { id: string; title: string; assignee: string | null; status: string; attempt: number; maxAttempts: number };
  chain: { ok: boolean; headSeq: number; head: string };
  truncated: boolean;
  totalEntries: number;
  entries: LedgerEntry[];
  attempts: AuditAttempt[];
  spend: AuditSpend;
  spendByAttempt: (AuditSpend & { attempt: number })[];
  approvals: AuditApproval[];
  decisions: AuditDecision[];
  rework: AuditRework[];
}
