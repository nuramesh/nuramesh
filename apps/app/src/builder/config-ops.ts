/**
 * Pure edits on the configuration part of a builder document: permissions, presets, gates, evals,
 * budgets, verification lines, people and node appearance. Same style as ops.ts: each returns a new
 * document, shares what it didn't change, and returns the same document when nothing changed.
 */
import {
  type Appearance,
  assignmentOf,
  CHEAP_GATE_KINDS,
  gatesOf,
  type GatePresetDef,
  type GatePresetGate,
  type OrgConfig,
  type PolicyPresetDef,
  type PolicyRule,
  type PresetAssignment,
  roleGatePresetId,
} from "@/model/config";

import type { BuilderDocument } from "./document";
import { uniqueId } from "./ids";

function withConfig(doc: BuilderDocument, config: OrgConfig): BuilderDocument {
  return config === doc.config ? doc : { ...doc, config };
}

const sameList = (a: readonly unknown[], b: readonly unknown[]): boolean => a.length === b.length && a.every((v, i) => v === b[i]);

// ---- permissions ------------------------------------------------------------------------------

function upsertPolicy(
  doc: BuilderDocument,
  role: string,
  patch: (current: { rules: PolicyRule[]; approvers: string[] }) => { rules: PolicyRule[]; approvers: string[] },
): BuilderDocument {
  const current = doc.config.policies.find((p) => p.role === role) ?? { role, rules: [], approvers: [] };
  const next = patch(current);
  if (sameList(next.rules, current.rules) && sameList(next.approvers, current.approvers)) return doc;
  const policies = doc.config.policies.some((p) => p.role === role)
    ? doc.config.policies.map((p) => (p.role === role ? { ...p, ...next } : p))
    : [...doc.config.policies, { role, ...next }];
  // A role with nothing set has no policy at all (default deny), as in a hand-written config.
  const pruned = policies.filter((p) => p.rules.length > 0 || p.approvers.length > 0);
  return withConfig(doc, { ...doc.config, policies: pruned });
}

export function setRules(doc: BuilderDocument, role: string, rules: PolicyRule[]): BuilderDocument {
  return upsertPolicy(doc, role, (p) => ({ ...p, rules }));
}

export function addRule(doc: BuilderDocument, role: string, rule: PolicyRule): BuilderDocument {
  return upsertPolicy(doc, role, (p) => ({ ...p, rules: [...p.rules, rule] }));
}

export function updateRule(doc: BuilderDocument, role: string, index: number, rule: PolicyRule): BuilderDocument {
  return upsertPolicy(doc, role, (p) => ({
    ...p,
    rules: p.rules.map((r, i) => (i === index && (r.action !== rule.action || r.decision !== rule.decision || r.target !== rule.target) ? rule : r)),
  }));
}

export function removeRule(doc: BuilderDocument, role: string, index: number): BuilderDocument {
  return upsertPolicy(doc, role, (p) => ({ ...p, rules: p.rules.filter((_, i) => i !== index) }));
}

export function setApprovers(doc: BuilderDocument, role: string, approvers: string[]): BuilderDocument {
  return upsertPolicy(doc, role, (p) => ({ ...p, approvers: [...new Set(approvers)] }));
}

// ---- presets ----------------------------------------------------------------------------------

function upsertAssignment(
  doc: BuilderDocument,
  role: string,
  patch: (a: PresetAssignment) => PresetAssignment,
): OrgConfig["presets"]["assignments"] {
  const current = assignmentOf(doc.config, role) ?? { role, policy_presets: [], gate_preset: null };
  const next = patch(current);
  const rest = doc.config.presets.assignments.filter((a) => a.role !== role);
  // An assignment that names nothing is dropped, so an untouched role leaves no trace in the YAML.
  if (next.policy_presets.length === 0 && next.gate_preset === null) return rest;
  const index = doc.config.presets.assignments.findIndex((a) => a.role === role);
  return index < 0 ? [...rest, next] : doc.config.presets.assignments.map((a) => (a.role === role ? next : a));
}

/** Gives the role a policy preset (its definition joins the document if it isn't there yet). */
export function applyPolicyPreset(doc: BuilderDocument, role: string, preset: PolicyPresetDef): BuilderDocument {
  const known = doc.config.presets.policy.some((p) => p.id === preset.id);
  const defs = known ? doc.config.presets.policy.map((p) => (p.id === preset.id ? preset : p)) : [...doc.config.presets.policy, preset];
  const assignments = upsertAssignment(doc, role, (a) =>
    a.policy_presets.includes(preset.id) ? a : { ...a, policy_presets: [...a.policy_presets, preset.id] },
  );
  return withConfig(doc, { ...doc.config, presets: { ...doc.config.presets, policy: defs, assignments } });
}

/** Takes a preset off a role; a definition no role uses any more leaves the document. */
export function removePolicyPreset(doc: BuilderDocument, role: string, presetId: string): BuilderDocument {
  const assignments = upsertAssignment(doc, role, (a) => ({ ...a, policy_presets: a.policy_presets.filter((id) => id !== presetId) }));
  const used = assignments.some((a) => a.policy_presets.includes(presetId));
  const policy = used ? doc.config.presets.policy : doc.config.presets.policy.filter((p) => p.id !== presetId);
  return withConfig(doc, { ...doc.config, presets: { ...doc.config.presets, policy, assignments } });
}

// ---- gates and evals --------------------------------------------------------------------------

/** Where a new gate goes so the list stays cheapest first: cheap checks before a verifier or a person. */
export function insertionIndex(gates: readonly GatePresetGate[], kind: GatePresetGate["kind"]): number {
  if (!CHEAP_GATE_KINDS.has(kind)) return gates.length;
  const firstCostly = gates.findIndex((g) => !CHEAP_GATE_KINDS.has(g.kind));
  return firstCostly < 0 ? gates.length : firstCostly;
}

/** Replaces a role's ordered gate list (kept as a gate preset of its own, `<role>-gates`). */
export function setRoleGates(doc: BuilderDocument, role: string, gates: GatePresetGate[], roleName = role): BuilderDocument {
  const own = roleGatePresetId(role);
  const current = gatesOf(doc.config, role);
  if (current !== undefined && current.id !== own) {
    // The role shares a preset someone else defined: editing copies it, so other roles keep theirs.
    return setRoleGates(unshare(doc, role, roleName), role, gates, roleName);
  }
  const rest = doc.config.presets.gate.filter((g) => g.id !== own);
  const evals = current?.evals ?? [];
  if (gates.length === 0 && evals.length === 0) {
    const assignments = upsertAssignment(doc, role, (a) => ({ ...a, gate_preset: null }));
    return withConfig(doc, { ...doc.config, presets: { ...doc.config.presets, gate: rest, assignments } });
  }
  const def: GatePresetDef = { id: own, name: `${roleName} gates`, description: "", gates, evals: [...evals] };
  if (current !== undefined && sameList(current.gates, gates)) return doc;
  const assignments = upsertAssignment(doc, role, (a) => ({ ...a, gate_preset: own }));
  return withConfig(doc, { ...doc.config, presets: { ...doc.config.presets, gate: [...rest, def], assignments } });
}

/** Gives a role its own copy of the gate preset it shares, so it can be edited alone. */
function unshare(doc: BuilderDocument, role: string, roleName: string): BuilderDocument {
  const shared = gatesOf(doc.config, role);
  if (shared === undefined) return doc;
  const own = roleGatePresetId(role);
  const copy: GatePresetDef = { ...shared, id: own, name: `${roleName} gates`, gates: [...shared.gates], evals: [...shared.evals] };
  const assignments = upsertAssignment(doc, role, (a) => ({ ...a, gate_preset: own }));
  return withConfig(doc, { ...doc.config, presets: { ...doc.config.presets, gate: [...doc.config.presets.gate.filter((g) => g.id !== own), copy], assignments } });
}

/**
 * Adds a gate to a role, with its settings. A gate without settings is never written: the core
 * refuses a rule, test-command or verifier gate that has none, and a task must carry its role's
 * gates exactly, so such a gate would stop every run of the team (issue #1407).
 */
export function addGate(doc: BuilderDocument, role: string, kind: GatePresetGate["kind"], roleName?: string, config?: Record<string, unknown>): BuilderDocument {
  if (config === undefined || Object.keys(config).length === 0) return doc;
  const gates = gatesOf(doc.config, role)?.gates ?? [];
  const id = uniqueId(kind.replace(/_/g, "-"), new Set(gates.map((g) => g.id)));
  const next = [...gates];
  next.splice(insertionIndex(gates, kind), 0, { id, kind, required: true, config: structuredClone(config) });
  return setRoleGates(doc, role, next, roleName);
}

export function removeGate(doc: BuilderDocument, role: string, gateId: string, roleName?: string): BuilderDocument {
  const gates = gatesOf(doc.config, role)?.gates ?? [];
  return setRoleGates(doc, role, gates.filter((g) => g.id !== gateId), roleName);
}

export function moveGate(doc: BuilderDocument, role: string, gateId: string, delta: -1 | 1, roleName?: string): BuilderDocument {
  const gates = [...(gatesOf(doc.config, role)?.gates ?? [])];
  const from = gates.findIndex((g) => g.id === gateId);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= gates.length) return doc;
  const [moved] = gates.splice(from, 1);
  if (moved !== undefined) gates.splice(to, 0, moved);
  return setRoleGates(doc, role, gates, roleName);
}

export function setGateRequired(doc: BuilderDocument, role: string, gateId: string, required: boolean, roleName?: string): BuilderDocument {
  const gates = gatesOf(doc.config, role)?.gates ?? [];
  return setRoleGates(doc, role, gates.map((g) => (g.id === gateId ? { ...g, required } : g)), roleName);
}

/** The eval suites a role's agents must pass before taking work (suite ids; empty clears it). */
export function setRoleEvals(doc: BuilderDocument, role: string, suites: string[]): BuilderDocument {
  const rest = doc.config.evals.filter((e) => e.role !== role);
  const current = doc.config.evals.find((e) => e.role === role);
  if (suites.length === 0) return current === undefined ? doc : withConfig(doc, { ...doc.config, evals: rest });
  if (current !== undefined && sameList(current.suites, suites)) return doc;
  const evals = current === undefined ? [...rest, { role, suites }] : doc.config.evals.map((e) => (e.role === role ? { role, suites } : e));
  return withConfig(doc, { ...doc.config, evals });
}

/** Sets (or, with an empty map, clears) the criteria each gate of a role's preset checks for a task. */
export function setRoleChecks(doc: BuilderDocument, role: string, checks: Record<string, string[]>): BuilderDocument {
  const rest = doc.config.checks.filter((c) => c.role !== role);
  const kept = Object.fromEntries(Object.entries(checks).filter(([, criteria]) => criteria.length > 0));
  if (Object.keys(kept).length === 0) return rest.length === doc.config.checks.length ? doc : withConfig(doc, { ...doc.config, checks: rest });
  return withConfig(doc, { ...doc.config, checks: [...rest, { role, checks: kept }] });
}

// ---- budgets ----------------------------------------------------------------------------------

/** Sets (or, with null, removes) an agent's own budget; its id is `<agent>-budget`. */
export function setAgentBudget(doc: BuilderDocument, agentId: string, amount: string | null): BuilderDocument {
  const current = doc.config.budgets.find((b) => b.scope === "agent" && b.scope_id === agentId);
  if (amount === null) {
    return current === undefined ? doc : withConfig(doc, { ...doc.config, budgets: doc.config.budgets.filter((b) => b !== current) });
  }
  if (current?.limit.amount === amount) return doc;
  if (current !== undefined) {
    return withConfig(doc, {
      ...doc.config,
      budgets: doc.config.budgets.map((b) => (b === current ? { ...b, limit: { amount, currency: "USD" as const } } : b)),
    });
  }
  const id = uniqueId(`${agentId}-budget`, new Set(doc.config.budgets.map((b) => b.id)));
  return withConfig(doc, {
    ...doc.config,
    budgets: [...doc.config.budgets, { id, scope: "agent", scope_id: agentId, limit: { amount, currency: "USD" } }],
  });
}

// ---- verification line ------------------------------------------------------------------------

/** Makes `verifierId` check `checkedId` (the core's `Agent.checks`). Validation says if that's allowed. */
export function addCheck(doc: BuilderDocument, verifierId: string, checkedId: string): BuilderDocument {
  const agents = doc.org.agents.map((a) =>
    a.id === verifierId && !a.checks.includes(checkedId) ? { ...a, checks: [...a.checks, checkedId] } : a,
  );
  return agents.some((a, i) => a !== doc.org.agents[i]) ? { ...doc, org: { ...doc.org, agents } } : doc;
}

export function removeCheck(doc: BuilderDocument, verifierId: string, checkedId: string): BuilderDocument {
  const agents = doc.org.agents.map((a) =>
    a.id === verifierId && a.checks.includes(checkedId) ? { ...a, checks: a.checks.filter((id) => id !== checkedId) } : a,
  );
  return agents.some((a, i) => a !== doc.org.agents[i]) ? { ...doc, org: { ...doc.org, agents } } : doc;
}

// ---- people and appearance --------------------------------------------------------------------

const ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789";

/** A fresh opaque user id such as `human:u_7k2x9q`; `random` lets tests make it deterministic. */
export function newPersonId(taken: ReadonlySet<string>, random: () => number = Math.random): string {
  for (;;) {
    let suffix = "";
    for (let i = 0; i < 6; i += 1) suffix += ID_CHARS[Math.floor(random() * ID_CHARS.length)] ?? "x";
    const id = `human:u_${suffix}`;
    if (!taken.has(id)) return id;
  }
}

/** Adds a person to the canvas (a placeholder: who they are lives outside the configuration). */
export function addPerson(doc: BuilderDocument, random?: () => number): { doc: BuilderDocument; id: string } {
  const id = newPersonId(new Set(doc.ui.people.map((p) => p.id)), random);
  const label = `Approver ${String(doc.ui.people.length + 1)}`;
  return { doc: { ...doc, ui: { ...doc.ui, people: [...doc.ui.people, { id, label }] } }, id };
}

export function renamePerson(doc: BuilderDocument, id: string, label: string): BuilderDocument {
  const people = doc.ui.people.map((p) => (p.id === id && p.label !== label ? { ...p, label } : p));
  return people.some((p, i) => p !== doc.ui.people[i]) ? { ...doc, ui: { ...doc.ui, people } } : doc;
}

/** Removes a person from the canvas and from every approvers list. */
export function removePerson(doc: BuilderDocument, id: string): BuilderDocument {
  if (!doc.ui.people.some((p) => p.id === id)) return doc;
  const policies = doc.config.policies
    .map((p) => (p.approvers.includes(id) ? { ...p, approvers: p.approvers.filter((x) => x !== id) } : p))
    .filter((p) => p.rules.length > 0 || p.approvers.length > 0);
  return { ...doc, config: { ...doc.config, policies }, ui: { ...doc.ui, people: doc.ui.people.filter((p) => p.id !== id) } };
}

/**
 * A copy for saving as a template: no people on the canvas and no approvers. Templates hold roles,
 * not people (SPEC-app-v1 5, T42), and a person's label is free text that could be a name.
 */
export function stripPeople(doc: BuilderDocument): BuilderDocument {
  if (doc.ui.people.length === 0 && doc.config.policies.every((p) => p.approvers.length === 0)) return doc;
  const gone = new Set(doc.ui.people.map((p) => p.id));
  const policies = doc.config.policies
    .map((p) => (p.approvers.length > 0 ? { ...p, approvers: [] } : p))
    .filter((p) => p.rules.length > 0);
  const positions = Object.fromEntries(Object.entries(doc.layout.positions).filter(([id]) => !gone.has(id)));
  return { ...doc, config: { ...doc.config, policies }, ui: { ...doc.ui, people: [] }, layout: { positions } };
}

export function setAppearance(doc: BuilderDocument, agentId: string, patch: Appearance): BuilderDocument {
  const current = doc.ui.appearance[agentId] ?? {};
  const next: Appearance = { ...current, ...patch };
  if (next.avatar === "") delete next.avatar;
  if (next.color === "") delete next.color;
  const same = next.avatar === current.avatar && next.color === current.color;
  if (same) return doc;
  const rest = Object.fromEntries(Object.entries(doc.ui.appearance).filter(([id]) => id !== agentId));
  return { ...doc, ui: { ...doc.ui, appearance: Object.keys(next).length === 0 ? rest : { ...rest, [agentId]: next } } };
}

// ---- keeping references in step with renames and deletions ------------------------------------

/** Moves every reference to role `from` to `to`. */
export function renameRoleRefs(doc: BuilderDocument, from: string, to: string): BuilderDocument {
  const c = doc.config;
  const own = roleGatePresetId(from);
  const ownNew = roleGatePresetId(to);
  const gate = c.presets.gate.map((g) => (g.id === own ? { ...g, id: ownNew } : g));
  const config: OrgConfig = {
    policies: c.policies.map((p) => (p.role === from ? { ...p, role: to } : p)),
    budgets: c.budgets,
    presets: {
      policy: c.presets.policy,
      gate,
      assignments: c.presets.assignments.map((a) =>
        a.role === from ? { ...a, role: to, gate_preset: a.gate_preset === own ? ownNew : a.gate_preset } : a,
      ),
    },
    evals: c.evals.map((e) => (e.role === from ? { ...e, role: to } : e)),
    checks: c.checks.map((e) => (e.role === from ? { ...e, role: to } : e)),
  };
  return JSON.stringify(config) === JSON.stringify(c) ? doc : { ...doc, config };
}

/** Moves every reference to agent `from` to `to` (budgets, verification lines, appearance). */
export function renameAgentRefs(doc: BuilderDocument, from: string, to: string): BuilderDocument {
  const budgets = doc.config.budgets.map((b) => (b.scope === "agent" && b.scope_id === from ? { ...b, scope_id: to } : b));
  const agents = doc.org.agents.map((a) => (a.checks.includes(from) ? { ...a, checks: a.checks.map((id) => (id === from ? to : id)) } : a));
  const { [from]: moved, ...rest } = doc.ui.appearance;
  return {
    ...doc,
    org: agents.some((a, i) => a !== doc.org.agents[i]) ? { ...doc.org, agents } : doc.org,
    config: budgets.some((b, i) => b !== doc.config.budgets[i]) ? { ...doc.config, budgets } : doc.config,
    ui: moved === undefined ? doc.ui : { ...doc.ui, appearance: { ...rest, [to]: moved } },
  };
}

/** Drops what pointed at deleted agents: their budgets, appearance and other agents' checks of them. */
export function removeAgentRefs(doc: BuilderDocument, gone: ReadonlySet<string>): BuilderDocument {
  if (gone.size === 0) return doc;
  const budgets = doc.config.budgets.filter((b) => !(b.scope === "agent" && gone.has(b.scope_id)));
  const agents = doc.org.agents.map((a) => (a.checks.some((id) => gone.has(id)) ? { ...a, checks: a.checks.filter((id) => !gone.has(id)) } : a));
  const appearance = Object.fromEntries(Object.entries(doc.ui.appearance).filter(([id]) => !gone.has(id)));
  return {
    ...doc,
    org: agents.some((a, i) => a !== doc.org.agents[i]) ? { ...doc.org, agents } : doc.org,
    config: budgets.length === doc.config.budgets.length ? doc.config : { ...doc.config, budgets },
    ui: Object.keys(appearance).length === Object.keys(doc.ui.appearance).length ? doc.ui : { ...doc.ui, appearance },
  };
}

/** Drops a role's permissions, assignment, own gate list and eval requirement (a role nobody holds). */
export function removeRoleRefs(doc: BuilderDocument, role: string): BuilderDocument {
  const own = roleGatePresetId(role);
  const c = doc.config;
  const assignments = c.presets.assignments.filter((a) => a.role !== role);
  const stillUsedGate = new Set(assignments.map((a) => a.gate_preset));
  const config: OrgConfig = {
    policies: c.policies.filter((p) => p.role !== role),
    budgets: c.budgets,
    presets: {
      policy: c.presets.policy.filter((p) => assignments.some((a) => a.policy_presets.includes(p.id)) || !c.presets.assignments.some((a) => a.policy_presets.includes(p.id))),
      gate: c.presets.gate.filter((g) => g.id !== own || stillUsedGate.has(g.id)),
      assignments,
    },
    evals: c.evals.filter((e) => e.role !== role),
    checks: c.checks.filter((e) => e.role !== role),
  };
  return JSON.stringify(config) === JSON.stringify(c) ? doc : { ...doc, config };
}
