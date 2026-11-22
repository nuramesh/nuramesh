/**
 * The dashboards (R2.11, spec/r211): what the server's `GET /v1/dashboards/*` says, read only.
 * Nothing here is computed in the browser: every figure, ratio and sum is the server's text or
 * integer, an absent figure stays `null`, and the app calls only these four routes.
 */

export type DashboardView = "verification" | "verifiers" | "cost-latency" | "approvals";

export const DASHBOARD_VIEWS: readonly { id: DashboardView; label: string }[] = [
  { id: "verification", label: "Verification" },
  { id: "verifiers", label: "Verifiers" },
  { id: "cost-latency", label: "Cost and latency" },
  { id: "approvals", label: "Approvals waiting" },
];

/** The time range a dashboard covers; `asOf` is the instant the state figures are read at. */
export interface DashboardWindow {
  from?: string;
  to?: string;
  asOf?: string;
}

export interface DashboardEnvelope {
  as_of: string;
  from: string;
  to: string;
  organization: string;
  /** The server stopped reading at its event cap: the figures cover the entries scanned only. */
  partial: boolean;
  scanned_events: number;
  dropped_values: Record<string, number>;
}

export interface Tally {
  passed: number;
  failed: number;
  error: number;
  pass_rate: string | null;
}

export interface VerificationDashboard extends DashboardEnvelope, Tally {
  gates: ({ gate: string } & Tally)[];
  roles: ({ role: string | null } & Tally)[];
}

export interface VerifiersDashboard extends DashboardEnvelope {
  verifier_passed: number;
  verifier_failed: number;
  /** Failed over passed plus failed; the screen calls it the verifier fail rate. */
  catch_rate: string | null;
  unknown_kind_results: number;
  rounds_to_pass: Record<"1" | "2" | "3" | "4+", number>;
  done_without_verifier_pass: number;
}

export interface MoneyRow {
  total: string;
  calls: number;
  unknown_cost_calls: number;
}

export interface CostLatencyDashboard extends DashboardEnvelope {
  currencies: Record<string, MoneyRow>;
  models: ({ model: string; currency: string } & MoneyRow)[];
  roles: ({ role: string | null; currency: string } & MoneyRow)[];
  p50: number | null;
  p95: number | null;
  max: number | null;
  latency_skipped: number;
}

export interface ApprovalsDashboard extends DashboardEnvelope {
  waiting: number;
  oldest_waiting_seconds: number | null;
  waiting_by_action: Record<string, number>;
  decided_approved: number;
  decided_denied: number;
}

export interface DashboardsApi {
  verification(window?: DashboardWindow): Promise<VerificationDashboard>;
  verifiers(window?: DashboardWindow): Promise<VerifiersDashboard>;
  costLatency(window?: DashboardWindow): Promise<CostLatencyDashboard>;
  approvals(window?: DashboardWindow): Promise<ApprovalsDashboard>;
}

/** The route of one view with its window as query parameters (the organization is never one). */
export function dashboardPath(view: DashboardView, window?: DashboardWindow): string {
  const query = new URLSearchParams();
  if (window?.from !== undefined) query.set("from", window.from);
  if (window?.to !== undefined) query.set("to", window.to);
  if (window?.asOf !== undefined) query.set("as_of", window.asOf);
  const text = query.toString();
  return `/v1/dashboards/${view}${text === "" ? "" : `?${text}`}`;
}

/** "0.7500" as "75.00%", from the text, never through a float; no figure is an em dash, never "0%". */
export function formatShare(rate: string | null): string {
  if (rate === null) return "—";
  const match = /^(\d)\.(\d{1,4})$/.exec(rate);
  if (match === null) return rate;
  const [, whole = "0", fraction = ""] = match;
  const n = BigInt(`${whole}${fraction.padEnd(4, "0")}`);
  return `${(n / 100n).toString()}.${(n % 100n).toString().padStart(2, "0")}%`;
}
