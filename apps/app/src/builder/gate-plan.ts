/**
 * The run planner's gate step: a mirror of `nuramesh.core.presets` (`instantiate_gates`,
 * `gates_for_role`, `effective_suites`). Same input, same result; error text is the same except where noted in the fixture (an invalid criterion id is rejected by both, worded differently). The shared
 * fixture `spec/gate-instantiation/cases.json` is checked against both implementations.
 */
import type { EvalRequirement, GatePresetDef, OrgConfig } from "@/model/config";

/** `checks[gate id]` names the criteria each gate of a preset checks (the TS side of `checks`). */
export type GateChecks = Readonly<Record<string, readonly string[]>>;

/** A real gate for a task (the core's `GateSpec`). */
export interface GateSpec {
  id: string;
  kind: GatePresetDef["gates"][number]["kind"];
  checks: string[];
  required: boolean;
  config: Record<string, unknown>;
}

const SLUG = /^[a-z0-9][a-z0-9_-]{0,62}$/;

/** Python's list repr for strings, so messages match the core's word for word. */
function pyList(items: readonly string[]): string {
  return `[${items.map((i) => `'${i}'`).join(", ")}]`;
}

function cloneConfig(config: Record<string, unknown> | undefined): Record<string, unknown> {
  return config === undefined ? {} : structuredClone(config);
}

/** Real gates from a preset. Every gate needs criteria and none may be for a gate the preset lacks. */
export function instantiateGates(preset: GatePresetDef, checks: GateChecks): GateSpec[] {
  const known = new Set(preset.gates.map((g) => g.id));
  const extra = Object.keys(checks).filter((id) => !known.has(id)).sort();
  if (extra.length > 0) throw new Error(`checks for gates the preset doesn't have: ${pyList(extra)}`);
  const missing = preset.gates.filter((g) => (checks[g.id] ?? []).length === 0).map((g) => g.id);
  if (missing.length > 0) throw new Error(`say which criteria these gates check: ${pyList(missing)}`);
  return preset.gates.map((g) => {
    const criteria = [...(checks[g.id] ?? [])];
    const bad = criteria.find((c) => !SLUG.test(c));
    if (bad !== undefined) throw new Error(`gate '${g.id}': '${bad}' is not a valid criterion id`);
    if (new Set(criteria).size !== criteria.length) throw new Error("a gate lists the same criterion twice");
    return { id: g.id, kind: g.kind, checks: criteria, required: g.required, config: cloneConfig(g.config) };
  });
}

/** The gate preset a role uses, if any. */
export function gatePresetForRole(config: OrgConfig, role: string): GatePresetDef | null {
  const assignment = config.presets.assignments.find((a) => a.role === role);
  if (assignment === undefined || assignment.gate_preset === null) return null;
  return config.presets.gate.find((g) => g.id === assignment.gate_preset) ?? null;
}

/** The gates a task for `role` must carry; `null` when the role has no gate preset. */
export function gatesForRole(config: OrgConfig, role: string, checks: GateChecks): GateSpec[] | null {
  const preset = gatePresetForRole(config, role);
  return preset === null ? null : instantiateGates(preset, checks);
}

/** The eval suite ids a role must pass: its own requirements, then its gate preset's, each once. */
export function effectiveSuites(config: OrgConfig, role: string): string[] {
  const own = config.evals.find((e: EvalRequirement) => e.role === role);
  const ids: string[] = own === undefined ? [] : [...own.suites];
  for (const id of gatePresetForRole(config, role)?.evals ?? []) if (!ids.includes(id)) ids.push(id);
  return ids;
}
