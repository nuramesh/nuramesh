import { describe, expect, it } from "vitest";

import fieldCases from "../../../../spec/org-fields/cases.json";
import { agentFieldErrors, fieldRules, makeAgent, makeRole, parseOrg, pyStrip, roleFieldErrors } from "./org";

interface FieldCase {
  description: string;
  org: unknown;
  ok: boolean;
  paths?: string[];
  names?: { org: string; role: string; agent: string };
}

describe("parseOrg agrees with pydantic OrgFields (spec/org-fields/cases.json, shared with Python)", () => {
  it.each((fieldCases.cases as FieldCase[]).map((c) => [c.description, c] as const))("%s", (_name, c) => {
    const result = parseOrg(c.org);
    expect(result.ok).toBe(c.ok);
    if (!result.ok) {
      expect([...new Set(result.errors.map((e) => e.path))].sort()).toEqual([...(c.paths ?? [])].sort());
    } else if (c.names) {
      expect({
        org: result.value.name,
        role: result.value.roles[0]?.name,
        agent: result.value.agents[0]?.name,
      }).toEqual(c.names);
    }
  });
});

describe("parseOrg", () => {
  it("applies the Python model's defaults", () => {
    const result = parseOrg({
      id: "acme",
      name: "  Acme  ",
      roles: [{ id: "cos", name: "Chief", kind: "chief_of_staff" }, { id: "dev", name: "Dev" }],
      agents: [{ id: "boss", name: "Boss", role: "cos" }],
    });
    expect(result).toEqual({
      ok: true,
      value: {
        id: "acme",
        name: "Acme",
        roles: [
          makeRole({ id: "cos", name: "Chief", kind: "chief_of_staff" }),
          makeRole({ id: "dev", name: "Dev", kind: "worker" }),
        ],
        agents: [makeAgent({ id: "boss", name: "Boss", role: "cos", reports_to: null })],
      },
    });
  });

  it("rejects an empty organization unless explicitly allowed (builder drafts)", () => {
    const empty = { id: "blank", name: "Blank", roles: [], agents: [] };
    const strict = parseOrg(empty);
    expect(strict.ok).toBe(false);
    if (!strict.ok) expect(strict.errors.map((e) => e.path)).toEqual(["org.roles", "org.agents"]);
    expect(parseOrg(empty, { allowEmpty: true }).ok).toBe(true);
  });

  it("reports every malformed field with its path", () => {
    const result = parseOrg({
      id: "Bad Id",
      name: "",
      roles: [{ id: "dev", name: "Dev", kind: "boss", tools: ["ok", "Not OK"], extra: 1 }, "nope"],
      agents: [{ id: "a", name: "A", role: "dev", reports_to: 7, model: "" }],
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors.map((e) => e.path)).toEqual([
      "org.id",
      "org.name",
      "org.roles[0].extra",
      "org.roles[0].kind",
      "org.roles[0].tools[1]",
      "org.roles[1]",
      "org.agents[0].reports_to",
      "org.agents[0].model",
    ]);
  });

  it("rejects missing required fields and non-objects", () => {
    expect(parseOrg(null)).toEqual({ ok: false, errors: [{ path: "org", message: "Must be an object" }] });
    const result = parseOrg({ roles: {}, agents: [{}] });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors.map((e) => `${e.path}: ${e.message}`)).toEqual([
      "org.id: Required",
      "org.name: Required",
      "org.roles: Must be a list",
      "org.agents[0].id: Required",
      "org.agents[0].name: Required",
      "org.agents[0].role: Required",
    ]);
  });
});

describe("field rules", () => {
  it("strips names like pydantic (Unicode White_Space; not U+FEFF, not U+001C)", () => {
    expect(pyStrip("\u0085　 a  \t")).toBe("a");
    expect(pyStrip("﻿a")).toBe("﻿a");
    expect(pyStrip("\u001ca")).toBe("\u001ca");
    expect(fieldRules.name("  ")).toBe("Name can't be empty");
  });

  it("counts characters as Python does (code points)", () => {
    const emoji = "😀".repeat(120);
    expect(fieldRules.name(emoji)).toBeNull();
    expect(fieldRules.name(`${emoji}x`)).not.toBeNull();
    expect(fieldRules.model(null)).toBeNull();
  });

  it("lists role and agent field errors with a prefix", () => {
    expect(roleFieldErrors(makeRole({ id: "X", name: " " }), "r.")).toEqual([
      { path: "r.id", message: expect.any(String) as string },
      { path: "r.name", message: "Name can't be empty" },
    ]);
    expect(agentFieldErrors(makeAgent({ id: "a", name: "A", role: "dev", reports_to: "B!" }))).toEqual([
      { path: "reports_to", message: expect.any(String) as string },
    ]);
  });
});
