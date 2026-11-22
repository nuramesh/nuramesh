import { formatMoneyExact } from "@/api/money";
import type { AuditDecision, AuditRework, AuditSpend, LedgerEntry } from "@/api/types";
import { decisionSentence } from "@/api/server/map";
import { atLeastWords, COST_UNKNOWN_WORDS } from "@/models/words";

/** The statuses after which a task cannot change (the page stops asking). */
export const FINAL_TASK_STATUSES: ReadonlySet<string> = new Set(["done", "cancelled", "failed"]);

export const calls = (n: number): string => `${String(n)} call${n === 1 ? "" : "s"}`;

/** The three counts of calls without an amount, in their own words; only non-zero ones are given. */
export function missingParts(s: AuditSpend): string[] {
  return [
    ...(s.costUnknownCalls > 0 ? [`${String(s.costUnknownCalls)} with ${COST_UNKNOWN_WORDS.toLowerCase()}`] : []),
    ...(s.priceUnknownCalls > s.costUnknownCalls ? [`${String(s.priceUnknownCalls - s.costUnknownCalls)} with unknown price`] : []),
    ...(s.otherCurrencyCalls > 0 ? [`${String(s.otherCurrencyCalls)} priced in another currency, not added`] : []),
    ...(s.failedCallsNoCost > 0 ? [`${String(s.failedCallsNoCost)} failed, no cost`] : []),
  ];
}

/** "3 calls, 1 with cost unknown (charged the reservation)": the line under a task's total. */
export function summaryCalls(s: AuditSpend): string {
  return [calls(s.calls), ...missingParts(s)].join(", ");
}

/**
 * One attempt's spend in words. Never "$0" for a call whose cost is unknown: a total that holds one
 * says "at least" (the server's field: the KNOWN total only, the charge is an upper bound) and the call is
 * named. `running` adds "(still running)".
 */
export function attemptSpendWords(s: AuditSpend, running: boolean): string {
  const priced = s.calls - s.unknownCostCalls;
  const parts: string[] = [];
  if (s.atLeast !== null) parts.push(`${atLeastWords(s.atLeast)} in ${calls(s.calls)}`);
  else if (priced > 0) parts.push(`${formatMoneyExact(s.total)} in ${calls(s.calls)}`);
  parts.push(...missingParts(s));
  const words = parts.length === 0 ? "no calls" : parts.join(", ");
  return running ? `${words}${s.calls > 0 ? " (still running)" : " (still running, no calls yet)"}` : words;
}

/** A model call's cost cell: the amount, or the reason there is none. */
export function costWords(entry: LedgerEntry): string | null {
  if (entry.cost !== null) return formatMoneyExact(entry.cost);
  if (entry.kind !== "model.called") return null;
  // the server's own field decides (`cost_basis: unknown`), never a guess from the outcome
  if (entry.fields.find((f) => f.label === "cost basis")?.value === "unknown") return COST_UNKNOWN_WORDS;
  const outcome = entry.fields.find((f) => f.label === "outcome")?.value;
  return outcome === "ok" || outcome === "settled_late" ? "price unknown" : "no cost (call failed)";
}

/** "a1b2c3d4e5f6..." : the first 12 characters of a hash, as a pointer (the full value is in the ledger). */
export const hashWords = (hash: string): string => `${hash.slice(0, 12)}...`;

export function reworkWords(r: AuditRework): string {
  if (r.by === "gate") return `sent back by a gate${r.reason === null ? "" : `: ${r.reason}`}`;
  if (r.by === "person") return "sent back by a person: A person's reason (not shown here)";
  return `sent back by ${r.actor}`;
}

/** One line for a decision row, in the words of spec 3.3 (no free text, ever). */
export function decisionWords(d: AuditDecision): string {
  return decisionSentence(d.kind, d.actor, (key) => d.fields[key]);
}

export type AuditFilter = "all" | "gates" | "spend" | "approvals" | "rework";

export const AUDIT_FILTERS: { id: AuditFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "gates", label: "Gates" },
  { id: "spend", label: "Spend" },
  { id: "approvals", label: "Approvals" },
  { id: "rework", label: "Rework" },
];

/** Gates = `gate.*`; Spend = `budget.*` and `model.called`; Approvals = `approval.*` and `decision.*`; Rework = the server's rework seqs. */
export function inFilter(entry: LedgerEntry, filter: AuditFilter, reworkSeqs: ReadonlySet<number>): boolean {
  switch (filter) {
    case "all":
      return true;
    case "gates":
      return entry.kind.startsWith("gate.");
    case "spend":
      return entry.kind.startsWith("budget.") || entry.kind === "model.called";
    case "approvals":
      return entry.kind.startsWith("approval.") || entry.kind.startsWith("decision.");
    case "rework":
      return reworkSeqs.has(Number(entry.label));
  }
}
