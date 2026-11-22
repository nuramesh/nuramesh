import { describe, expect, it } from "vitest";

import type { GatePresetDef, OrgConfig } from "@/model/config";
import { emptyConfig, parseConfig } from "@/model/config";

import { effectiveSuites, gatesForRole, instantiateGates } from "./gate-plan";

interface GateCase {
  name: string;
  preset: { id: string; gates: GatePresetDef["gates"] };
  checks: Record<string, string[]>;
  expect?: unknown;
  error?: string;
  error_any?: boolean;
}
interface SuiteCase {
  name: string;
  evals: { role: string; suites: string[] }[];
  presets: { gate: (Omit<GatePresetDef, "name" | "description"> & Partial<GatePresetDef>)[]; assignments: { role: string; gate_preset: string }[] };
  role: string;
  expect: string[];
}

// The same file tests/test_gate_instantiation_fixture.py checks against nuramesh.core.
const raw = import.meta.glob<string>("../../../../spec/gate-instantiation/cases.json", { eager: true, query: "?raw", import: "default" });
const fixture = JSON.parse(Object.values(raw)[0] ?? "{}") as {
  gates: GateCase[];
  suites: SuiteCase[];
};

function first<T>(items: readonly T[]): T {
  const item = items[0];
  if (item === undefined) throw new Error("fixture is empty");
  return item;
}

function presetOf(raw: GateCase["preset"]): GatePresetDef {
  return { name: raw.id, description: "", evals: [], ...raw };
}

describe("instantiateGates matches nuramesh.core", () => {
  it.each(fixture.gates.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    if (c.error_any === true) {
      // Both reject; the wording differs (core's comes from pydantic), so only the rejection is compared.
      expect(() => instantiateGates(presetOf(c.preset), c.checks)).toThrow();
      return;
    }
    if (c.error !== undefined) {
      expect(() => instantiateGates(presetOf(c.preset), c.checks)).toThrow(c.error);
      return;
    }
    expect(instantiateGates(presetOf(c.preset), c.checks)).toEqual(c.expect);
  });

  it("does not share config with the preset", () => {
    const c = first(fixture.gates);
    const preset = presetOf(c.preset);
    const gate = first(instantiateGates(preset, c.checks));
    gate.config.max = 99;
    expect(first(preset.gates).config?.max).toBe(3);
  });
});

describe("effectiveSuites matches nuramesh.core", () => {
  it.each(fixture.suites.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const config: OrgConfig = {
      ...emptyConfig(),
      evals: c.evals,
      presets: {
        policy: [],
        gate: c.presets.gate.map(presetOf2),
        assignments: c.presets.assignments.map((a) => ({ policy_presets: [], ...a })),
      },
    };
    expect(effectiveSuites(config, c.role)).toEqual(c.expect);
  });
});

function presetOf2(raw: SuiteCase["presets"]["gate"][number]): GatePresetDef {
  return { name: raw.id, description: "", ...raw };
}

describe("gatesForRole", () => {
  const preset = presetOf(first(fixture.gates).preset);
  const config: OrgConfig = {
    ...emptyConfig(),
    presets: { policy: [], gate: [preset], assignments: [{ role: "dev", policy_presets: [], gate_preset: "dev" }] },
  };
  it("is null for a role without a gate preset", () => {
    expect(gatesForRole(config, "other", {})).toBeNull();
    expect(gatesForRole(emptyConfig(), "dev", {})).toBeNull();
  });
  it("instantiates the role's preset", () => {
    expect(gatesForRole(config, "dev", first(fixture.gates).checks)).toEqual(first(fixture.gates).expect);
  });
});

describe("the builder loads every fixture preset, gate config included", () => {
  it.each(fixture.gates.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const preset = presetOf(c.preset);
    const parsed = parseConfig({ presets: { gate: [preset] } });
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.value.presets.gate[0]?.gates).toEqual(preset.gates);
  });

  it("refuses a gate config the core refuses", () => {
    const bad = (config: unknown) => parseConfig({ presets: { gate: [{ id: "p", name: "p", gates: [{ id: "g", kind: "rule", config }] }] } }).ok;
    expect(bad({ ratio: 0.5 })).toBe(false);
    expect(bad([1])).toBe(false);
    expect(bad({ a: { b: [1, "x", null, true] } })).toBe(true);
  });
});
