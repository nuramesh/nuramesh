import type { Approval, ApprovalState } from "@/api/types";
import type { ChipTone } from "@/components/ui/chip";
import { pyStrip } from "@/model/org";

/** The person using the app, as the approvals list names them (until sign-in exists, R1.22). */
export const CURRENT_USER = "You";

export const STATE_LABEL: Record<ApprovalState, string> = {
  pending: "Waiting",
  approved: "Approved",
  denied: "Rejected",
};

export const STATE_TONE: Record<ApprovalState, ChipTone> = {
  pending: "warning",
  approved: "success",
  denied: "danger",
};

/** The coloured bar at the left of a row in the list. */
export const STATE_STYLE: Record<ApprovalState, { bar: string }> = {
  pending: { bar: "border-l-warm" },
  approved: { bar: "border-l-link" },
  denied: { bar: "border-l-danger" },
};

/** Pending first, then decided; newest first inside each group. */
export function sortApprovals(items: readonly Approval[]): Approval[] {
  const rank = (a: Approval): number => (a.state === "pending" ? 0 : 1);
  return [...items].sort((a, b) => rank(a) - rank(b) || b.requestedAt.localeCompare(a.requestedAt));
}

/**
 * Whether the person may decide this request now. Only a pending request can be decided, and
 * never by whoever made it: an agent can't answer its own request (the server enforces it; the
 * screen never offers it), and nor can the person who asked.
 */
export function canDecide(approval: Approval, me: string = CURRENT_USER): boolean {
  return approval.state === "pending" && approval.requester.trim().toLowerCase() !== me.toLowerCase();
}

/** A reason is required, and whitespace isn't one. */
export function reasonProblem(reason: string): string | null {
  return pyStrip(reason) === "" ? "Give a reason: it is saved with the decision" : null;
}
