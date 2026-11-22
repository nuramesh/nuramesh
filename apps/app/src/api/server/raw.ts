/**
 * The shapes the `nuramesh serve` API returns (docs/api.md), as far as the app reads them. Every
 * field the app doesn't need is left out; fields a newer server adds are ignored.
 */
export interface RawMoney {
  amount: string;
  currency: string;
}

export interface RawSpend {
  total: RawMoney;
  calls: number;
  unknown_cost_calls?: number;
  /** Served calls of unknown cost (the reservation was charged), and the known total only (spec default-prices 6.2). */
  cost_unknown_calls?: number;
  at_least?: RawMoney | null;
  by_agent?: { agent: string; total: RawMoney; calls: number }[];
  by_model?: { model: string; total: RawMoney; calls: number }[];
}

export interface RawRunSummary {
  run_id: string;
  status: string;
  org: string | null;
  started_at: string | null;
  started_by: string | null;
  ended_at: string | null;
  tasks: { total: number; by_status: Record<string, number> };
  gates: { total: number; passed: number; failed: number; error: number };
  spend: RawSpend;
  stop: { requested_by?: string | null; stop_reason_withheld?: string | null } | null;
  events: number;
  head: string;
  updated: string | null;
}

export interface RawRunList {
  runs: RawRunSummary[];
  truncated: boolean;
  scanned: number;
}

export interface RawVerdict {
  criterion: string;
  text?: string;
  verdict: string;
}

export interface RawGate {
  gate: string;
  kind: string;
  required: boolean;
  outcome: string;
  checked_by: string;
  ledger_seq: number;
  at: string;
  verdicts: RawVerdict[];
}

export interface RawTask {
  id: string;
  title: string;
  assignee: string | null;
  status: string;
  attempt: number;
  max_attempts: number;
  note?: string | null;
  attempts: { attempt: number; submission?: number; gates: RawGate[] }[];
  latest_gates: RawGate[];
  spend: { total: RawMoney; calls: number };
  submission?: number;
  /** `checks` are the criteria a gate decides: a human decision covers exactly its gate's checks. */
  definition_of_done?: { criteria: { id: string; text: string }[]; gates: { id: string; kind: string; required: boolean; checks?: string[] }[] };
}

/** One item of `GET /v1/autonomy/proposals`. */
export interface RawProposalItem {
  subject: { kind: "agent" | "role"; id: string };
  cell: { role: string; rule_key: number };
  kind: string | null;
  /** `proposable`, `blocked`, `not_enough_evidence`, `ceiling`, `already_granted`, `never_grantable` or `no_such_rule`. */
  status: string;
  /** Reason codes (`core/proposals.py` `REASON_ORDER`), in the server's order. */
  reasons: string[];
  /** The counted evidence; a `never_grantable` or `no_such_rule` cell has no counts. */
  evidence: { approved_asks?: number } & Record<string, unknown>;
  evidence_hash: string;
  solo: { approved_asks_solo: number; solo_share: string | null };
}

export interface RawProposals {
  items: RawProposalItem[];
  ledger_position: number;
  as_of: string;
}

export interface RawBudget {
  id: string;
  scope: string;
  scope_id: string;
  limit: RawMoney;
  spent: RawMoney;
  reserved: RawMoney;
  available: RawMoney;
  blocked: boolean;
  window: string;
  on_breach: string;
  window_start: string | null;
  version: number;
  history?: { ledger_seq: number; type: string; at: string; actor: string; payload: Record<string, unknown> }[];
  history_truncated?: boolean;
}

export interface RawBudgetAlert {
  kind: string;
  ledger_seq: number;
  budget: string;
  scope: string;
  window: string | null;
  /** The real server sends a plain decimal text here (docs/api.md shows a money object); both are read. */
  limit: RawMoney | string | null;
  spent?: RawMoney | string | null;
  over_by?: RawMoney | string | null;
  on_breach?: string | null;
  window_start?: string | null;
  resets_at?: string | null;
  /** True when the budget was overspent in a window that had already ended (nothing is blocked). */
  window_closed?: boolean;
  held: boolean;
}

export interface RawApproval {
  id: string;
  run_id: string;
  task_id: string | null;
  agent_id: string;
  action: { kind: string; target: string | null; amount: RawMoney | null };
  asked_because: string;
  status: string;
  requested_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason_hash: string | null;
  decision_reason: string | null;
  ledger?: { ledger_seq: number; type: string; at: string; actor: string }[];
  reason_available?: boolean;
  /** SHA-256 of the action a solo approver confirms (SA-5); null on old rows. */
  action_digest?: string | null;
  /**
   * What an approve sent now would be treated as, for the caller (SA-16); null when not pending or not a cookie session.
   * `caller_is_originator` (#1458): the caller started the run, in a session or with an API token they issued; display only.
   */
  caller_terms?: {
    mode: "refused" | "solo" | "peer" | "no_originator";
    fresh_proof_needed: boolean;
    proofs: string[];
    caller_is_originator?: boolean;
  } | null;
}

export interface RawRunDetail extends Omit<RawRunSummary, "tasks"> {
  task_counts: RawRunSummary["tasks"];
  chief_of_staff: string | null;
  canon_commit: string | null;
  ledger: { stream: string; seq: number; head: string; ok: boolean };
  tasks: RawTask[];
  budgets: RawBudget[];
  budget_alerts: RawBudgetAlert[];
  approvals: RawApproval[];
}

export interface RawEvent {
  stream: string;
  seq: number;
  type: string;
  actor: string;
  at: string;
  run_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
  /** Names of the keys the server hid (R4); absent when it hid nothing. */
  redacted?: string[];
  cost: RawMoney | null;
  cost_basis: string | null;
  hash: string;
  prev_hash: string;
}

export interface RawLedgerPage {
  stream: string;
  events: RawEvent[];
  head_seq: number;
  head: string;
  next_after_seq: number;
  has_more?: boolean;
}

export interface RawAnchors {
  status?: string;
  anchored?: boolean;
  count?: number;
  unanchored_events?: number;
  /** What the server sends: a code (`head_mismatch`, `stream_shorter`, `bad_signature`...) and a sentence. */
  problems?: { code: string; detail: string }[];
}

export interface RawStreamCheck {
  stream: string;
  ok: boolean;
  checked: number;
  /** The hash-chain problem code (`bad_hash`, `broken_link`...), null when the chain is intact. */
  problem: string | null;
  at_seq?: number | null;
  anchors: RawAnchors | null;
}

/** The anchors object: a stream's own state, or (at the top level) whether anchors are configured. */
export type RawAnchorInfo = RawAnchors & { configured?: boolean; checked?: boolean; error?: string };

export interface RawVerify {
  ok: boolean;
  chain_ok: boolean;
  verified: boolean;
  streams?: RawStreamCheck[];
  /** Many streams: `{configured, checked}`. The single-stream endpoint puts the stream's own anchors here. */
  anchors?: RawAnchorInfo | null;
  /** Single-stream endpoint only: `{configured, checked}`. */
  anchor_status?: RawAnchorInfo;
  truncated?: boolean;
  next_after?: string | null;
  stream?: string;
  checked?: number;
  problem?: string | null;
  at_seq?: number | null;
}

export interface RawStopResponse {
  run_id: string;
  status: string;
  already_requested: boolean;
  requested_by: string;
  requested_at: string;
  ledger_seq: number;
  /** Fixed words, present only when the stop's reason could not be kept (R1.37). */
  stop_reason_withheld?: string | null;
}

export interface RawHealth {
  status: string;
  streams: number;
  org: string | null;
  humans_enabled: boolean;
}

// ---- organization safety (E19, spec/e19/README.md section 11) ---------------------------------

export interface RawOrgRole {
  role: string;
  paused: boolean;
  by: string | null;
  at: string | null;
  cause: "human" | "breaker" | null;
  rule: string | null;
  ledger_seq: number | null;
  in_config: boolean;
}

/** The `halt` object; the halt and role POSTs add `stopped_runs` / `stopped_tasks`. */
export interface RawHalt {
  halted: boolean;
  by: string | null;
  at: string | null;
  cause: "human" | "breaker" | "evaluation_unavailable" | null;
  rule: string | null;
  ledger_seq: number | null;
  phase: string | null;
  error_class: string | null;
  failing_rule: string | null;
  failed_ticks: number | null;
  roles: RawOrgRole[];
  can_act?: boolean;
  stopped_runs?: number;
  stopped_tasks?: number;
  /** Only on the status read (SA-18 item 4); the halt and role POSTs don't carry it. */
  solo_approval?: RawSoloApproval;
}

/** `solo_approval` of the organization status: 30-day counts of approved requests (SA-18 item 4). */
export interface RawSoloApproval {
  solo_now: boolean;
  limit_per_hour: number;
  window_days: number;
  approved: number;
  approved_solo: number;
  solo_share: string | null;
  approved_solo_after_removal: number;
}

export interface RawBreaker {
  name: string;
  signal: string | null;
  action: string | null;
  role: string | null;
  window_seconds: number | null;
  dry_run: boolean | null;
  limit: string | null;
  observed: string | null;
  observed_as: string | null;
  unknown_cost_calls: number | null;
  window_start: string | null;
  state: string;
  tripped_at: string | null;
  tripped_seq: number | null;
  tripped_by: string | null;
  manual: boolean | null;
  last_reset_at: string | null;
  disabled_at: string | null;
  disabled_by: string | null;
  disabled_reason: string | null;
  in_config: boolean;
}

export interface RawEvaluation {
  state: string;
  tick_seconds: number;
  halt_after_failed_ticks: number;
  last_evaluated_at: string | null;
  last_error_at: string | null;
  last_error_phase: string | null;
  last_error_class: string | null;
  last_failing_rule: string | null;
  consecutive_failures: number;
}

export interface RawBreakers {
  org: string;
  halt: RawHalt;
  evaluation: RawEvaluation;
  breakers: RawBreaker[];
}

export interface RawBreakerReset {
  name: string;
  state: string;
  reset_at: string;
  ledger_seq: number;
  org_halted: boolean;
}

export interface RawBreakerDisable {
  name: string;
  state: string;
  disabled_at: string;
  reason: string;
  ledger_seq: number;
}

export interface RawBreakerEnable {
  name: string;
  state: string;
  enabled_at: string;
  ledger_seq: number;
}

/** `GET /v1/runs/{run}/tasks/{task}/audit` (R1.27); only the fields the app shows. */
export interface RawAuditSpend {
  total: RawMoney;
  calls: number;
  unknown_cost_calls: number;
  price_unknown_calls: number;
  cost_unknown_calls?: number;
  at_least?: RawMoney | null;
  other_currency_calls: number;
  failed_calls_no_cost: number;
}

export interface RawTaskAudit {
  run_id: string;
  task: {
    id: string;
    title: string;
    assignee: string | null;
    status: string;
    attempt: number;
    max_attempts: number;
    submission: number;
  };
  chain: { ok: boolean; head_seq: number; head: string };
  truncated: boolean;
  total_entries: number;
  entries: RawEvent[];
  attempts: {
    attempt: number;
    submission: number;
    gates: RawGate[];
    moved_to: string | null;
    reason: string | null;
    reason_hash: string | null;
  }[];
  spend: RawAuditSpend;
  spend_by_attempt: (RawAuditSpend & { attempt: number })[];
  approvals: {
    id: string;
    agent_id: string;
    asked_because: string;
    status: string;
    decided_by: string | null;
    decided_at: string | null;
    decision_reason_hash: string | null;
    reason_available: boolean;
  }[];
  decisions: ({ ledger_seq: number; at: string; actor: string; kind: string } & Record<string, unknown>)[];
  rework: {
    ledger_seq: number;
    at: string;
    from: string | null;
    to: string | null;
    attempt_before: number | null;
    attempt_after: number | null;
    max_attempts: number | null;
    by: "gate" | "person" | "other";
    reason: string | null;
    reason_hash: string | null;
    feedback_hash: string | null;
    actor: string;
  }[];
}
