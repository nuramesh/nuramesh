import type { EventCategory, RunEvent } from "@/api/types";

export type TimelineFilter = "all" | "gates" | "spend" | "approvals";

export const FILTERS: { id: TimelineFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "gates", label: "Gates" },
  { id: "spend", label: "Spend" },
  { id: "approvals", label: "Approvals" },
];

/**
 * Adds events to what is already shown. The same event may arrive twice (a poll and the live
 * stream overlap), so events are keyed by `seq`; the result is newest first.
 */
export function mergeEvents(current: readonly RunEvent[], incoming: readonly RunEvent[]): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const e of current) bySeq.set(e.seq, e);
  let changed = false;
  for (const e of incoming) {
    if (!bySeq.has(e.seq)) {
      bySeq.set(e.seq, e);
      changed = true;
    }
  }
  if (!changed) return current as RunEvent[];
  return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
}

export function filterEvents(events: readonly RunEvent[], filter: TimelineFilter): RunEvent[] {
  if (filter === "all") return [...events];
  const category: EventCategory = filter;
  return events.filter((e) => e.category === category);
}

/** The highest `seq` seen (the cursor for the next poll), or 0. */
export function lastSeq(events: readonly RunEvent[]): number {
  return events.reduce((max, e) => Math.max(max, e.seq), 0);
}
