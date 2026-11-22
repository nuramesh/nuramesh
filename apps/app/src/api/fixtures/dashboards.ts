import type { ApprovalsDashboard, CostLatencyDashboard, VerificationDashboard, VerifiersDashboard } from "../dashboards";

const envelope = {
  as_of: "2026-10-24T12:00:00Z",
  from: "2026-10-17T12:00:00Z",
  to: "2026-10-24T12:00:00Z",
  organization: "demo",
  partial: false,
  scanned_events: 128,
  dropped_values: {},
};

/** Sample answers in the server's shape, for the demo data source. */
export const DASHBOARD_FIXTURES: {
  verification: VerificationDashboard;
  verifiers: VerifiersDashboard;
  costLatency: CostLatencyDashboard;
  approvals: ApprovalsDashboard;
} = {
  verification: {
    ...envelope,
    passed: 18,
    failed: 4,
    error: 1,
    pass_rate: "0.8182",
    gates: [
      { gate: "review", passed: 8, failed: 3, error: 0, pass_rate: "0.7273" },
      { gate: "tests", passed: 10, failed: 1, error: 1, pass_rate: "0.9091" },
    ],
    roles: [
      { role: null, passed: 2, failed: 0, error: 0, pass_rate: "1.0000" },
      { role: "dev", passed: 16, failed: 4, error: 1, pass_rate: "0.8000" },
    ],
  },
  verifiers: {
    ...envelope,
    verifier_passed: 8,
    verifier_failed: 3,
    catch_rate: "0.2727",
    unknown_kind_results: 0,
    rounds_to_pass: { "1": 5, "2": 2, "3": 1, "4+": 0 },
    done_without_verifier_pass: 1,
  },
  costLatency: {
    ...envelope,
    currencies: { USD: { total: "4.35", calls: 40, unknown_cost_calls: 2 } },
    models: [
      { model: "azure/big", currency: "USD", total: "3.10", calls: 20, unknown_cost_calls: 0 },
      { model: "azure/small", currency: "USD", total: "1.25", calls: 20, unknown_cost_calls: 2 },
    ],
    roles: [{ role: "dev", currency: "USD", total: "4.35", calls: 40, unknown_cost_calls: 2 }],
    p50: 820,
    p95: 2900,
    max: 4100,
    latency_skipped: 0,
  },
  approvals: {
    ...envelope,
    waiting: 2,
    oldest_waiting_seconds: 3600,
    waiting_by_action: { merge: 1, spend: 1 },
    decided_approved: 5,
    decided_denied: 1,
  },
};
