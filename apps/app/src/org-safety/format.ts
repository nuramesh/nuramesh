/** Display text for the kill switch, role pause and breakers (the organization safety notes section 12). Pure functions. */
import type { BreakerAction, BreakerRow, BreakerSignal, DisableReason, OrgRole, OrgStatus } from "@/api/types";

const plural = (n: number, unit: string): string => `${String(n)} ${unit}${n === 1 ? "" : "s"}`;

/** `3600` -> `1 hour`, `600` -> `10 minutes`, `45` -> `45 seconds`, `5400` -> `90 minutes`: the largest whole unit that divides it. */
export function windowLabel(seconds: number): string {
  if (seconds > 0 && seconds % 3600 === 0) return plural(seconds / 3600, "hour");
  if (seconds > 0 && seconds % 60 === 0) return plural(seconds / 60, "minute");
  return plural(seconds, "second");
}

const SIGNALS: Record<BreakerSignal, string> = {
  spend: "Spend",
  policy_denied: "Denied actions",
  gate_failed: "Failed gates",
  model_blocked: "Blocked model calls",
  model_called: "Model calls",
};

/** `Spend`, `Denied actions`... with ` for role <role>` when the rule counts only one role. */
export function signalLabel(signal: BreakerSignal | null, role: string | null): string {
  if (signal === null) return "";
  return `${SIGNALS[signal]}${role === null ? "" : ` for role ${role}`}`;
}

export function actionLabel(action: BreakerAction | null, role: string | null): string {
  if (action === null) return "";
  return action === "halt_org" ? "Halt organization" : `Pause role${role === null ? "" : ` ${role}`}`;
}

/** `4 of 20 events`, `3.5 of 25 USD`, with ` at trip` for a tripped rule and the unknown-cost note. */
export function observedText(row: BreakerRow): string {
  if (row.signal === null || row.observed === null || row.limit === null) return "";
  const unit = row.signal === "spend" ? "USD" : "events";
  const base = `${row.observed} of ${row.limit} ${unit}${row.observedAs === "at_trip" ? " at trip" : ""}`;
  return row.unknownCostCalls > 0 ? `${base} + ${plural(row.unknownCostCalls, "call")} with unknown cost` : base;
}

export const REASON_LABELS: Record<DisableReason, string> = {
  false_positive: "False alarm",
  planned_work: "Planned work",
  rule_being_replaced: "Rule is being replaced",
  other: "Other",
};

/** `4 seconds`, `2 minutes`, `1 hour`, `3 days`: how long before `now` (never negative). */
export function relativeAgo(iso: string, nowMs: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
  if (seconds < 60) return plural(seconds, "second");
  if (seconds < 3600) return plural(Math.floor(seconds / 60), "minute");
  if (seconds < 86400) return plural(Math.floor(seconds / 3600), "hour");
  return plural(Math.floor(seconds / 86400), "day");
}

/** `19:39` for today in the viewer's time zone, `21 Oct 19:39` for another day. */
export function localTime(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === now.toDateString() ? time : `${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${time}`;
}

const PHASES: Record<string, string> = { read: "reading", evaluate: "evaluating", write: "writing" };

/** The first line of the halted banner. */
export function haltedLine(status: OrgStatus, now: Date = new Date()): string {
  const time = status.at === null ? "an unknown time" : localTime(status.at, now);
  if (status.cause === "breaker") return `Breaker ${status.rule ?? "(unknown)"} halted it at ${time}.`;
  if (status.cause === "evaluation_unavailable") {
    const phase = status.phase === null ? undefined : PHASES[status.phase];
    const what = phase === undefined ? "" : ` while ${phase}`;
    const rule = status.failingRule === null ? "" : ` rule ${status.failingRule}`;
    const cause = status.errorClass ?? "an error";
    const after = status.failedTicks === null ? "" : `, after ${plural(status.failedTicks, "failed check")}`;
    return `Breaker checks were failing, so it was halted at ${time}. Cause: ${cause}${what}${rule}${after}.`;
  }
  return `${status.by ?? "Someone"} halted it at ${time}.`;
}

/** The D7 report line (the solo approval notes SA-18 item 4), shown whenever the count is above 0; null otherwise. */
export function soloRemovalLine(count: number): string | null {
  return count > 0 ? `solo approvals within 7 days of an approver being removed: ${String(count)}` : null;
}

/** `researcher paused it at 11:40` / `Breaker denied-loop paused it at 11:40`. */
export function pausedLine(role: OrgRole, now: Date = new Date()): string {
  const time = role.at === null ? "an unknown time" : localTime(role.at, now);
  return role.cause === "breaker" ? `Breaker ${role.rule ?? "(unknown)"} paused it at ${time}` : `${role.by ?? "Someone"} paused it at ${time}`;
}

/** Up to three lines, then `and <n> more`. */
export function capped(lines: string[], max = 3): string[] {
  return lines.length <= max ? lines : [...lines.slice(0, max), `and ${String(lines.length - max)} more`];
}
