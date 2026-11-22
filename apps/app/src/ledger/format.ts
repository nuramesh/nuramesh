import type { LedgerCategory, LedgerEntry } from "@/api/types";

export const CATEGORIES: { id: LedgerCategory | "all"; label: string }[] = [
  { id: "all", label: "All" },
  { id: "tasks", label: "Tasks" },
  { id: "gates", label: "Gates" },
  { id: "spend", label: "Spend" },
  { id: "decisions", label: "Decisions" },
];

/** First characters of a hash for a table cell; the full value stays in the detail panel. */
export function shortHash(hash: string, length = 6): string {
  return hash.slice(0, length);
}

/** "1,051" for entry counts. */
export function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

/** The colour of an entry kind: passed gates in the accent, failures in danger, money and people in warm. */
export function kindTone(kind: string): "link" | "danger" | "warm" | "muted" {
  if (/\.(failed|blocked|rejected)$/.test(kind)) return "danger";
  if (/^gate\.passed$|\.passed$|\.verified$/.test(kind)) return "link";
  if (/^(approval|budget|human)\./.test(kind)) return "warm";
  return "muted";
}

export const TONE_CLASS: Record<ReturnType<typeof kindTone>, string> = {
  link: "text-link",
  danger: "text-danger",
  warm: "text-warm",
  muted: "text-muted-foreground",
};

/**
 * The entries as a JSON file for export: what the table shows (hashes, never content), in the
 * order shown. The entries hold no prompt or output text (S2), so this is safe to hand over.
 */
export function exportEntries(entries: readonly LedgerEntry[]): string {
  return `${JSON.stringify(
    entries.map((e) => ({
      entry: e.n,
      at: e.at,
      kind: e.kind,
      actor: e.actor,
      summary: e.summary,
      cost: e.cost,
      hash: e.hash,
      previous_hash: e.prevHash,
      stream: e.stream,
      task_id: e.taskId,
    })),
    null,
    2,
  )}\n`;
}

/** The number shown after `#` for a ledger entry id: "1043" stays, "run:run-1@32" becomes "32". */
export function entryLabel(id: string): string {
  const at = id.lastIndexOf("@");
  return at > 0 ? id.slice(at + 1) : id;
}

/** Where a ledger entry id lives in the app. */
export function entryHref(id: string): string {
  return `/ledger/${encodeURIComponent(id)}`;
}
