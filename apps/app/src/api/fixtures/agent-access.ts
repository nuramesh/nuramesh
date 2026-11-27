import type { AgentCredential } from "../types";

/** Sample states of the Access panel for screenshots and checks (`?agentAccess=<name>`, fixture data only). */
export type AgentAccessScenario = "mixed" | "erased" | "empty" | "approver" | "viewer";

const base = (id: string, agent: string, over: Partial<AgentCredential>): AgentCredential => ({
  id: `agc_${id.repeat(24)}`,
  agent,
  scopes: ["read"],
  state: "active",
  createdAt: "2026-10-26T12:00:00Z",
  expiresAt: "2099-11-25T12:00:00Z",
  revokedAt: null,
  issuerErased: false,
  inConfig: true,
  ...over,
});

const MIXED = [
  base("a", "developer", { scopes: ["read", "run"] }),
  base("b", "developer", { state: "revoked", revokedAt: "2026-10-27T09:00:00Z" }),
  base("c", "developer", { state: "expired", expiresAt: "2026-10-20T12:00:00Z" }),
  base("d", "tester", {}),
];

const ERASED = [
  base("e", "developer", { scopes: ["read", "run"], issuerErased: true }),
  base("f", "tester", { issuerErased: true }),
  base("a", "reviewer", {}),
];

export const AGENT_ACCESS_SCENARIOS: readonly AgentAccessScenario[] = ["mixed", "erased", "empty", "approver", "viewer"];

export function agentAccessOptions(scenario: AgentAccessScenario): {
  credentials: AgentCredential[];
  role: "owner" | "approver" | "viewer";
} {
  switch (scenario) {
    case "mixed":
      return { credentials: MIXED, role: "owner" };
    case "erased":
      return { credentials: ERASED, role: "owner" };
    case "empty":
      return { credentials: [], role: "owner" };
    case "approver":
      return { credentials: ERASED, role: "approver" };
    case "viewer":
      return { credentials: ERASED, role: "viewer" };
  }
}
