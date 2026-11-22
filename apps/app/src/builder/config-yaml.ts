/**
 * YAML for the builder: the whole organization as a `nuramesh.yaml` the core accepts (Export YAML),
 * and one agent's slice of it as the inspector's YAML tab, which edits back into the document.
 *
 * The slice has these top-level keys (all optional when editing; absent means "leave as is" for
 * `agent` and `role`, and "none" for the rest):
 *
 *     agent:   {id, name, reports_to, model, checks}   id can't change here
 *     role:    {id, name, kind, description, instructions, tools, default_model}
 *     policy:  {presets: [...], rules: [...], approvers: [...]}
 *     gates:   [{id, kind, required}, ...]             cheapest first
 *     evals:   [suite-id, ...]
 *     budget:  {limit: {amount: "5.00", currency: USD}}  or null
 */
import { parse, stringify } from "yaml";

import {
  agentBudget,
  assignmentOf,
  gatesOf,
  HUMAN_ID,
  MONEY,
  type OrgConfig,
  parseGates,
  parseRules,
  policyOf,
} from "@/model/config";
import { type Agent, fieldRules, pyStrip, type Role, ROLE_KINDS, type RoleKind } from "@/model/org";

import { setAgentBudget, setApprovers, setRoleEvals, setRoleGates, setRules } from "./config-ops";
import { type BuilderDocument, formatErrors, parseDocument } from "./document";
import { updateAgent, updateRole } from "./ops";

const dump = (value: unknown): string => stringify(value, { lineWidth: 0, defaultStringType: "PLAIN", flowCollectionPadding: false });

/** A role without its default-valued fields, as a hand-written config has it. */
function roleOut(role: Role): Record<string, unknown> {
  const out: Record<string, unknown> = { id: role.id, name: role.name };
  if (role.kind !== "worker") out.kind = role.kind;
  if (role.description !== "") out.description = role.description;
  if (role.instructions !== "") out.instructions = role.instructions;
  if (role.tools.length > 0) out.tools = [...role.tools];
  if (role.default_model !== null) out.default_model = role.default_model;
  return out;
}

function agentOut(agent: Agent): Record<string, unknown> {
  const out: Record<string, unknown> = { id: agent.id, name: agent.name, role: agent.role };
  if (agent.reports_to !== null) out.reports_to = agent.reports_to;
  if (agent.model !== null) out.model = agent.model;
  if (agent.checks.length > 0) out.checks = [...agent.checks];
  return out;
}

function configOut(config: OrgConfig): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (config.policies.length > 0) {
    out.policies = {
      policies: config.policies.map((p) => ({ role: p.role, ...(p.rules.length > 0 ? { rules: p.rules } : {}), ...(p.approvers.length > 0 ? { approvers: p.approvers } : {}) })),
    };
  }
  if (config.budgets.length > 0) out.budgets = config.budgets;
  const { policy, gate, assignments } = config.presets;
  if (policy.length + gate.length + assignments.length > 0) {
    out.presets = {
      ...(policy.length > 0 ? { policy } : {}),
      ...(gate.length > 0 ? { gate } : {}),
      ...(assignments.length > 0 ? { assignments: assignments.map((a) => ({ role: a.role, ...(a.policy_presets.length > 0 ? { policy_presets: a.policy_presets } : {}), ...(a.gate_preset !== null ? { gate_preset: a.gate_preset } : {}) })) } : {}),
    };
  }
  if (config.evals.length > 0) out.evals = { requirements: config.evals };
  return out;
}

/** The whole organization as a `nuramesh.yaml` (people and node appearance are builder-only, so not here). */
export function exportYaml(doc: BuilderDocument): string {
  const body = {
    version: 1,
    org: { id: doc.org.id, name: doc.org.name, roles: doc.org.roles.map(roleOut), agents: doc.org.agents.map(agentOut) },
    ...configOut(doc.config),
  };
  return dump(body);
}

/** One agent's slice, as the inspector's YAML tab shows it. */
export function agentSnippet(doc: BuilderDocument, agentId: string): string {
  const agent = doc.org.agents.find((a) => a.id === agentId);
  if (agent === undefined) return "";
  const role = doc.org.roles.find((r) => r.id === agent.role);
  const out: Record<string, unknown> = { agent: agentOut(agent) };
  if (role !== undefined) out.role = roleOut(role);
  const policy = policyOf(doc.config, agent.role);
  const presets = assignmentOf(doc.config, agent.role)?.policy_presets ?? [];
  if (policy !== undefined || presets.length > 0) {
    out.policy = {
      ...(presets.length > 0 ? { presets } : {}),
      ...(policy !== undefined && policy.rules.length > 0 ? { rules: policy.rules } : {}),
      ...(policy !== undefined && policy.approvers.length > 0 ? { approvers: policy.approvers } : {}),
    };
  }
  const gates = gatesOf(doc.config, agent.role);
  if (gates !== undefined && gates.gates.length > 0) out.gates = gates.gates;
  const evals = doc.config.evals.find((e) => e.role === agent.role);
  if (evals !== undefined) out.evals = evals.suites;
  const budget = agentBudget(doc.config, agent.id);
  if (budget !== undefined) out.budget = { limit: budget.limit };
  return dump(out);
}

export type SnippetResult = { ok: true; doc: BuilderDocument } | { ok: false; errors: string[] };

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Applies an edited slice to the document; nothing changes unless the whole slice is valid. */
export function applyAgentSnippet(doc: BuilderDocument, agentId: string, text: string): SnippetResult {
  const agent = doc.org.agents.find((a) => a.id === agentId);
  if (agent === undefined) return { ok: false, errors: ["This agent no longer exists"] };
  const role = doc.org.roles.find((r) => r.id === agent.role);
  let raw: unknown;
  try {
    raw = parse(text, { prettyErrors: true });
  } catch (error) {
    return { ok: false, errors: [error instanceof Error ? error.message.split("\n")[0] ?? "Not valid YAML" : "Not valid YAML"] };
  }
  raw ??= {};
  if (!isObject(raw)) return { ok: false, errors: ["The top level must be a map (agent:, role:, policy: …)"] };
  const errors: string[] = [];
  for (const key of Object.keys(raw)) {
    if (!["agent", "role", "policy", "gates", "evals", "budget"].includes(key)) errors.push(`${key}: Unknown key`);
  }

  let next = doc;
  const read = <T,>(key: string, fn: (v: unknown) => T | null): T | null => {
    const value = raw[key];
    return value === undefined ? null : fn(value);
  };

  read("agent", (v) => {
    if (!isObject(v)) return errors.push("agent: Must be a map");
    for (const k of Object.keys(v)) if (!["id", "name", "role", "reports_to", "model", "checks"].includes(k)) errors.push(`agent.${k}: Unknown key`);
    if (v.id !== undefined && v.id !== agent.id) errors.push("agent.id: Can't be changed here (use Settings, which updates every reference)");
    if (v.role !== undefined && v.role !== agent.role) errors.push("agent.role: Change the role in Settings");
    if (v.reports_to !== undefined && v.reports_to !== agent.reports_to) errors.push("agent.reports_to: Change reporting lines on the canvas");
    const patch: Partial<Pick<Agent, "name" | "model">> = {};
    if (typeof v.name === "string") {
      const problem = fieldRules.name(v.name);
      if (problem !== null) errors.push(`agent.name: ${problem}`);
      else patch.name = pyStrip(v.name);
    } else if (v.name !== undefined) errors.push("agent.name: Must be text");
    if (v.model === null || v.model === undefined) patch.model = v.model === null ? null : agent.model;
    else if (typeof v.model === "string") {
      const problem = fieldRules.model(v.model);
      if (problem !== null) errors.push(`agent.model: ${problem}`);
      else patch.model = v.model;
    } else errors.push("agent.model: Must be text or null");
    if (v.checks !== undefined) {
      if (!Array.isArray(v.checks) || !v.checks.every((c) => typeof c === "string")) errors.push("agent.checks: Must be a list of agent ids");
      else if (v.checks.join("\n") !== agent.checks.join("\n")) next = { ...next, org: { ...next.org, agents: next.org.agents.map((a) => (a.id === agent.id ? { ...a, checks: v.checks as string[] } : a)) } };
    }
    next = updateAgent(next, agent.id, patch);
    return true;
  });

  read("role", (v) => {
    if (role === undefined) return errors.push("role: This agent's role doesn't exist");
    if (!isObject(v)) return errors.push("role: Must be a map");
    for (const k of Object.keys(v)) if (!["id", "name", "kind", "description", "instructions", "tools", "default_model"].includes(k)) errors.push(`role.${k}: Unknown key`);
    if (v.id !== undefined && v.id !== role.id) errors.push("role.id: Can't be changed here (use the Roles tab)");
    const patch: Partial<Omit<Role, "id">> = {};
    if (typeof v.name === "string") {
      const problem = fieldRules.name(v.name);
      if (problem !== null) errors.push(`role.name: ${problem}`);
      else patch.name = pyStrip(v.name);
    }
    if (v.kind !== undefined) {
      if (typeof v.kind === "string" && (ROLE_KINDS as readonly string[]).includes(v.kind)) patch.kind = v.kind as RoleKind;
      else errors.push(`role.kind: Must be one of ${ROLE_KINDS.join(", ")}`);
    }
    for (const key of ["description", "instructions"] as const) {
      if (v[key] !== undefined) {
        if (typeof v[key] === "string") patch[key] = v[key];
        else errors.push(`role.${key}: Must be text`);
      }
    }
    if (v.tools !== undefined) {
      if (Array.isArray(v.tools) && v.tools.every((t) => typeof t === "string" && fieldRules.slug(t) === null)) patch.tools = v.tools as string[];
      else errors.push("role.tools: Must be a list of tool ids");
    }
    if (v.default_model !== undefined) {
      if (v.default_model === null) patch.default_model = null;
      else if (typeof v.default_model === "string" && fieldRules.model(v.default_model) === null) patch.default_model = v.default_model;
      else errors.push("role.default_model: Must be a model name or null");
    }
    next = updateRole(next, role.id, patch);
    return true;
  });

  // policy: absent means no role rules and no approvers (the slice is the whole picture).
  const policy = raw.policy;
  if (policy !== undefined && !isObject(policy)) errors.push("policy: Must be a map");
  else {
    const p = (policy ?? {});
    for (const k of Object.keys(p)) if (!["presets", "rules", "approvers"].includes(k)) errors.push(`policy.${k}: Unknown key`);
    const rules = parseRules(p.rules, "policy.rules");
    if (!rules.ok) errors.push(...rules.errors.map((e) => `${e.path}: ${e.message}`));
    const approvers = p.approvers === undefined ? [] : p.approvers;
    if (!Array.isArray(approvers) || !approvers.every((a) => typeof a === "string" && HUMAN_ID.test(a))) errors.push("policy.approvers: Must be a list of opaque user ids (human:u_…)");
    const presets = p.presets === undefined ? [] : p.presets;
    const defined = new Set(next.config.presets.policy.map((x) => x.id));
    if (!Array.isArray(presets) || !presets.every((x) => typeof x === "string")) errors.push("policy.presets: Must be a list of preset ids");
    else {
      for (const id of presets) if (!defined.has(id)) errors.push(`policy.presets: '${id}' isn't a known preset (add it with Apply policy preset)`);
      if (new Set(presets).size !== presets.length) errors.push("policy.presets: A preset is listed twice");
    }
    if (errors.length === 0 && rules.ok && Array.isArray(approvers) && Array.isArray(presets)) {
      next = setRules(next, agent.role, rules.value);
      next = setApprovers(next, agent.role, approvers as string[]);
      const wanted = presets as string[];
      const have = assignmentOf(next.config, agent.role)?.policy_presets ?? [];
      const assignments = next.config.presets.assignments;
      const current = assignments.find((a) => a.role === agent.role);
      if (wanted.join("\n") !== have.join("\n")) {
        const without = assignments.filter((a) => a.role !== agent.role);
        const merged = { role: agent.role, policy_presets: wanted, gate_preset: current?.gate_preset ?? null };
        next = { ...next, config: { ...next.config, presets: { ...next.config.presets, assignments: merged.policy_presets.length === 0 && merged.gate_preset === null ? without : [...without, merged] } } };
      }
    }
  }

  if (raw.gates !== undefined) {
    const gates = parseGates(raw.gates, "gates");
    if (!gates.ok) errors.push(...gates.errors.map((e) => `${e.path}: ${e.message}`));
    else if (JSON.stringify(gates.value) !== JSON.stringify(gatesOf(next.config, agent.role)?.gates ?? [])) {
      next = setRoleGates(next, agent.role, gates.value, role?.name ?? agent.role);
    }
  } else if (gatesOf(next.config, agent.role) !== undefined) {
    next = setRoleGates(next, agent.role, [], role?.name ?? agent.role);
  }

  if (raw.evals !== undefined) {
    if (!Array.isArray(raw.evals) || !raw.evals.every((s) => typeof s === "string" && fieldRules.slug(s) === null)) errors.push("evals: Must be a list of eval suite ids");
    else next = setRoleEvals(next, agent.role, raw.evals as string[]);
  } else next = setRoleEvals(next, agent.role, []);

  const budget = raw.budget;
  if (budget === undefined || budget === null) next = setAgentBudget(next, agent.id, null);
  else if (!isObject(budget) || !isObject(budget.limit)) errors.push("budget: Must look like {limit: {amount: \"5.00\", currency: USD}}");
  else {
    const amount = budget.limit.amount;
    // Text only: a YAML number would pass through a float and could lose digits (money is decimal).
    if (typeof amount !== "string") errors.push("budget.limit.amount: Write the amount in quotes, such as \"5.00\" (money is never a number)");
    else if (!MONEY.test(amount)) errors.push("budget.limit.amount: Must be a non-negative amount with up to 6 decimals");
    else if (budget.limit.currency !== undefined && budget.limit.currency !== "USD") errors.push("budget.limit.currency: Must be USD");
    else next = setAgentBudget(next, agent.id, amount);
  }

  return errors.length > 0 ? { ok: false, errors } : { ok: true, doc: next };
}

export type ImportResult = { ok: true; doc: BuilderDocument } | { ok: false; errors: string[] };

/** Largest YAML text the builder will parse (the same 10 MB as an imported JSON file). */
export const MAX_YAML_BYTES = 10 * 1024 * 1024;

/**
 * Reads a `nuramesh.yaml` (what Export YAML writes, or a hand-written config) into a builder
 * document. Parts the builder doesn't edit (prices, canon, price overrides, eval suites) are
 * refused by name rather than silently dropped, so nothing is lost without the person knowing.
 */
export function importOrgYaml(text: string): ImportResult {
  if (new TextEncoder().encode(text).length > MAX_YAML_BYTES) return { ok: false, errors: ["The YAML is larger than 10 MB"] };
  let raw: unknown;
  try {
    raw = parse(text, { prettyErrors: true, maxAliasCount: 50 });
  } catch (error) {
    return { ok: false, errors: [error instanceof Error ? (error.message.split("\n")[0] ?? "Not valid YAML") : "Not valid YAML"] };
  }
  if (!isObject(raw)) return { ok: false, errors: ["The top level must be a map (version, org, policies …)"] };
  const errors: string[] = [];
  const known = ["version", "org", "policies", "budgets", "presets", "evals", "prices", "price_overrides", "canon"];
  for (const key of Object.keys(raw)) if (!known.includes(key)) errors.push(`${key}: Unknown key`);
  for (const key of ["prices", "price_overrides", "canon"]) {
    if (raw[key] !== undefined) errors.push(`${key}: The builder doesn't edit this part, so importing it would drop it. Keep it in the file in Git.`);
  }
  if (raw.version !== undefined && raw.version !== 1) errors.push("version: Must be 1");
  const policies = raw.policies;
  if (policies !== undefined && !(isObject(policies) && Object.keys(policies).every((k) => k === "policies"))) errors.push("policies: Must be {policies: [...]}");
  const evals = raw.evals;
  if (isObject(evals)) {
    if (evals.suites !== undefined) errors.push("evals.suites: The builder doesn't edit eval suites, and the core refuses requirements for suites that aren't defined, so a file with evals can't be imported. Keep it in Git.");
    for (const key of Object.keys(evals)) if (!["suites", "requirements"].includes(key)) errors.push(`evals.${key}: Unknown key`);
    if (Array.isArray(evals.requirements) && evals.requirements.length > 0 && evals.suites === undefined) errors.push("evals.requirements: Eval requirements need their suites, which the builder can't carry; keep this file in Git.");
  }
  const config = {
    policies: isObject(policies) ? policies.policies : undefined,
    budgets: raw.budgets,
    presets: raw.presets,
    evals: isObject(evals) ? evals.requirements : evals === undefined ? undefined : null,
  };
  const parsed = parseDocument({ schema_version: 1, org: raw.org, config, layout: { positions: {} } });
  if (!parsed.ok) errors.push(...formatErrors(parsed.errors));
  else {
    for (const p of parsed.value.config.policies) {
      for (const id of p.approvers) if (!HUMAN_ID.test(id)) errors.push(`policies: Approver '${id.includes("@") ? "(an email address)" : id}' isn't an opaque user id (human:u_…); people are never named in a config`);
    }
    if (parsed.value.org.agents.length > 5_000) errors.push("org.agents: At most 5,000 agents");
  }
  if (errors.length > 0 || !parsed.ok) return { ok: false, errors };
  return { ok: true, doc: parsed.value };
}
