/**
 * The rest of an organization's configuration, next to the `Org` (roles, agents, lines): the
 * per-role permissions, budgets, presets and eval requirements the inspector edits. The shapes
 * and snake_case keys follow `nuramesh.core` (`policy.py`, `budgets.py`, `presets.py`,
 * `evals.py`) so `exportYaml` produces a file `nuramesh try` accepts.
 *
 * People are never named here: a policy's approvers are opaque user ids (`human:u_7k2x9q`, T42).
 */
import type { ActionKind, Decision, GatePresetGate, PolicyRule } from "@/api/types";

import { fieldRules, type Org } from "./org";

export type { ActionKind, Decision, GatePresetGate, PolicyRule };

export const ACTIONS: readonly ActionKind[] = [
  "delegate",
  "call_model",
  "use_tool",
  "run_command",
  "write_file",
  "spend",
  "external_message",
  "merge",
  "network",
];
export const DECISIONS: readonly Decision[] = ["allow", "ask", "deny"];

/** Gate kinds the builder offers (a subset of the core's `GateKind`). */
export type GateKindName = GatePresetGate["kind"];
/** Cheap deterministic checks run before costly ones (a verifier or a person), as `GatePreset` requires. */
export const CHEAP_GATE_KINDS: ReadonlySet<string> = new Set(["rule", "test_command"]);

export const HUMAN_ID = /^human:u_[a-z0-9]{4,32}$/;
export const MONEY = /^\d{1,12}(\.\d{1,6})?$/;
export const MAX_RULES = 500;
export const MAX_GATES = 20;
/** Gate kinds the builder can write so the core accepts them (a human gate needs configuration it can't edit yet). */
export const GATE_KINDS: readonly string[] = ["rule", "test_command", "verifier"];

export interface RolePolicy {
  role: string;
  rules: PolicyRule[];
  /** Opaque user ids that may answer this role's approval requests. */
  approvers: string[];
}

export type BudgetScopeName = "run" | "task" | "agent" | "team";

export interface BudgetRow {
  id: string;
  scope: BudgetScopeName;
  scope_id: string;
  limit: { amount: string; currency: "USD" };
}

export interface PolicyPresetDef {
  id: string;
  name: string;
  description: string;
  rules: PolicyRule[];
}

export interface GatePresetDef {
  id: string;
  name: string;
  description: string;
  gates: GatePresetGate[];
  evals: string[];
}

export interface PresetAssignment {
  role: string;
  policy_presets: string[];
  gate_preset: string | null;
}

export interface EvalRequirement {
  role: string;
  suites: string[];
}

/** The criteria each gate of a role's gate preset checks for a task (`checks[gate id]`, builder-only: never exported to the core's YAML). */
export interface RoleChecks {
  role: string;
  checks: Record<string, string[]>;
}

/** Permissions, budgets, presets, eval requirements and gate checks. Every list is empty for a new organization. */
export interface OrgConfig {
  policies: RolePolicy[];
  budgets: BudgetRow[];
  presets: { policy: PolicyPresetDef[]; gate: GatePresetDef[]; assignments: PresetAssignment[] };
  evals: EvalRequirement[];
  checks: RoleChecks[];
}

export function emptyConfig(): OrgConfig {
  return { policies: [], budgets: [], presets: { policy: [], gate: [], assignments: [] }, evals: [], checks: [] };
}

/** Builder-only extras that never reach the core: the people on the canvas and node appearance. */
export interface Person {
  /** `human:u_…`, the id policies reference. */
  id: string;
  /** What the canvas shows for this person (never stored in the ledger). */
  label: string;
}

export interface Appearance {
  /** A short label for the node's avatar (initials by default). */
  avatar?: string;
  /** One of `ACCENTS`. */
  color?: string;
}

export const ACCENTS = ["indigo", "sky", "amber", "rose", "slate"] as const;

// ---- validation -----------------------------------------------------------------------------

export interface ConfigProblem {
  code:
    | "unknown_role"
    | "unknown_agent"
    | "duplicate_policy"
    | "duplicate_budget"
    | "unknown_preset"
    | "bad_rule"
    | "bad_gate_order"
    | "no_required_gate"
    | "too_many_gates"
    | "duplicate_gate"
    | "bad_gate_kind"
    | "eval_suite_undefined"
    | "duplicate_preset_use"
    | "bad_amount"
    | "bad_approver"
    | "duplicate_requirement";
  message: string;
  /** The role or agent id the problem is about. */
  subject: string | null;
}

/** Why a permission rule is malformed, or null. Mirrors `PolicyRule` (core/policy.py). */
export function ruleProblem(rule: PolicyRule): string | null {
  if (!ACTIONS.includes(rule.action)) return `Unknown action '${rule.action}'`;
  if (!DECISIONS.includes(rule.decision)) return `Unknown decision '${rule.decision}'`;
  if (rule.target.length === 0 || rule.target.length > 200) return "Target must be 1–200 characters";
  if (rule.action === "spend" && rule.target !== "*") return "A spend rule has no target pattern";
  return null;
}

/** Cheapest-first order, 1–20 gates, unique ids and at least one required gate, as `GatePreset` demands. */
export function gateProblem(
  gates: readonly GatePresetGate[],
): "bad_gate_order" | "no_required_gate" | "too_many_gates" | "duplicate_gate" | "bad_gate_kind" | null {
  if (gates.length > MAX_GATES) return "too_many_gates";
  if (new Set(gates.map((g) => g.id)).size !== gates.length) return "duplicate_gate";
  if (gates.some((g) => !GATE_KINDS.includes(g.kind))) return "bad_gate_kind";
  let costly = false;
  for (const gate of gates) {
    if (CHEAP_GATE_KINDS.has(gate.kind)) {
      if (costly) return "bad_gate_order";
    } else {
      costly = true;
    }
  }
  return !gates.some((g) => g.required) ? "no_required_gate" : null;
}

/** Every cross-reference problem between the configuration and the organization. */
export function configProblems(org: Org, config: OrgConfig): ConfigProblem[] {
  const out: ConfigProblem[] = [];
  const roles = new Set(org.roles.map((r) => r.id));
  const agents = new Set(org.agents.map((a) => a.id));
  const add = (code: ConfigProblem["code"], message: string, subject: string | null): void => {
    out.push({ code, message, subject });
  };

  const seenPolicy = new Set<string>();
  for (const policy of config.policies) {
    if (!roles.has(policy.role)) add("unknown_role", `Permissions are for role '${policy.role}', which doesn't exist`, policy.role);
    if (seenPolicy.has(policy.role)) add("duplicate_policy", `Role '${policy.role}' has two permission lists`, policy.role);
    seenPolicy.add(policy.role);
    policy.rules.forEach((rule, index) => {
      const problem = ruleProblem(rule);
      if (problem !== null) add("bad_rule", `Role '${policy.role}' rule ${String(index + 1)}: ${problem}`, policy.role);
    });
    for (const id of policy.approvers) {
      if (!HUMAN_ID.test(id)) add("bad_approver", `Approver '${id}' isn't an opaque user id (human:u_…)`, policy.role);
    }
  }

  const seenBudget = new Set<string>();
  for (const budget of config.budgets) {
    if (fieldRules.slug(budget.id) !== null) add("duplicate_budget", `Budget id '${budget.id}' isn't a valid id`, budget.scope_id);
    if (seenBudget.has(budget.id)) add("duplicate_budget", `Budget id '${budget.id}' is used twice`, budget.scope_id);
    seenBudget.add(budget.id);
    if (budget.scope === "agent" && !agents.has(budget.scope_id)) {
      add("unknown_agent", `Budget '${budget.id}' is for agent '${budget.scope_id}', which doesn't exist`, budget.scope_id);
    }
    if (!MONEY.test(budget.limit.amount)) add("bad_amount", `Budget '${budget.id}': the limit must be an amount such as 5.00`, budget.scope_id);
  }

  const policyIds = new Set(config.presets.policy.map((p) => p.id));
  const gateIds = new Set(config.presets.gate.map((g) => g.id));
  for (const assignment of config.presets.assignments) {
    if (!roles.has(assignment.role)) add("unknown_role", `Presets are assigned to role '${assignment.role}', which doesn't exist`, assignment.role);
    if (new Set(assignment.policy_presets).size !== assignment.policy_presets.length) {
      add("duplicate_preset_use", `Role '${assignment.role}' lists a policy preset twice`, assignment.role);
    }
    for (const id of assignment.policy_presets) {
      if (!policyIds.has(id)) add("unknown_preset", `Role '${assignment.role}' uses policy preset '${id}', which isn't defined`, assignment.role);
    }
    if (assignment.gate_preset !== null && !gateIds.has(assignment.gate_preset)) {
      add("unknown_preset", `Role '${assignment.role}' uses gate preset '${assignment.gate_preset}', which isn't defined`, assignment.role);
    }
  }
  for (const preset of config.presets.policy) {
    for (const rule of preset.rules) {
      const problem = ruleProblem(rule);
      if (problem !== null) add("bad_rule", `Policy preset '${preset.id}': ${problem}`, null);
    }
  }
  for (const preset of config.presets.gate) {
    const problem = gateProblem(preset.gates);
    const text: Record<NonNullable<typeof problem>, string> = {
      bad_gate_order: "order gates cheapest first (checks before a verifier or a person)",
      no_required_gate: "needs at least one gate, and at least one of them required",
      too_many_gates: `has more than ${String(MAX_GATES)} gates`,
      duplicate_gate: "uses the same gate id twice",
      bad_gate_kind: "has a gate kind the builder can't write for the core (rule, test command and verifier only)",
    };
    if (problem !== null) add(problem, `Gate preset '${preset.id}' ${text[problem]}`, null);
  }

  const seenRequirement = new Set<string>();
  for (const requirement of config.evals) {
    if (!roles.has(requirement.role)) add("unknown_role", `Evals are required for role '${requirement.role}', which doesn't exist`, requirement.role);
    for (const suite of requirement.suites) {
      add("eval_suite_undefined", `Eval suite '${suite}' (role '${requirement.role}') must be defined in the evals part of your config in Git, or the core refuses the file`, requirement.role);
    }
    if (seenRequirement.has(requirement.role)) add("duplicate_requirement", `Role '${requirement.role}' has two eval requirements`, requirement.role);
    seenRequirement.add(requirement.role);
  }
  return out;
}

// ---- lookups --------------------------------------------------------------------------------

export function policyOf(config: OrgConfig, role: string): RolePolicy | undefined {
  return config.policies.find((p) => p.role === role);
}

/** Opaque ids of the people who answer a role's approval requests. */
export function approversOf(config: OrgConfig, role: string): string[] {
  return policyOf(config, role)?.approvers ?? [];
}

export function assignmentOf(config: OrgConfig, role: string): PresetAssignment | undefined {
  return config.presets.assignments.find((a) => a.role === role);
}

/** The id of the gate list a role owns (created the first time its gates are edited). */
export function roleGatePresetId(role: string): string {
  return `${role}-gates`.slice(0, 63);
}

/** The ordered gates a role's agents must pass: its gate preset's list. */
export function gatesOf(config: OrgConfig, role: string): GatePresetDef | undefined {
  const id = assignmentOf(config, role)?.gate_preset;
  return id === null || id === undefined ? undefined : config.presets.gate.find((g) => g.id === id);
}

/** The rules in force for a role, in the core's order: each preset's rules first, then its own. */
export function effectiveRules(config: OrgConfig, role: string): { source: string; rule: PolicyRule }[] {
  const out: { source: string; rule: PolicyRule }[] = [];
  for (const presetId of assignmentOf(config, role)?.policy_presets ?? []) {
    const preset = config.presets.policy.find((p) => p.id === presetId);
    for (const rule of preset?.rules ?? []) out.push({ source: preset?.name ?? presetId, rule });
  }
  for (const rule of policyOf(config, role)?.rules ?? []) out.push({ source: "This role", rule });
  return out;
}

/** The agent's own budget (scope `agent`), if it has one. */
export function agentBudget(config: OrgConfig, agentId: string): BudgetRow | undefined {
  return config.budgets.find((b) => b.scope === "agent" && b.scope_id === agentId);
}

// ---- parsing untrusted JSON ---------------------------------------------------------------------

type Json = Record<string, unknown>;
export type ConfigParse<T> = { ok: true; value: T } | { ok: false; errors: { path: string; message: string }[] };

const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

class Reader {
  readonly errors: { path: string; message: string }[] = [];
  fail(path: string, message: string): void {
    this.errors.push({ path, message });
  }
  obj(v: unknown, path: string, keys: readonly string[]): Json | null {
    if (!isObject(v)) {
      this.fail(path, "Must be an object");
      return null;
    }
    for (const key of Object.keys(v)) if (!keys.includes(key)) this.fail(`${path}.${key}`, "Unknown field");
    return v;
  }
  list(v: unknown, path: string): unknown[] {
    if (v === undefined) return [];
    if (!Array.isArray(v)) {
      this.fail(path, "Must be a list");
      return [];
    }
    return v as unknown[];
  }
  str(v: unknown, path: string, fallback?: string): string {
    if (v === undefined && fallback !== undefined) return fallback;
    if (typeof v !== "string") {
      this.fail(path, v === undefined ? "Required" : "Must be a string");
      return "";
    }
    return v;
  }
  strs(v: unknown, path: string): string[] {
    return this.list(v, path).map((item, i) => this.str(item, `${path}[${String(i)}]`));
  }
}

function readRule(r: Reader, raw: unknown, path: string): PolicyRule | null {
  const o = r.obj(raw, path, ["action", "target", "over", "decision", "reason"]);
  if (o === null) return null;
  const rule: PolicyRule = {
    action: r.str(o.action, `${path}.action`) as ActionKind,
    decision: r.str(o.decision, `${path}.decision`) as Decision,
    target: r.str(o.target, `${path}.target`, "*"),
  };
  if (o.over !== undefined || o.reason !== undefined) {
    // The builder doesn't edit these two core fields; refusing is safer than silently dropping them.
    r.fail(`${path}.${o.over !== undefined ? "over" : "reason"}`, "Not supported by the builder yet");
  }
  const problem = ruleProblem(rule);
  if (problem !== null) r.fail(path, problem);
  return rule;
}

function readRules(r: Reader, raw: unknown, path: string): PolicyRule[] {
  const rules: PolicyRule[] = [];
  r.list(raw, path).forEach((item, i) => {
    const rule = readRule(r, item, `${path}[${String(i)}]`);
    if (rule !== null) rules.push(rule);
  });
  if (rules.length > MAX_RULES) r.fail(path, `At most ${String(MAX_RULES)} rules`);
  return rules;
}

/** A gate's `config` as the core's `GateTemplate` takes it: plain JSON with no floats (it is hashed into the ledger). */
function jsonProblem(v: unknown, path: string, depth = 0): string | null {
  if (depth > 20) return `${path}: nested too deeply`;
  if (v === null || typeof v === "string" || typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isSafeInteger(v) ? null : `${path}: numbers must be whole (the core refuses floats)`;
  if (Array.isArray(v)) {
    for (const [i, item] of (v as unknown[]).entries()) {
      const problem = jsonProblem(item, `${path}[${String(i)}]`, depth + 1);
      if (problem !== null) return problem;
    }
    return null;
  }
  if (isObject(v)) {
    for (const [key, item] of Object.entries(v)) {
      const problem = jsonProblem(item, `${path}.${key}`, depth + 1);
      if (problem !== null) return problem;
    }
    return null;
  }
  return `${path}: not plain JSON`;
}

function readGates(r: Reader, raw: unknown, path: string): GatePresetGate[] {
  const out: GatePresetGate[] = [];
  r.list(raw, path).forEach((item, i) => {
    const p = `${path}[${String(i)}]`;
    const o = r.obj(item, p, ["id", "kind", "required", "config"]);
    if (o === null) return;
    const kind = r.str(o.kind, `${p}.kind`) as GateKindName;
    if (!GATE_KINDS.includes(kind)) r.fail(`${p}.kind`, "Gate kind must be rule, test_command or verifier (a human gate needs configuration the builder can't edit yet)");
    const required = o.required === undefined ? true : o.required;
    if (typeof required !== "boolean") r.fail(`${p}.required`, "Must be true or false");
    const id = r.str(o.id, `${p}.id`);
    if (out.some((g) => g.id === id)) r.fail(`${p}.id`, `Gate id '${id}' is used twice`);
    const gate: GatePresetGate = { id, kind, required: required !== false };
    if (o.config !== undefined) {
      if (!isObject(o.config)) r.fail(`${p}.config`, "Must be an object");
      else {
        const problem = jsonProblem(o.config, `${p}.config`);
        if (problem !== null) r.fail(`${p}.config`, problem);
        else gate.config = o.config;
      }
    }
    out.push(gate);
  });
  if (out.length > MAX_GATES) r.fail(path, `At most ${String(MAX_GATES)} gates`);
  return out;
}

/** Parses the `config` part of a builder document (absent means empty). */
export function parseConfig(raw: unknown, path = "config"): ConfigParse<OrgConfig> {
  if (raw === undefined) return { ok: true, value: emptyConfig() };
  const r = new Reader();
  const o = r.obj(raw, path, ["policies", "budgets", "presets", "evals", "checks"]);
  if (o === null) return { ok: false, errors: r.errors };
  const policies = r.list(o.policies, `${path}.policies`).flatMap((item, i): RolePolicy[] => {
    const p = `${path}.policies[${String(i)}]`;
    const po = r.obj(item, p, ["role", "rules", "approvers"]);
    if (po === null) return [];
    return [{ role: r.str(po.role, `${p}.role`), rules: readRules(r, po.rules, `${p}.rules`), approvers: r.strs(po.approvers, `${p}.approvers`) }];
  });
  const budgets = r.list(o.budgets, `${path}.budgets`).flatMap((item, i): BudgetRow[] => {
    const p = `${path}.budgets[${String(i)}]`;
    const bo = r.obj(item, p, ["id", "scope", "scope_id", "limit"]);
    if (bo === null) return [];
    const scope = r.str(bo.scope, `${p}.scope`) as BudgetScopeName;
    if (!["run", "task", "agent", "team"].includes(scope)) r.fail(`${p}.scope`, "Must be run, task, agent or team");
    const lo = r.obj(bo.limit, `${p}.limit`, ["amount", "currency"]);
    const amount = lo === null ? "" : r.str(lo.amount, `${p}.limit.amount`);
    if (lo !== null && lo.currency !== "USD") r.fail(`${p}.limit.currency`, "Must be USD");
    return [{ id: r.str(bo.id, `${p}.id`), scope, scope_id: r.str(bo.scope_id, `${p}.scope_id`), limit: { amount, currency: "USD" } }];
  });
  const presetsRaw = o.presets === undefined ? {} : o.presets;
  const po = r.obj(presetsRaw, `${path}.presets`, ["policy", "gate", "assignments"]) ?? {};
  const policyPresets = r.list(po.policy, `${path}.presets.policy`).flatMap((item, i): PolicyPresetDef[] => {
    const p = `${path}.presets.policy[${String(i)}]`;
    const x = r.obj(item, p, ["id", "name", "description", "rules"]);
    return x === null
      ? []
      : [{ id: r.str(x.id, `${p}.id`), name: r.str(x.name, `${p}.name`), description: r.str(x.description, `${p}.description`, ""), rules: readRules(r, x.rules, `${p}.rules`) }];
  });
  const gatePresets = r.list(po.gate, `${path}.presets.gate`).flatMap((item, i): GatePresetDef[] => {
    const p = `${path}.presets.gate[${String(i)}]`;
    const x = r.obj(item, p, ["id", "name", "description", "gates", "evals"]);
    return x === null
      ? []
      : [{ id: r.str(x.id, `${p}.id`), name: r.str(x.name, `${p}.name`), description: r.str(x.description, `${p}.description`, ""), gates: readGates(r, x.gates, `${p}.gates`), evals: r.strs(x.evals, `${p}.evals`) }];
  });
  const assignments = r.list(po.assignments, `${path}.presets.assignments`).flatMap((item, i): PresetAssignment[] => {
    const p = `${path}.presets.assignments[${String(i)}]`;
    const x = r.obj(item, p, ["role", "policy_presets", "gate_preset"]);
    if (x === null) return [];
    const gate = x.gate_preset === undefined || x.gate_preset === null ? null : r.str(x.gate_preset, `${p}.gate_preset`);
    return [{ role: r.str(x.role, `${p}.role`), policy_presets: r.strs(x.policy_presets, `${p}.policy_presets`), gate_preset: gate }];
  });
  const evals = r.list(o.evals, `${path}.evals`).flatMap((item, i): EvalRequirement[] => {
    const p = `${path}.evals[${String(i)}]`;
    const x = r.obj(item, p, ["role", "suites"]);
    return x === null ? [] : [{ role: r.str(x.role, `${p}.role`), suites: r.strs(x.suites, `${p}.suites`) }];
  });
  const checks = r.list(o.checks, `${path}.checks`).flatMap((item, i): RoleChecks[] => {
    const p = `${path}.checks[${String(i)}]`;
    const x = r.obj(item, p, ["role", "checks"]);
    if (x === null) return [];
    const byGate: Record<string, string[]> = Object.create(null) as Record<string, string[]>;
    if (!isObject(x.checks)) r.fail(`${p}.checks`, "Must be an object");
    else for (const [gate, criteria] of Object.entries(x.checks)) byGate[gate] = r.strs(criteria, `${p}.checks.${gate}`);
    return [{ role: r.str(x.role, `${p}.role`), checks: { ...byGate } }];
  });
  if (r.errors.length > 0) return { ok: false, errors: r.errors };
  return { ok: true, value: { policies, budgets, presets: { policy: policyPresets, gate: gatePresets, assignments }, evals, checks } };
}

export interface BuilderUi {
  people: Person[];
  appearance: Record<string, Appearance>;
}

export const emptyUi = (): BuilderUi => ({ people: [], appearance: {} });

/** Parses the `ui` part of a builder document (people on the canvas, node appearance). */
export function parseUi(raw: unknown, path = "ui"): ConfigParse<BuilderUi> {
  if (raw === undefined) return { ok: true, value: emptyUi() };
  const r = new Reader();
  const o = r.obj(raw, path, ["people", "appearance"]);
  if (o === null) return { ok: false, errors: r.errors };
  const people = r.list(o.people, `${path}.people`).flatMap((item, i): Person[] => {
    const p = `${path}.people[${String(i)}]`;
    const x = r.obj(item, p, ["id", "label"]);
    if (x === null) return [];
    const id = r.str(x.id, `${p}.id`);
    if (!HUMAN_ID.test(id)) r.fail(`${p}.id`, "Must be an opaque user id (human:u_…)");
    return [{ id, label: r.str(x.label, `${p}.label`) }];
  });
  const appearance: Record<string, Appearance> = Object.create(null) as Record<string, Appearance>;
  if (o.appearance !== undefined) {
    if (!isObject(o.appearance)) r.fail(`${path}.appearance`, "Must be an object");
    else {
      for (const [id, value] of Object.entries(o.appearance)) {
        const p = `${path}.appearance.${id}`;
        const x = r.obj(value, p, ["avatar", "color"]);
        if (x === null) continue;
        const entry: Appearance = {};
        if (x.avatar !== undefined) entry.avatar = r.str(x.avatar, `${p}.avatar`).slice(0, 3);
        if (x.color !== undefined) {
          const color = r.str(x.color, `${p}.color`);
          if (!(ACCENTS as readonly string[]).includes(color)) r.fail(`${p}.color`, `Must be one of ${ACCENTS.join(", ")}`);
          entry.color = color;
        }
        appearance[id] = entry;
      }
    }
  }
  if (r.errors.length > 0) return { ok: false, errors: r.errors };
  return { ok: true, value: { people, appearance } };
}

/** Parses a list of permission rules (the inspector's YAML tab and imports use it). */
export function parseRules(raw: unknown, path: string): ConfigParse<PolicyRule[]> {
  const r = new Reader();
  const rules = readRules(r, raw, path);
  return r.errors.length > 0 ? { ok: false, errors: r.errors } : { ok: true, value: rules };
}

/** Parses an ordered list of gates. */
export function parseGates(raw: unknown, path: string): ConfigParse<GatePresetGate[]> {
  const r = new Reader();
  const gates = readGates(r, raw, path);
  return r.errors.length > 0 ? { ok: false, errors: r.errors } : { ok: true, value: gates };
}
