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
  base("a", "dana", { scopes: ["read", "run"] }),
  base("b", "dana", { state: "revoked", revokedAt: "2026-10-27T09:00:00Z" }),
  base("c", "dana", { state: "expired", expiresAt: "2026-10-20T12:00:00Z" }),
  base("d", "theo", {}),
];

const ERASED = [
  base("e", "dana", { scopes: ["read", "run"], issuerErased: true }),
  base("f", "theo", { issuerErased: true }),
  base("a", "rhea", {}),
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
