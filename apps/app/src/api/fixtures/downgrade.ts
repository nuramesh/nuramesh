import { type RawDowngradeReport, type RawEvidence } from "../reports";

function ev(
  tasks: number,
  attempts: number,
  passed: number,
  firstTry: number,
  knownTotal: string,
  unknownCalls: number,
  costPerAccepted: string | null,
): RawEvidence {
  const rate = (n: number, d: number) => (d === 0 ? "0.000000" : (n / d).toFixed(6));
  return {
    tasks,
    attempts,
    passed,
    first_try: firstTry,
    pass_rate: rate(passed, attempts),
    first_try_rate: rate(firstTry, tasks),
    known_total: knownTotal,
    unknown_calls: unknownCalls,
    cost_per_accepted: costPerAccepted,
  };
}

/** Sample data for the downgrade report: one of each kind of answer. Never shown for a real server. */
export const DOWNGRADE_REPORT: RawDowngradeReport = {
  org: "My organization",
  read_only: true,
  window: { from: "2026-11-01T00:00:00Z", to: "2026-12-01T00:00:00Z", as_of: "2026-11-02T12:00:00Z" },
  min_sample: 30,
  excluded: { mixed_model: 1, not_evidence: 4 },
  runs_scanned: 6,
  truncated: false,
  roles: [
    {
      role: "developer",
      models: [
        {
          model: "azure-openai/gpt-5.4",
          tasks: 24,
          calls: 31,
          gate: { passed: 20, failed: 2, error: 0 },
          verifier: { passed: 22, failed: 2, error: 1 },
          results: 46,
          pass_rate: "0.9130",
          cost_per_task: { amount: "0.1840", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
        {
          model: "azure-openai/gpt-5.4-mini",
          tasks: 20,
          calls: 22,
          gate: { passed: 17, failed: 1, error: 0 },
          verifier: { passed: 19, failed: 1, error: 0 },
          results: 38,
          pass_rate: "0.9474",
          cost_per_task: { amount: "0.0352", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
      ],
      evidence: {
        "azure-openai/gpt-5.4": ev(36, 38, 34, 32, "6.624", 0, "0.194824"),
        "azure-openai/gpt-5.4-mini": ev(34, 35, 33, 32, "1.19", 0, "0.036061"),
      },
      recommendation: {
        status: "recommend",
        current: "azure-openai/gpt-5.4",
        candidate: "azure-openai/gpt-5.4-mini",
        refused: [],
        saving: "0.814906",
        projected_saving: "5.397939",
        pass_rate_current: "0.894737",
        pass_rate_candidate: "0.942857",
        tasks_current: 36,
        tasks_candidate: 34,
      },
    },
    {
      role: "reviewer",
      models: [
        {
          model: "azure-openai/gpt-5.4",
          tasks: 9,
          calls: 9,
          gate: { passed: 0, failed: 0, error: 0 },
          verifier: { passed: 8, failed: 1, error: 0 },
          results: 9,
          pass_rate: "0.8889",
          cost_per_task: { amount: "0.0921", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
        {
          model: "azure-openai/gpt-5.4-nano",
          tasks: 4,
          calls: 4,
          gate: { passed: 0, failed: 0, error: 0 },
          verifier: { passed: 4, failed: 0, error: 0 },
          results: 4,
          pass_rate: "1.0000",
          cost_per_task: { amount: "0.0049", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
      ],
      evidence: {
        "azure-openai/gpt-5.4": ev(12, 12, 12, 12, "2.4", 0, "0.200000"),
        "azure-openai/gpt-5.4-nano": ev(8, 8, 8, 8, "0.16", 0, "0.020000"),
      },
      recommendation: { status: "insufficient_evidence", current: "azure-openai/gpt-5.4", candidate: null, refused: [] },
    },
    {
      role: "researcher",
      models: [
        {
          model: "lab/local-llama",
          tasks: 5,
          calls: 8,
          gate: { passed: 4, failed: 1, error: 0 },
          verifier: { passed: 0, failed: 0, error: 0 },
          results: 5,
          pass_rate: "0.8000",
          cost_per_task: null,
          cost_known: false,
          unknown_cost_calls: 8,
        },
      ],
      evidence: {
        "lab/local-llama": ev(6, 6, 6, 6, "0.6", 3, null),
      },
      recommendation: { status: "none", current: "lab/local-llama", candidate: null, refused: [] },
    },
  ],
};
