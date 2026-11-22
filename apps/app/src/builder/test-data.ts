/** Small documents for unit tests. */
import { emptyConfig, emptyUi } from "@/model/config";
import type { Agent, Role } from "@/model/org";

import { type BuilderDocument, SCHEMA_VERSION } from "./document";

export const cos: Role = {
  id: "cos",
  name: "Chief of Staff",
  kind: "chief_of_staff",
  description: "",
  instructions: "",
  tools: [],
  default_model: null,
};
export const dev: Role = { ...cos, id: "dev", name: "Developer", kind: "worker" };

export function agent(id: string, role: string, reportsTo: string | null): Agent {
  return { id, name: id.toUpperCase(), role, reports_to: reportsTo, model: null, checks: [] };
}

/** boss ← a ← a1, boss ← b */
export function sampleDoc(): BuilderDocument {
  return {
    schema_version: SCHEMA_VERSION,
    org: {
      id: "acme",
      name: "Acme",
      roles: [cos, dev],
      agents: [agent("boss", "cos", null), agent("a", "dev", "boss"), agent("a1", "dev", "a"), agent("b", "dev", "boss")],
    },
    config: emptyConfig(),
    ui: emptyUi(),
    layout: { positions: { a: { x: 10, y: 20 } } },
  };
}
