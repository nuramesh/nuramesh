import { describe, expect, it } from "vitest";

import { effectiveRules, gateProblem, type PolicyPresetDef } from "@/model/config";
import { isOpenWeight, modelFamily } from "@/model/family";
import { makeAgent, makeRole } from "@/model/org";

import { applyAgentSnippet, agentSnippet, exportYaml, importOrgYaml } from "./config-yaml";
import {
  addGate,
  addPerson,
  applyPolicyPreset,
  moveGate,
  newPersonId,
  removeGate,
  removePerson,
  removePolicyPreset,
  addRule,
  removeRule,
  setAgentBudget,
  setApprovers,
  setAppearance,
  setRoleEvals,
  renamePerson,
  stripPeople,
  updateRule,
} from "./config-ops";
import { type BuilderDocument, parseDocument, serializeDocument } from "./document";
import { computeIssues } from "./issues";
import { removeAgents, removeRole, renameAgent, renameRole } from "./ops";
import { BUILT_IN_TEMPLATES } from "./templates";
import { cos, dev, sampleDoc } from "./test-data";

const reviewer = makeRole({ id: "reviewer", name: "Reviewer", kind: "verifier" });

/** boss ← a, a1 (under a), b; plus a verifier `rev` under boss. */
function team(): BuilderDocument {
  const doc = sampleDoc();
  return {
    ...doc,
    org: {
      ...doc.org,
      roles: [cos, dev, reviewer],
      agents: [...doc.org.agents, makeAgent({ id: "rev", name: "Rev", role: "reviewer", reports_to: "boss" })],
    },
  };
}

const readOnly: PolicyPresetDef = {
  id: "read-only",
  name: "Read-only",
  description: "",
  rules: [{ action: "write_file", decision: "deny", target: "*" }],
};

describe("model families", () => {
  it.each([
    ["anthropic/claude-sonnet-4-6", "anthropic"],
    ["openai/gpt-4.1-mini", "openai"],
    ["azure-openai/text-embedding-3-small", "openai"],
    ["google/gemini-2.5-pro", "google"],
    ["ollama/llama3.1-70b", "meta"],
    ["acme/unknown-1", "acme:unknown"],
  ])("%s is %s", (model, family) => {
    expect(modelFamily(model)).toBe(family);
  });

  it("gives fake models their own families so the demo templates stay valid", () => {
    expect(modelFamily("fake/dev-1")).not.toBe(modelFamily("fake/reviewer-1"));
    expect(modelFamily("fake/dev-1")).toBe(modelFamily("fake/dev-2"));
    expect(modelFamily(null)).toBeNull();
  });

  it("knows open-weight and self-hosted models", () => {
    expect(isOpenWeight("meta/llama-3.1-8b")).toBe(true);
    expect(isOpenWeight("ollama/anything")).toBe(true);
    expect(isOpenWeight("anthropic/claude-sonnet-4-6")).toBe(false);
  });
});

describe("verification line and the model-family rule", () => {
  it("is an error for a verifier to run the same family as the work it checks", () => {
    let doc = team();
    doc = renameAgent(doc, "rev", "rev"); // no-op
    doc = { ...doc, org: { ...doc.org, agents: doc.org.agents.map((a) => (a.id === "rev" ? { ...a, checks: ["a"], model: "anthropic/claude-opus-4-6" } : a.id === "a" ? { ...a, model: "anthropic/claude-sonnet-4-6" } : a)) } };
    const codes = computeIssues(doc.org, doc.config).issues.map((i) => i.code);
    expect(codes).toContain("verifier_same_family");
  });

  it("accepts a different family, and reads the role's default model", () => {
    let doc = team();
    doc = { ...doc, org: { ...doc.org, roles: doc.org.roles.map((r) => (r.id === "reviewer" ? { ...r, default_model: "openai/gpt-4.1-mini" } : r)), agents: doc.org.agents.map((a) => (a.id === "rev" ? { ...a, checks: ["a"] } : a.id === "a" ? { ...a, model: "anthropic/claude-sonnet-4-6" } : a)) } };
    expect(computeIssues(doc.org, doc.config).issues.map((i) => i.code)).not.toContain("verifier_same_family");
    const same = { ...doc, org: { ...doc.org, agents: doc.org.agents.map((a) => (a.id === "a" ? { ...a, model: "openai/gpt-4o" } : a)) } };
    expect(computeIssues(same.org, same.config).issues.map((i) => i.code)).toContain("verifier_same_family");
  });

  it("reports the core's verification-line errors", () => {
    const doc = team();
    const bad = { ...doc.org, agents: doc.org.agents.map((a) => (a.id === "a" ? { ...a, checks: ["b"] } : a)) };
    expect(computeIssues(bad).issues.map((i) => i.code)).toContain("checker_not_verifier");
  });
});

describe("permissions", () => {
  it("adds, edits and removes rules, and leaves no empty policy behind", () => {
    let doc = team();
    doc = addRule(doc, "dev", { action: "write_file", decision: "allow", target: "*.py" });
    doc = addRule(doc, "dev", { action: "merge", decision: "ask", target: "*" });
    expect(doc.config.policies.map((p) => [p.role, p.rules.length, p.approvers.length])).toEqual([["dev", 2, 0]]);
    doc = updateRule(doc, "dev", 1, { action: "merge", decision: "deny", target: "*" });
    expect(doc.config.policies[0]?.rules[1]?.decision).toBe("deny");
    expect(updateRule(doc, "dev", 1, { action: "merge", decision: "deny", target: "*" })).toBe(doc);
    doc = removeRule(removeRule(doc, "dev", 1), "dev", 0);
    expect(doc.config.policies).toEqual([]);
  });

  it("lists preset rules before the role's own, as the core does", () => {
    let doc = applyPolicyPreset(team(), "dev", readOnly);
    doc = addRule(doc, "dev", { action: "write_file", decision: "allow", target: "src/*" });
    expect(effectiveRules(doc.config, "dev").map((r) => `${r.source}:${r.rule.decision}`)).toEqual(["Read-only:deny", "This role:allow"]);
  });

  it("applies a preset once, and drops its definition when no role uses it", () => {
    let doc = applyPolicyPreset(team(), "dev", readOnly);
    expect(applyPolicyPreset(doc, "dev", readOnly).config.presets.assignments).toEqual(doc.config.presets.assignments);
    doc = applyPolicyPreset(doc, "reviewer", readOnly);
    doc = removePolicyPreset(doc, "dev", "read-only");
    expect(doc.config.presets.policy).toHaveLength(1);
    doc = removePolicyPreset(doc, "reviewer", "read-only");
    expect(doc.config.presets).toEqual({ policy: [], gate: [], assignments: [] });
  });

  it("keeps approvers as opaque ids, unique, and removes a person everywhere", () => {
    let doc = team();
    const added = addPerson(doc, () => 0.5);
    doc = added.doc;
    expect(added.id).toMatch(/^human:u_[a-z0-9]{6}$/);
    doc = setApprovers(doc, "dev", [added.id, added.id]);
    expect(doc.config.policies[0]?.approvers).toEqual([added.id]);
    doc = removePerson(doc, added.id);
    expect(doc.config.policies).toEqual([]);
    expect(doc.ui.people).toEqual([]);
  });

  it("never reuses an existing person id", () => {
    const taken = new Set(["human:u_aaaaaa"]);
    let n = 0;
    const id = newPersonId(taken, () => (n++ < 6 ? 0 : 0.99));
    expect(id).not.toBe("human:u_aaaaaa");
  });
});

describe("gates, evals, budgets and appearance", () => {
  it("keeps gates cheapest first and refuses a preset with no required gate", () => {
    let doc = addGate(team(), "dev", "verifier", "Developer");
    doc = addGate(doc, "dev", "rule", "Developer");
    doc = addGate(doc, "dev", "test_command", "Developer");
    const gates = doc.config.presets.gate[0]?.gates ?? [];
    expect(gates.map((g) => g.kind)).toEqual(["rule", "test_command", "verifier"]);
    expect(gateProblem(gates)).toBeNull();
    expect(gateProblem([{ id: "v", kind: "verifier", required: true }, { id: "r", kind: "rule", required: true }])).toBe("bad_gate_order");
    expect(gateProblem([{ id: "r", kind: "rule", required: false }])).toBe("no_required_gate");
    expect(doc.config.presets.assignments).toEqual([{ role: "dev", policy_presets: [], gate_preset: "dev-gates" }]);
  });

  it("reorders and removes gates; removing the last one removes the preset", () => {
    let doc = addGate(addGate(team(), "dev", "rule", "Developer"), "dev", "test_command", "Developer");
    const [first, second] = doc.config.presets.gate[0]?.gates ?? [];
    doc = moveGate(doc, "dev", first?.id ?? "", 1, "Developer");
    expect(doc.config.presets.gate[0]?.gates.map((g) => g.id)).toEqual([second?.id, first?.id]);
    expect(moveGate(doc, "dev", second?.id ?? "", -1, "Developer")).toBe(doc);
    doc = removeGate(removeGate(doc, "dev", first?.id ?? "", "Developer"), "dev", second?.id ?? "", "Developer");
    expect(doc.config.presets).toEqual({ policy: [], gate: [], assignments: [] });
  });

  it("copies a shared gate preset before editing it, so other roles keep theirs", () => {
    const shared = { id: "python-tests", name: "Python tests", description: "", gates: [{ id: "pytest", kind: "test_command" as const, required: true }], evals: [] };
    let doc = team();
    doc = { ...doc, config: { ...doc.config, presets: { policy: [], gate: [shared], assignments: [{ role: "dev", policy_presets: [], gate_preset: "python-tests" }, { role: "reviewer", policy_presets: [], gate_preset: "python-tests" }] } } };
    doc = addGate(doc, "dev", "rule", "Developer");
    const byRole = Object.fromEntries(doc.config.presets.assignments.map((a) => [a.role, a.gate_preset]));
    expect(byRole).toEqual({ dev: "dev-gates", reviewer: "python-tests" });
    expect(doc.config.presets.gate.find((g) => g.id === "python-tests")?.gates).toHaveLength(1);
  });

  it("sets and clears eval requirements and agent budgets", () => {
    let doc = setRoleEvals(team(), "dev", ["dev-basics"]);
    expect(doc.config.evals).toEqual([{ role: "dev", suites: ["dev-basics"] }]);
    expect(setRoleEvals(doc, "dev", ["dev-basics"])).toBe(doc);
    doc = setRoleEvals(doc, "dev", []);
    expect(doc.config.evals).toEqual([]);
    doc = setAgentBudget(doc, "a", "5.00");
    expect(doc.config.budgets).toEqual([{ id: "a-budget", scope: "agent", scope_id: "a", limit: { amount: "5.00", currency: "USD" } }]);
    expect(setAgentBudget(doc, "a", "5.00")).toBe(doc);
    expect(setAgentBudget(setAgentBudget(doc, "a", "7"), "a", null).config.budgets).toEqual([]);
  });

  it("stores avatar and colour, and drops them when cleared", () => {
    let doc = setAppearance(team(), "a", { avatar: "DV", color: "sky" });
    expect(doc.ui.appearance).toEqual({ a: { avatar: "DV", color: "sky" } });
    doc = setAppearance(setAppearance(doc, "a", { avatar: "" }), "a", { color: "" });
    expect(doc.ui.appearance).toEqual({});
  });
});

describe("renames and deletions keep the configuration consistent", () => {
  function configured(): BuilderDocument {
    let doc = team();
    doc = applyPolicyPreset(doc, "dev", readOnly);
    doc = addRule(doc, "dev", { action: "merge", decision: "ask", target: "*" });
    doc = addGate(doc, "dev", "rule", "Developer");
    doc = setRoleEvals(doc, "dev", ["dev-basics"]);
    doc = setAgentBudget(doc, "a", "5.00");
    doc = { ...doc, org: { ...doc.org, agents: doc.org.agents.map((x) => (x.id === "rev" ? { ...x, checks: ["a"] } : x)) } };
    return setAppearance(doc, "a", { color: "sky" });
  }

  it("renaming a role moves its permissions, presets, gates and evals", () => {
    const doc = renameRole(configured(), "dev", "engineer");
    expect(doc.config.policies.map((p) => p.role)).toEqual(["engineer"]);
    expect(doc.config.presets.assignments).toEqual([{ role: "engineer", policy_presets: ["read-only"], gate_preset: "engineer-gates" }]);
    expect(doc.config.presets.gate.map((g) => g.id)).toEqual(["engineer-gates"]);
    expect(doc.config.evals).toEqual([{ role: "engineer", suites: ["dev-basics"] }]);
    expect(computeIssues(doc.org, doc.config).issues.filter((i) => i.severity === "error")).toEqual([]);
  });

  it("renaming an agent moves its budget, checks and appearance", () => {
    const doc = renameAgent(configured(), "a", "alice");
    expect(doc.config.budgets[0]?.scope_id).toBe("alice");
    expect(doc.org.agents.find((x) => x.id === "rev")?.checks).toEqual(["alice"]);
    expect(doc.ui.appearance).toEqual({ alice: { color: "sky" } });
  });

  it("deleting an agent removes its budget and checks of it; deleting an unused role removes its configuration", () => {
    let doc = removeAgents(configured(), new Set(["a"]));
    expect(doc.config.budgets).toEqual([]);
    expect(doc.org.agents.find((x) => x.id === "rev")?.checks).toEqual([]);
    expect(doc.ui.appearance).toEqual({});
    doc = removeAgents(doc, new Set(["a1", "b"]));
    doc = removeRole(doc, "dev");
    expect(doc.config.policies).toEqual([]);
    expect(doc.config.evals).toEqual([]);
    expect(doc.config.presets.assignments).toEqual([]);
    expect(doc.config.presets.policy).toEqual([]);
  });
});

describe("YAML", () => {
  function configured(): BuilderDocument {
    let doc = team();
    doc = applyPolicyPreset(doc, "dev", readOnly);
    doc = addRule(doc, "dev", { action: "write_file", decision: "allow", target: "*.py" });
    doc = addGate(doc, "dev", "test_command", "Developer");
    doc = setRoleEvals(doc, "dev", ["dev-basics"]);
    return setAgentBudget(doc, "a", "5.00");
  }

  it("round-trips an agent's slice without changing the document", () => {
    const doc = configured();
    const result = applyAgentSnippet(doc, "a", agentSnippet(doc, "a"));
    expect(result.ok && result.doc).toEqual(doc);
  });

  it("edits the document from the YAML: name, model, rules, gates, evals, budget", () => {
    const doc = configured();
    const text = `agent: {id: a, name: Alice, model: openai/gpt-4.1-mini}
role: {id: dev, name: Developer}
policy:
  presets: [read-only]
  rules:
    - {action: merge, decision: ask, target: "*"}
gates:
  - {id: lint, kind: rule, required: true}
  - {id: tests, kind: test_command, required: false}
evals: [dev-basics, dev-security]
budget: {limit: {amount: "12.50", currency: USD}}
`;
    const result = applyAgentSnippet(doc, "a", text);
    if (!result.ok) throw new Error(result.errors.join("; "));
    const next = result.doc;
    expect(next.org.agents.find((x) => x.id === "a")).toMatchObject({ name: "Alice", model: "openai/gpt-4.1-mini" });
    expect(next.config.policies[0]?.rules).toEqual([{ action: "merge", decision: "ask", target: "*" }]);
    expect(next.config.presets.gate[0]?.gates.map((g) => g.id)).toEqual(["lint", "tests"]);
    expect(next.config.evals[0]?.suites).toEqual(["dev-basics", "dev-security"]);
    expect(next.config.budgets[0]?.limit.amount).toBe("12.50");
  });

  it("changes nothing when any part is invalid, and says where", () => {
    const doc = configured();
    for (const [text, expected] of [
      ["agent: {id: other}", /agent.id/],
      ["agent: [", /./],
      ["nope: 1", /Unknown key/],
      ["policy: {rules: [{action: fly, decision: allow}]}", /Unknown action/],
      ["policy: {presets: [missing]}", /isn't a known preset/],
      ["policy: {approvers: [alice@example.com]}", /opaque user ids/],
      ["gates: [{id: x, kind: sorcery}]", /Gate kind must be/],
      ["budget: {limit: {amount: lots}}", /amount/],
      ["- not a map", /top level/],
    ] as const) {
      const result = applyAgentSnippet(doc, "a", text);
      expect(result.ok, text).toBe(false);
      if (!result.ok) expect(result.errors.join("\n"), text).toMatch(expected);
    }
  });

  it("exports a whole organization the way a hand-written nuramesh.yaml looks", () => {
    const yaml = exportYaml(configured());
    expect(yaml).toMatch(/^version: 1\n/);
    expect(yaml).toContain("policies:\n  policies:\n    - role: dev");
    expect(yaml).toContain("budgets:\n  - id: a-budget");
    expect(yaml).toContain("presets:");
    expect(yaml).toContain("evals:\n  requirements:");
    expect(yaml).not.toContain("human:u_"); // no people unless a policy names one
  });
});

// What Export YAML writes for the built-in templates, pinned (spec/builder-export/) and loaded by
// the Python core's own test (tests/core/test_builder_export.py), so a file the builder writes is a
// file `nuramesh try` accepts.
const golden = import.meta.glob<string>("../../../../spec/builder-export/*.yaml", { eager: true, query: "?raw", import: "default" });

describe("Export YAML for the built-in templates", () => {
  it.each(BUILT_IN_TEMPLATES.map((t) => [t.id, t] as const))("%s matches spec/builder-export", (id, template) => {
    const expected = Object.entries(golden).find(([path]) => path.endsWith(`/${id}.yaml`))?.[1];
    expect(expected, `spec/builder-export/${id}.yaml`).toBeDefined();
    expect(exportYaml(template.document)).toBe(expected);
  });
});

describe("what the builder refuses so the core accepts the file (review of PR d)", () => {
  const withGates = (gates: unknown[]): string => `gates:\n${gates.map((g) => `  - ${JSON.stringify(g)}`).join("\n")}\n`;

  it("keeps a positioned person across a save and reload", () => {
    const { doc: withPerson, id } = addPerson(team(), () => 0.3);
    const moved = { ...withPerson, layout: { positions: { ...withPerson.layout.positions, [id]: { x: 10, y: 20 } } } };
    const parsed = parseDocument(JSON.parse(serializeDocument(moved)) as unknown);
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.value.layout.positions[id]).toEqual({ x: 10, y: 20 });
    // a position for something that exists nowhere is still refused
    const bad = JSON.parse(serializeDocument(team())) as { layout: { positions: Record<string, unknown> } };
    bad.layout.positions.ghost = { x: 1, y: 1 };
    expect(parseDocument(bad).ok).toBe(false);
  });

  it("refuses human and eval gates, duplicate gate ids and duplicate presets in the YAML tab", () => {
    const doc = team();
    for (const [text, expected] of [
      [withGates([{ id: "h", kind: "human", required: true }]), /Gate kind must be/],
      [withGates([{ id: "e", kind: "eval", required: true }]), /Gate kind must be/],
      [withGates([{ id: "a", kind: "rule" }, { id: "a", kind: "rule" }]), /used twice/],
    ] as const) {
      const result = applyAgentSnippet(doc, "a", text);
      expect(result.ok, text).toBe(false);
      if (!result.ok) expect(result.errors.join("\n")).toMatch(expected);
    }
    const twice = applyAgentSnippet(applyPolicyPreset(doc, "dev", readOnly), "a", "policy: {presets: [read-only, read-only]}");
    expect(twice.ok).toBe(false);
  });

  it("flags a gate list the core would refuse: empty, too long, no required gate", () => {
    expect(gateProblem([])).toBe("no_required_gate");
    expect(gateProblem(Array.from({ length: 21 }, (_, i) => ({ id: `g${String(i)}`, kind: "rule" as const, required: true })))).toBe("too_many_gates");
    expect(gateProblem([{ id: "a", kind: "rule", required: true }, { id: "a", kind: "rule", required: true }])).toBe("duplicate_gate");
    expect(gateProblem([{ id: "h", kind: "human", required: true }])).toBe("bad_gate_kind");
  });

  it("warns, not errors, that an eval suite must exist in the config in Git", () => {
    const doc = setRoleEvals(team(), "dev", ["dev-basics"]);
    const report = computeIssues(doc.org, doc.config);
    const issue = report.issues.find((i) => i.code === "eval_suite_undefined");
    expect(issue?.severity).toBe("warning");
    expect(report.errorCount).toBe(0);
    expect(report.warningCount).toBeGreaterThanOrEqual(1);
  });

  it("never lets a YAML number into money: the amount must be text", () => {
    const doc = team();
    const number = applyAgentSnippet(doc, "a", "budget: {limit: {amount: 12345678901.123456}}");
    expect(number.ok).toBe(false);
    if (!number.ok) expect(number.errors.join("\n")).toMatch(/in quotes/);
    const text = applyAgentSnippet(doc, "a", 'budget: {limit: {amount: "12345678901.123456"}}');
    expect(text.ok && text.doc.config.budgets[0]?.limit.amount).toBe("12345678901.123456");
  });

  it("saves a template without people or approvers", () => {
    let doc = team();
    const added = addPerson(doc);
    doc = renamePerson(added.doc, added.id, "alice@example.com");
    doc = addRule(doc, "dev", { action: "merge", decision: "ask", target: "*" });
    doc = setApprovers(doc, "dev", [added.id]);
    const stripped = stripPeople(doc);
    expect(JSON.stringify(stripped)).not.toContain("alice");
    expect(JSON.stringify(stripped)).not.toContain("human:u_");
    expect(stripped.config.policies[0]?.rules).toHaveLength(1);
    expect(stripPeople(team())).toEqual(team());
  });

  it("a YAML edit that changes nothing leaves the document as it was", () => {
    const doc = applyPolicyPreset(team(), "dev", readOnly);
    const result = applyAgentSnippet(doc, "a", agentSnippet(doc, "a"));
    expect(result.ok && agentSnippet(result.doc, "a")).toBe(agentSnippet(doc, "a"));
  });
});

describe("Import YAML", () => {
  it("round-trips every built-in template: export, import, export again is identical", () => {
    for (const t of BUILT_IN_TEMPLATES) {
      const text = exportYaml(t.document);
      const result = importOrgYaml(text);
      if (!result.ok) throw new Error(`${t.id}: ${result.errors.join("; ")}`);
      expect(exportYaml(result.doc)).toBe(text);
      expect(result.doc.org.agents.map((a) => a.checks)).toEqual(t.document.org.agents.map((a) => a.checks));
    }
  });

  it("refuses parts the builder doesn't edit, by name, instead of dropping them", () => {
    const base = exportYaml(BUILT_IN_TEMPLATES[0]?.document ?? team());
    for (const [extra, expected] of [
      ["prices:\n  prices: []\n", /prices: The builder doesn't edit/],
      ["canon:\n  repo: x\n", /canon:/],
      ["bogus: 1\n", /bogus: Unknown key/],
      ["version: 2\n", /version/],
    ] as const) {
      const text = extra.startsWith("version") ? base.replace("version: 1", "version: 2") : base + extra;
      const result = importOrgYaml(text);
      expect(result.ok, extra).toBe(false);
      if (!result.ok) expect(result.errors.join("\n")).toMatch(expected);
    }
  });

  it("refuses eval requirements, unknown evals keys, and approvers that aren't opaque ids", () => {
    const base = exportYaml(BUILT_IN_TEMPLATES[0]?.document ?? team());
    for (const [extra, expected] of [
      ["evals:\n  requirements: [{role: developer, suites: [s1]}]\n", /evals.requirements/],
      ["evals:\n  other: 5\n", /evals.other: Unknown key/],
      ["policies:\n  policies:\n    - role: developer\n      approvers: [alice@example.com]\n", /an email address/],
    ] as const) {
      const text = extra.startsWith("policies") ? base.replace(/policies:[\s\S]*?(?=\nbudgets:|\npresets:|\nevals:|$)/, "") + extra : base + extra;
      const result = importOrgYaml(text);
      expect(result.ok, extra).toBe(false);
      if (!result.ok) expect(result.errors.join("\n")).toMatch(expected);
    }
  });

  it("says what is wrong with bad input and never throws", () => {
    for (const text of ["", "- a\n- b", "org: 5", "org: {id: X, name: n, roles: [], agents: []}\npolicies: 4", "a: &a [*a]", "\u0000", "{{{"]) {
      const result = importOrgYaml(text);
      expect(result.ok, text).toBe(false);
      if (!result.ok) expect(result.errors.length).toBeGreaterThan(0);
    }
  });

  it("an alias bomb is refused, not expanded", () => {
    const bomb = "a: &a [x,x,x,x,x,x,x,x,x]\n" + Array.from({ length: 20 }, (_, i) => `b${String(i)}: &b${String(i)} [${i === 0 ? "*a,*a,*a" : `*b${String(i - 1)},*b${String(i - 1)},*b${String(i - 1)}`}]`).join("\n");
    expect(importOrgYaml(bomb).ok).toBe(false);
  });
});
