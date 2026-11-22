/**
 * Undo/redo for document edits. Every edit is a command holding the document before and after
 * it; documents are immutable with structural sharing, so a command costs only what it changed.
 * Rapid edits to the same thing (typing in one field, nudging with arrow keys) merge into one step.
 */
import type { BuilderDocument } from "./document";

export interface HistoryEntry {
  label: string;
  before: BuilderDocument;
  after: BuilderDocument;
  /** Edits with the same key within MERGE_WINDOW_MS merge into one undo step. */
  mergeKey: string | null;
  at: number;
}

export interface History {
  past: HistoryEntry[];
  future: HistoryEntry[];
}

export const HISTORY_LIMIT = 200;
export const MERGE_WINDOW_MS = 1_000;

export const emptyHistory = (): History => ({ past: [], future: [] });

/** Records an edit. A no-op edit (same document) records nothing. Redo is cleared. */
export function record(
  history: History,
  before: BuilderDocument,
  after: BuilderDocument,
  label: string,
  mergeKey: string | null = null,
  now: number = Date.now(),
): History {
  if (before === after) return history;
  const last = history.past.at(-1);
  if (
    mergeKey !== null &&
    last?.mergeKey === mergeKey &&
    last.after === before &&
    now - last.at < MERGE_WINDOW_MS
  ) {
    return { past: [...history.past.slice(0, -1), { ...last, after, at: now }], future: [] };
  }
  const past = [...history.past, { label, before, after, mergeKey, at: now }];
  return { past: past.length > HISTORY_LIMIT ? past.slice(-HISTORY_LIMIT) : past, future: [] };
}

/** Steps back one edit; returns null when there is nothing to undo. */
export function undo(history: History): { history: History; doc: BuilderDocument; label: string } | null {
  const entry = history.past.at(-1);
  if (entry === undefined) return null;
  return {
    history: { past: history.past.slice(0, -1), future: [...history.future, entry] },
    doc: entry.before,
    label: entry.label,
  };
}

/** Re-applies the last undone edit; returns null when there is nothing to redo. */
export function redo(history: History): { history: History; doc: BuilderDocument; label: string } | null {
  const entry = history.future.at(-1);
  if (entry === undefined) return null;
  return {
    history: { past: [...history.past, entry], future: history.future.slice(0, -1) },
    doc: entry.after,
    label: entry.label,
  };
}
