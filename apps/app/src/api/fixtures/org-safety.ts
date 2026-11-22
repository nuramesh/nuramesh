/**
 * Sample data for the organization-safety half of the fixture client (E19, spec/e19/README.md
 * section 12). Sample data only; the app says so wherever the fixture client is in use. The states
 * are the spec's: `running` (default), `halted`, `tripped`, `failing`, `poison`, `rolePaused`,
 * `disabled`, `empty` and `viewer`.
 */
import type { BreakerEvaluation, BreakerRow, OrgRole, OrgStatus } from "../types";

export type OrgSafetyScenario =
  | "running"
  | "halted"
  | "tripped"
  | "failing"
  | "poison"
  | "rolePaused"
  | "disabled"
  | "empty"
  | "viewer";

export interface OrgSafetyState {
  org: string;
  halt: OrgStatus;
  evaluation: BreakerEvaluation;
  breakers: BreakerRow[];
}

const DAY = "2026-11-01";
const at = (time: string): string => `${DAY}T${time}+00:00`;

function role(name: string): OrgRole {
  return { role: name, paused: false, by: null, at: null, cause: null, rule: null, ledgerSeq: null, inConfig: true };
}

function rule(over: Partial<BreakerRow> & Pick<BreakerRow, "name">): BreakerRow {
  return {
    signal: "policy_denied",
    action: "halt_org",
    role: null,
    windowSeconds: 600,
    dryRun: false,
    limit: "20",
    observed: "0",
    observedAs: "live",
    unknownCostCalls: 0,
    windowStart: at("11:50:00"),
    state: "ok",
    trippedAt: null,
    trippedSeq: null,
    trippedBy: null,
    manual: false,
    lastResetAt: null,
    disabledAt: null,
    disabledBy: null,
    disabledReason: null,
    inConfig: true,
    ...over,
  };
}

/** The three rules of the `running` state. */
function rules(): BreakerRow[] {
  return [
    rule({ name: "spend-spike", signal: "spend", windowSeconds: 3600, limit: "25", observed: "3.5", windowStart: at("11:00:00") }),
    rule({ name: "denied-loop", signal: "policy_denied", windowSeconds: 600, limit: "20", observed: "4" }),
    rule({ name: "watch-gates", signal: "gate_failed", windowSeconds: 1800, limit: "5", observed: "0", dryRun: true, windowStart: at("11:30:00") }),
  ];
}

const RUNNING_HALT: OrgStatus = {
  halted: false,
  by: null,
  at: null,
  cause: null,
  rule: null,
  ledgerSeq: null,
  phase: null,
  errorClass: null,
  failingRule: null,
  failedTicks: null,
  roles: [],
  canAct: true,
  soloAfterRemoval: 0,
};

const EVALUATION_OK: BreakerEvaluation = {
  state: "ok",
  tickSeconds: 10,
  haltAfterFailedTicks: 3,
  // set on every read: a sample "last checked a few seconds ago", whatever the date is
  lastEvaluatedAt: null,
  lastErrorAt: null,
  lastErrorPhase: null,
  lastErrorClass: null,
  lastFailingRule: null,
  consecutiveFailures: 0,
};

/** A fresh copy of the sample state for a scenario. */
export function seedOrgSafety(scenario: OrgSafetyScenario = "running"): OrgSafetyState {
  const state: OrgSafetyState = {
    org: "acme",
    halt: { ...RUNNING_HALT, roles: ["researcher", "writer", "verifier"].map(role) },
    evaluation: { ...EVALUATION_OK },
    breakers: rules(),
  };
  const halted = (over: Partial<OrgStatus>): void => {
    state.halt = { ...state.halt, halted: true, at: at("11:58:00"), ...over };
  };
  switch (scenario) {
    case "halted":
      halted({ by: "human:ann", cause: "human", ledgerSeq: 412 });
      break;
    case "tripped": {
      halted({ by: "service:breaker", cause: "breaker", rule: "denied-loop", ledgerSeq: 413 });
      state.breakers = state.breakers.map((b) =>
        b.name === "denied-loop"
          ? { ...b, state: "tripped", observed: "21", observedAs: "at_trip", trippedAt: at("11:58:00"), trippedSeq: 413, trippedBy: "service:breaker" }
          : b,
      );
      break;
    }
    case "failing":
      state.evaluation = {
        ...state.evaluation,
        state: "failing",
        lastErrorAt: at("11:59:30"),
        lastErrorPhase: "evaluate",
        lastErrorClass: "KeyError",
        lastFailingRule: "spend-spike",
        consecutiveFailures: 3,
      };
      break;
    case "poison":
      halted({ by: "service:breaker", cause: "evaluation_unavailable", ledgerSeq: 414, phase: "evaluate", errorClass: "KeyError", failingRule: "spend-spike", failedTicks: 3 });
      state.evaluation = { ...state.evaluation, state: "failing", lastErrorAt: at("11:58:00"), lastErrorPhase: "evaluate", lastErrorClass: "KeyError", lastFailingRule: "spend-spike", consecutiveFailures: 3 };
      break;
    case "rolePaused":
      state.halt.roles = state.halt.roles.map((r) =>
        r.role === "researcher" ? { ...r, paused: true, by: "human:ann", at: at("11:40:00"), cause: "human", ledgerSeq: 410 } : r,
      );
      break;
    case "disabled":
      state.breakers = state.breakers.map((b) =>
        b.name === "watch-gates" ? { ...b, state: "disabled", disabledAt: at("11:30:00"), disabledBy: "human:ann", disabledReason: "planned_work" } : b,
      );
      break;
    case "empty":
      state.breakers = [];
      state.evaluation = { ...state.evaluation, state: "not_configured" };
      break;
    case "viewer":
      state.halt = { ...state.halt, canAct: false };
      break;
    case "running":
      break;
  }
  return state;
}
