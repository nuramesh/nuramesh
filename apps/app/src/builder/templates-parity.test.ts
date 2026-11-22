import { describe, expect, it } from "vitest";
import fixture from "./package-templates.json";
import { BUILT_IN_TEMPLATES } from "./templates";

interface PackageTemplate {
  id: string;
  roles: { id: string; kind: string }[];
  agents: { id: string; role: string; reports_to: string | null }[];
}

const PACKAGE = fixture as PackageTemplate[];

describe("built-in templates match the package templates (#1260)", () => {
  it("has the same template ids", () => {
    expect(BUILT_IN_TEMPLATES.map((t) => t.id).sort()).toEqual(PACKAGE.map((t) => t.id).sort());
  });

  it.each(PACKAGE.map((t) => [t.id, t] as const))("%s: roles and agents agree", (id, pkg) => {
    const app = BUILT_IN_TEMPLATES.find((t) => t.id === id);
    if (app === undefined) throw new Error(`no app template ${id}`);
    const org = app.document.org;
    expect(org.roles.map((r) => ({ id: r.id, kind: r.kind }))).toEqual(pkg.roles);
    expect(org.agents.map((a) => ({ id: a.id, role: a.role, reports_to: a.reports_to }))).toEqual(pkg.agents);
  });
});
