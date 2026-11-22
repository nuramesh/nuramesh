import { describe, expect, it } from "vitest";

import {
  addAgent,
  addRole,
  clearPositions,
  ensureRole,
  removeAgents,
  removeReportingLines,
  removeRole,
  renameAgent,
  renameRole,
  setManager,
  setPositions,
  uniqueId,
  updateAgent,
  updateOrg,
  updateRole,
  wouldCreateCycle,
} from "./ops";
import { cos, dev, sampleDoc } from "./test-data";

describe("uniqueId", () => {
  it("keeps a free id, numbers taken ones and stays a valid slug", () => {
    expect(uniqueId("dev", new Set())).toBe("dev");
    expect(uniqueId("dev", new Set(["dev", "dev-2"]))).toBe("dev-3");
    expect(uniqueId("New Role!", new Set())).toBe("new-role-");
    expect(uniqueId("--", new Set())).toBe("agent");
    const long = "a".repeat(63);
    const id = uniqueId(long, new Set([long]));
    expect(id).toHaveLength(63);
    expect(id.endsWith("-2")).toBe(true);
  });
});

describe("agent edits", () => {
  it("adds an agent with a free id, numbered name, manager and role", () => {
    const doc = sampleDoc();
    const { doc: next, id } = addAgent(doc, { role: dev, reportsTo: "boss" });
    expect(id).toBe("dev");
    expect(next.org.agents.at(-1)).toEqual({ id: "dev", name: "Developer 4", role: "dev", reports_to: "boss", model: null, checks: [] });
    expect(next.layout).toBe(doc.layout); // placed by layout, nothing pinned
    expect(next.org.roles).toBe(doc.org.roles); // structural sharing
  });

  it("adds the role when it's new, pins a dropped agent and ignores unknown managers", () => {
    const qa = { ...dev, id: "qa", name: "QA" };
    const { doc, id } = addAgent(sampleDoc(), { role: qa, reportsTo: "ghost", position: { x: 1, y: 2 } });
    expect(doc.org.roles.map((r) => r.id)).toContain("qa");
    expect(doc.org.agents.at(-1)?.reports_to).toBeNull();
    expect(doc.layout.positions[id]).toEqual({ x: 1, y: 2 });
  });

  it("returns the same document for no-op edits", () => {
    const doc = sampleDoc();
    expect(updateAgent(doc, "a", { name: "A" })).toBe(doc);
    expect(setManager(doc, "a", "boss")).toBe(doc);
    expect(updateOrg(doc, { name: "Acme" })).toBe(doc);
    expect(setPositions(doc, { a: { x: 10, y: 20 } })).toBe(doc);
    expect(removeAgents(doc, new Set())).toBe(doc);
    expect(clearPositions(clearPositions(doc))).toEqual(clearPositions(doc));
  });

  it("detects reporting loops", () => {
    const doc = sampleDoc();
    expect(wouldCreateCycle(doc, "a1", "a")).toBe(true); // a1 is below a
    expect(wouldCreateCycle(doc, "a1", "boss")).toBe(true);
    expect(wouldCreateCycle(doc, "a", "a")).toBe(true);
    expect(wouldCreateCycle(doc, "b", "a")).toBe(false);
  });

  it("renames an agent with its reports and pinned position", () => {
    const next = renameAgent(sampleDoc(), "a", "alpha");
    expect(next.org.agents.map((x) => [x.id, x.reports_to])).toEqual([
      ["boss", null],
      ["alpha", "boss"],
      ["a1", "alpha"],
      ["b", "boss"],
    ]);
    expect(next.layout.positions).toEqual({ alpha: { x: 10, y: 20 } });
  });

  it("removing agents moves their reports up to the nearest manager that stays", () => {
    const next = removeAgents(sampleDoc(), new Set(["a"]));
    expect(next.org.agents.map((x) => [x.id, x.reports_to])).toEqual([
      ["boss", null],
      ["a1", "boss"],
      ["b", "boss"],
    ]);
    expect(next.layout.positions).toEqual({});
    const all = removeAgents(sampleDoc(), new Set(["boss", "a"]));
    expect(all.org.agents.map((x) => [x.id, x.reports_to])).toEqual([
      ["a1", null],
      ["b", null],
    ]);
  });

  it("removes reporting lines", () => {
    const next = removeReportingLines(sampleDoc(), new Set(["a1", "boss"]));
    expect(next.org.agents.find((x) => x.id === "a1")?.reports_to).toBeNull();
  });
});

describe("role edits", () => {
  it("adds, updates and renames roles; agents follow a rename", () => {
    const { doc, id } = addRole(sampleDoc());
    expect(id).toBe("new-role");
    const edited = updateRole(doc, id, { kind: "verifier", tools: ["git"] });
    expect(edited.org.roles.at(-1)).toMatchObject({ kind: "verifier", tools: ["git"] });
    expect(updateRole(edited, id, { tools: ["git"] })).toBe(edited);
    const renamed = renameRole(edited, "dev", "developer");
    expect(renamed.org.agents.filter((a) => a.role === "developer")).toHaveLength(3);
  });

  it("deletes only unused roles and ensures roles once", () => {
    const doc = sampleDoc();
    expect(removeRole(doc, "dev")).toBe(doc);
    const { doc: withNew, id } = addRole(doc);
    expect(removeRole(withNew, id).org.roles).toHaveLength(2);
    expect(ensureRole(doc, cos)).toBe(doc);
  });
});
