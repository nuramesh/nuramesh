import type { BreachMode, BudgetWindow } from "@/api/types";

/** "Sat 25 Oct, 00:00 UTC": window edges are UTC calendar boundaries, so they are shown in UTC. */
export function formatUtc(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const day = date.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  return `${day}, ${time} UTC`;
}

export const WINDOW_NAME: Record<BudgetWindow, string> = { none: "Lifetime", day: "Daily", month: "Monthly" };

/**
 * What happens to a budget at the end of its window, in one sentence. A `reset` window starts
 * fresh by itself (and clears a block); a `hold` window does not clear a block, however many
 * windows pass: only a person lifts it (T56).
 */
export function windowNote(input: {
  window: BudgetWindow;
  onBreach: BreachMode;
  blocked: boolean;
  resetsAt: string | null;
}): string {
  const { window, onBreach, blocked, resetsAt } = input;
  if (window === "none") return blocked ? "Stopped until a person lifts it." : "No window: a lifetime cap.";
  const when = resetsAt === null ? "at the end of the window" : formatUtc(resetsAt);
  if (blocked) {
    return onBreach === "hold"
      ? "Held: stays blocked, even after the window rolls over, until a person lifts it."
      : `Blocked until the window resets (${when}); a person can lift it sooner.`;
  }
  return onBreach === "hold" ? `Window ends ${when}. If the limit is hit it is held until a person lifts it.` : `Resets ${when}.`;
}
