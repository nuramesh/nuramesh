/**
 * The read-only reports (ND63): what the server's `GET /v1/reports/downgrade` says, mapped for the
 * screens. Nothing here is rounded or guessed: money and rates stay the server's text, and an
 * unknown price stays `null`. The report never switches a model, so there is nothing to write.
 */
import type { Money } from "./types";

export interface RawCounts {
  passed: number;
  failed: number;
  error: number;
}

export interface RawDowngradeModel {
  model: string;
  tasks: number;
  calls: number;
  gate: RawCounts;
  verifier: RawCounts;
  results: number;
  /** Passed over passed plus failed, four decimals; null with no verdicts yet. */
  pass_rate: string | null;
  cost_per_task: Money | null;
  cost_known: boolean;
  unknown_cost_calls: number;
}

/** The evidence for one model of a role (R1.34 Part B): finished tasks, counted per attempt. */
export interface RawEvidence {
  tasks: number;
  attempts: number;
  passed: number;
  first_try: number;
  pass_rate: string;
  first_try_rate: string;
  known_total: string;
  unknown_calls: number;
  /** Null when any call had no recorded price or nothing passed: never a guess. */
  cost_per_accepted: string | null;
}

export interface RawRecommendation {
  status: string;
  current: string;
  candidate: string | null;
  refused: { model: string; reason: string }[];
  saving?: string;
  projected_saving?: string;
  pass_rate_current?: string;
  pass_rate_candidate?: string;
  tasks_current?: number;
  tasks_candidate?: number;
}

export interface RawDowngradeReport {
  org: string;
  read_only: boolean;
  window: { from: string | null; to: string | null; as_of: string | null };
  min_sample: number;
  excluded: { mixed_model: number; not_evidence: number };
  runs_scanned: number;
  truncated: boolean;
  roles: {
    role: string;
    models: RawDowngradeModel[];
    evidence: Record<string, RawEvidence>;
    recommendation: RawRecommendation | null;
  }[];
}

/** `recommend`: a cheaper model is supported by the evidence; the others say why not. */
export type RecommendationStatus = "recommend" | "none" | "insufficient_evidence";

export interface Recommendation {
  status: RecommendationStatus;
  current: string;
  candidate: string | null;
  refused: { model: string; reason: string }[];
  /** A share of the cost per accepted result, 6 decimals ("0.750000" is 75%). */
  saving: string | null;
  passRateCurrent: string | null;
  passRateCandidate: string | null;
  tasksCurrent: number | null;
  tasksCandidate: number | null;
}

export interface DowngradeModel {
  model: string;
  tasks: number;
  calls: number;
  gate: RawCounts;
  verifier: RawCounts;
  /** Gate and verifier results that were verdicts (passed or failed). */
  results: number;
  passRate: string | null;
  /** `null` is unknown: some call had no recorded price. */
  costPerTask: Money | null;
  costKnown: boolean;
  unknownCostCalls: number;
}

export interface DowngradeReport {
  org: string;
  /** The fewest finished tasks the server needs per model before it recommends anything. */
  minSample: number;
  runsScanned: number;
  /** A bound stopped the search before every run was read. */
  truncated: boolean;
  /** Tasks left out of the evidence: a mix of models, or no clear result yet. */
  excluded: { mixedModel: number; notEvidence: number };
  roles: { role: string; models: DowngradeModel[]; evidence: Record<string, RawEvidence>; recommendation: Recommendation | null }[];
}

export interface ReportsApi {
  /** Evidence per model next to cost, and one recommendation per role. */
  downgrade(options?: { minSample?: number }): Promise<DowngradeReport>;
}

const STATUSES: readonly RecommendationStatus[] = ["recommend", "none", "insufficient_evidence"];

function statusOf(value: string): RecommendationStatus {
  // A status this app doesn't know is shown as "no recommendation", never as a recommendation.
  return STATUSES.find((s) => s === value) ?? "none";
}

function recommendationOf(raw: RawRecommendation | null): Recommendation | null {
  if (raw === null) return null;
  return {
    status: statusOf(raw.status),
    current: raw.current,
    candidate: raw.candidate,
    refused: raw.refused,
    saving: raw.saving ?? null,
    passRateCurrent: raw.pass_rate_current ?? null,
    passRateCandidate: raw.pass_rate_candidate ?? null,
    tasksCurrent: raw.tasks_current ?? null,
    tasksCandidate: raw.tasks_candidate ?? null,
  };
}

/** The server's report as the screens read it. */
export function downgradeReport(raw: RawDowngradeReport): DowngradeReport {
  return {
    org: raw.org,
    minSample: raw.min_sample,
    runsScanned: raw.runs_scanned,
    truncated: raw.truncated,
    excluded: { mixedModel: raw.excluded.mixed_model, notEvidence: raw.excluded.not_evidence },
    roles: raw.roles.map((r) => ({
      role: r.role,
      evidence: r.evidence,
      recommendation: recommendationOf(r.recommendation),
      models: r.models.map((m) => ({
        model: m.model,
        tasks: m.tasks,
        calls: m.calls,
        gate: m.gate,
        verifier: m.verifier,
        results: m.results,
        passRate: m.pass_rate,
        costPerTask: m.cost_per_task,
        costKnown: m.cost_known,
        unknownCostCalls: m.unknown_cost_calls,
      })),
    })),
  };
}

/**
 * "$0.10", "$0.0005205": the server's decimal text with at least two places and no trailing
 * zeros beyond them (per-task costs can have eight places, which `formatMoney` doesn't show).
 * `null` is an unknown price.
 */
export function formatPerTask(money: Money | null): string {
  if (money === null) return "unknown";
  const [whole = "0", fraction = ""] = money.amount.split(".");
  const kept = fraction.replace(/0+$/, "").padEnd(2, "0");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return money.currency === "USD" ? `$${grouped}.${kept}` : `${grouped}.${kept} ${money.currency}`;
}

/** "0.750000" as "75.00%": six-place text from the server, never through a float. */
export function formatCostShare(share: string): string {
  const match = /^(-?)(\d)\.(\d{6})$/.exec(share);
  if (match === null) return share;
  const [, sign = "", whole = "0", fraction = ""] = match;
  const n = BigInt(`${whole}${fraction}`);
  const hundredths = (n + 50n) / 100n; // six places to two, half up
  return `${sign}${(hundredths / 100n).toString()}.${(hundredths % 100n).toString().padStart(2, "0")}%`;
}

/** "0.8333" as "83.33%", from the text, never through a float. */
export function formatRate(rate: string | null): string {
  if (rate === null) return "no results";
  const match = /^(\d)\.(\d{1,4})$/.exec(rate);
  if (match === null) return rate;
  const [, whole = "0", fraction = ""] = match;
  const n = BigInt(`${whole}${fraction.padEnd(4, "0")}`);
  return `${(n / 100n).toString()}.${(n % 100n).toString().padStart(2, "0")}%`;
}
