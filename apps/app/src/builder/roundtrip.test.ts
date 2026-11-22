/**
 * A team built by hand in the builder (not a template) and exported, pinned as
 * spec/builder-export/built/checked-team.yaml. The Python side
 * (tests/test_app_config_roundtrip.py) takes that same file through `nuramesh validate` and a
 * fake-model run, so the two tests together are the round trip app -> CLI (issue #1326).
 */
import { parse, stringify } from "yaml";
import { describe, expect, it } from "vitest";

import type { Role } from "@/model/org";

import { addCheck, addGate, addRule, setAgentBudget } from "./config-ops";
import { agentSnippet, applyAgentSnippet, exportYaml, importOrgYaml } from "./config-yaml";
import { blankDocument, type BuilderDocument } from "./document";
import { computeIssues } from "./issues";
import { addAgent, updateOrg, updateRole } from "./ops";
import { PREDEFINED_ROLES } from "./templates";

const golden = import.meta.glob<string>("../../../../spec/builder-export/built/*.yaml", { eager: true, query: "?raw", import: "default" });

function palette(id: string): Role {
  const role = PREDEFINED_ROLES.find((r) => r.id === id);
  if (role === undefined) throw new Error(`no predefined role ${id}`);
  return role;
}

/** What a gate needs to run, typed into the inspector's YAML tab (the form adds a gate without it). */
const GATE_SETTINGS: Record<string, Record<string, unknown>> = {
  rule: { rules: [{ criterion: "change-made", check: { type: "contains", value: "Done:" } }] },
  verifier: { verifier: "reviewer", require_different_model: true },
};

/** The steps a person takes in the builder, each through the same operation the screen calls. */
function buildCheckedTeam(): BuilderDocument {
  let doc = updateOrg(blankDocument(), { id: "checked-team", name: "Checked team" });
  // three roles dragged from the palette
  const lead = addAgent(doc, { role: palette("chief-of-staff") });
  const developer = addAgent(lead.doc, { role: palette("developer"), reportsTo: lead.id });
  const reviewer = addAgent(developer.doc, { role: palette("reviewer"), reportsTo: lead.id });
  doc = reviewer.doc;
  // models: the worker and its verifier on different models
  doc = updateRole(doc, "developer", { default_model: "fake/worker-1" });
  doc = updateRole(doc, "reviewer", { default_model: "fake/reviewer-1" });
  // the verification line, drawn from the reviewer to the developer
  doc = addCheck(doc, reviewer.id, developer.id);
  // permissions: the lead may delegate, the others may call a fake model and spend; everything else stays denied
  doc = addRule(doc, "chief-of-staff", { action: "delegate", decision: "allow", target: "*" });
  for (const role of ["developer", "reviewer"]) {
    doc = addRule(doc, role, { action: "call_model", decision: "allow", target: "fake/*" });
    doc = addRule(doc, role, { action: "spend", decision: "allow", target: "*" });
  }
  // budgets
  doc = setAgentBudget(doc, developer.id, "1.00");
  doc = setAgentBudget(doc, reviewer.id, "1.00");
  // gates for the developer's work: a cheap rule first, then the independent verifier
  doc = addGate(doc, "developer", "verifier", "Developer");
  doc = addGate(doc, "developer", "rule", "Developer");
  // the gates' settings, in the YAML tab of the developer
  const slice = parse(agentSnippet(doc, developer.id)) as { gates: { id: string; config?: unknown }[] };
  for (const gate of slice.gates) gate.config = GATE_SETTINGS[gate.id];
  const edited = applyAgentSnippet(doc, developer.id, stringify(slice));
  if (!edited.ok) throw new Error(edited.errors.join("; "));
  return edited.doc;
}

describe("a team built by hand in the builder", () => {
  it("has no blocking issue and exports exactly spec/builder-export/built/checked-team.yaml", () => {
    const doc = buildCheckedTeam();
    expect(computeIssues(doc.org, doc.config).issues).toEqual([]);
    const expected = Object.entries(golden).find(([path]) => path.endsWith("/checked-team.yaml"))?.[1];
    expect(expected, "spec/builder-export/built/checked-team.yaml").toBeDefined();
    expect(exportYaml(doc)).toBe(expected);
  });

  it("orders the gates cheapest first and keeps what the YAML tab added", () => {
    const gates = buildCheckedTeam().config.presets.gate[0]?.gates ?? [];
    expect(gates.map((g) => g.id)).toEqual(["rule", "verifier"]);
    expect(gates.map((g) => g.config)).toEqual([GATE_SETTINGS.rule, GATE_SETTINGS.verifier]);
  });

  it("imports its own export back to the same file", () => {
    const text = exportYaml(buildCheckedTeam());
    const back = importOrgYaml(text);
    expect(back.ok).toBe(true);
    if (back.ok) expect(exportYaml(back.doc)).toBe(text);
  });
});
