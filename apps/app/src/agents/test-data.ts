import type { AgentCredential } from "@/api/types";

export function credential(over: Partial<AgentCredential> & Pick<AgentCredential, "id" | "agent">): AgentCredential {
  return {
    scopes: ["read"],
    state: "active",
    createdAt: "2026-10-26T12:00:00Z",
    expiresAt: "2099-01-01T00:00:00Z",
    revokedAt: null,
    issuerErased: false,
    inConfig: true,
    ...over,
  };
}
