/**
 * Pure logic of the first-run checklist (the first-run checklist notes FR-1 to FR-7): who sees it and which
 * steps are done. "Done" comes only from what the server answered (FR-4); nothing here stores or guesses it.
 */
import type { AuthState } from "@/auth/types";

import type { FirstRunPrefs } from "./storage";

export type StepId = "owner" | "run" | "approve" | "invite";
export type StepStatus = "done" | "open" | "unknown" | "skipped";

export interface Step {
  id: StepId;
  status: StepStatus;
  canSkip: boolean;
  /** `no-approval-asked`: the run exists but the server lists no approval (C42); no cause is claimed. */
  note?: "no-approval-asked";
}

export type Answer<T> = ({ ok: true } & T) | { ok: false };
export interface Answers {
  runs: Answer<{ count: number }>;
  approvals: Answer<{ statuses: string[]; hasMore: boolean }>;
  invites: Answer<{ count: number }>;
}

const rec = (v: unknown): Record<string, unknown> | null =>
  typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;

/** FR-1: a signed-in owner on the server source; the server still checks every call. */
export function checklistVisible(
  auth: AuthState,
  source: "server" | "sample",
): boolean {
  return (
    source === "server" &&
    auth.kind === "signed-in" &&
    (auth.user.role as string) === "owner"
  );
}

/** `GET /v1/runs?limit=1`: `{runs: [...]}`; any other shape is unknown. */
export function parseRuns(body: unknown): Answers["runs"] {
  const runs = rec(body)?.runs;
  return Array.isArray(runs) ? { ok: true, count: runs.length } : { ok: false };
}

/** `GET /v1/approvals?limit=50`: `{items: [{status}], next_cursor}`. */
export function parseApprovals(body: unknown): Answers["approvals"] {
  const b = rec(body);
  if (!b || !Array.isArray(b.items)) return { ok: false };
  const statuses: string[] = [];
  for (const item of b.items) {
    const status = rec(item)?.status;
    if (typeof status !== "string") return { ok: false };
    statuses.push(status);
  }
  const cursor = b.next_cursor;
  return {
    ok: true,
    statuses,
    hasMore: typeof cursor === "string" && cursor.length > 0,
  };
}

/** `GET /v1/auth/invites?limit=1`: `{invites: [...], next_cursor}` (`items` in the 2026-12 shape; both count). */
export function parseInvites(body: unknown): Answers["invites"] {
  const b = rec(body);
  const list = Array.isArray(b?.invites) ? b.invites : b?.items;
  return Array.isArray(list) ? { ok: true, count: list.length } : { ok: false };
}

/** FR-3, FR-4, FR-7: the four steps in order. Done wins over skipped; an unknown read is never done. */
export function deriveSteps(answers: Answers, prefs: FirstRunPrefs): Step[] {
  const run: StepStatus = !answers.runs.ok
    ? "unknown"
    : answers.runs.count > 0
      ? "done"
      : "open";
  const approve: StepStatus = !answers.approvals.ok
    ? "unknown"
    : answers.approvals.statuses.some((s) => s !== "pending")
      ? "done"
      : "open";
  const inviteRead: StepStatus = !answers.invites.ok
    ? "unknown"
    : answers.invites.count > 0
      ? "done"
      : "open";
  const invite: StepStatus =
    inviteRead !== "done" && prefs.invite_skipped ? "skipped" : inviteRead;
  const noApproval =
    run === "done" &&
    answers.approvals.ok &&
    answers.approvals.statuses.length === 0 &&
    !answers.approvals.hasMore;
  return [
    { id: "owner", status: "done", canSkip: false },
    { id: "run", status: run, canSkip: false },
    {
      id: "approve",
      status: approve,
      canSkip: false,
      ...(noApproval ? { note: "no-approval-asked" as const } : {}),
    },
    { id: "invite", status: invite, canSkip: true },
  ];
}

/** The "n of 4" in the sidebar: only steps proved done. */
export function doneCount(steps: Step[]): number {
  return steps.filter((s) => s.status === "done").length;
}

/** FR-5: owner, run and approve done, and invite done or skipped. */
export function isComplete(steps: Step[]): boolean {
  return steps.every(
    (s) => s.status === "done" || (s.id === "invite" && s.status === "skipped"),
  );
}
