import { useQuery } from "@tanstack/react-query";

import { useApi } from "./context";

/** Query keys in one place, so a change can invalidate exactly what it affects. */
export const keys = {
  runs: ["runs"] as const,
  runsList: ["runs", "list"] as const,
  approvals: ["approvals"] as const,
  budgetSummary: ["budgets", "summary"] as const,
  budgets: ["budgets"] as const,
  ledger: ["ledger"] as const,
  org: ["org"] as const,
  /** First-run checklist reads (E35), per organization and person. */
  firstRun: (org: string, user: string, read: string) => ["first-run", org, user, read] as const,
  /** Halt state and role pauses (E19); polled by the status bar. */
  orgStatus: ["org-safety", "status"] as const,
  /** Circuit breakers (E19); polled by the Budgets page panel. */
  breakers: ["org-safety", "breakers"] as const,
};

/** Live-count and badge numbers for the sidebar. `undefined` until known, or if the source can't say. */
export function useSidebarFacts() {
  const api = useApi();
  const runs = useQuery({ queryKey: keys.runsList, queryFn: () => api.runs.list(), refetchInterval: 10_000 });
  const approvals = useQuery({ queryKey: keys.approvals, queryFn: () => api.approvals.list(), refetchInterval: 10_000 });
  const summary = useQuery({ queryKey: keys.budgetSummary, queryFn: () => api.budgets.summary(), refetchInterval: 30_000 });
  return {
    source: api.source,
    liveRuns: runs.data?.filter((r) => r.status === "running").length,
    pendingApprovals: approvals.data?.filter((a) => a.state === "pending").length,
    orgMonth: summary.data?.orgMonth ?? undefined,
    orgWindow: summary.data?.orgWindow ?? "month",
  };
}
