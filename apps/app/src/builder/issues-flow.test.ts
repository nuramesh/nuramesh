import { describe, expect, it } from "vitest";

import { EdgeCache, NodeCache } from "./flow";
import { computeIssues } from "./issues";
import { setManager, setPositions, updateAgent } from "./ops";
import { sampleDoc } from "./test-data";
import { BUILT_IN_TEMPLATES } from "./templates";

describe("computeIssues", () => {
  it("combines field errors and graph rules, errors first, with targets", () => {
    const doc = sampleDoc();
    const broken = {
      ...doc.org,
      name: " ",
      roles: [...doc.org.roles, { ...doc.org.roles[1], id: "spare", name: "Spare", kind: "worker" as const, description: "", instructions: "", tools: ["Bad"], default_model: null }],
      agents: doc.org.agents.map((a) => (a.id === "b" ? { ...a, reports_to: null } : a)),
    };
    const report = computeIssues(broken);
    expect(report.issues.map((i) => [i.code, i.target])).toEqual([
      ["invalid_field", { kind: "org" }],
      ["invalid_field", { kind: "role", id: "spare" }],
      ["multiple_roots", { kind: "agent", id: "boss" }],
      ["multiple_roots", { kind: "agent", id: "b" }],
      ["root_not_chief_of_staff", { kind: "agent", id: "b" }],
      ["unused_role", { kind: "role", id: "spare" }],
    ]);
    expect(report.errorCount).toBe(5);
    expect(report.warningCount).toBe(1);
    expect(report.byAgent.get("b")).toMatchObject({ errors: 2, warnings: 0 });
    expect(new Set(report.issues.map((i) => i.key)).size).toBe(report.issues.length);
  });

  it("built-in templates are valid", () => {
    for (const t of BUILT_IN_TEMPLATES) expect(computeIssues(t.document.org, t.document.config).issues).toEqual([]);
  });

  it("does a linear amount of work in the number of agents (load-independent; was a wall-clock bound)", () => {
    const doc = sampleDoc();
    // Count every read of an agent's fields: the work done grows with n, whatever the machine load.
    const reads = (n: number): number => {
      let count = 0;
      const agents = Array.from({ length: n }, (_, i) => {
        const base = {
          id: `a${String(i)}`,
          name: `A${String(i)}`,
          role: i === 0 ? "cos" : "dev",
          reports_to: i === 0 ? null : `a${String(Math.floor((i - 1) / 8))}`,
          model: null,
          checks: [],
        };
        return new Proxy(base, {
          get(target, key, receiver) {
            count += 1;
            return Reflect.get(target, key, receiver) as unknown;
          },
        });
      });
      const org = { ...doc.org, agents };
      computeIssues(org);
      return count;
    };
    const small = reads(250);
    const large = reads(500);
    expect(small).toBeGreaterThan(250); // the counter sees the work
    // Linear work doubles; quadratic work would quadruple.
    expect(large / small).toBeLessThan(2.6);
  });
});

describe("node and edge caches", () => {
  const input = (doc = sampleDoc()) => ({
    doc,
    computed: { boss: { x: 0, y: 0 }, a: { x: 0, y: 100 }, a1: { x: 0, y: 200 }, b: { x: 300, y: 100 } },
    dragging: null,
    selected: new Set<string>(),
    issues: computeIssues(doc.org).byAgent,
  });

  it("pinned positions win over layout; nodes carry role, kind and an accessible label", () => {
    const nodes = new NodeCache().build(input());
    expect(nodes.find((n) => n.id === "a")?.position).toEqual({ x: 10, y: 20 }); // pinned
    expect(nodes.find((n) => n.id === "b")?.position).toEqual({ x: 300, y: 100 });
    expect(nodes[0]?.data).toMatchObject({ roleName: "Chief of Staff", kind: "chief_of_staff" });
    expect(nodes[0]?.ariaLabel).toBe("Agent BOSS, Chief of Staff (Chief of Staff)");
  });

  it("reuses unchanged nodes and replaces only what changed", () => {
    const cache = new NodeCache();
    const doc = sampleDoc();
    const first = cache.build(input(doc));
    const moved = { ...input(doc), dragging: { b: { x: 1, y: 1 } } };
    const second = cache.build(moved);
    expect(second.filter((n, i) => n !== first[i]).map((n) => n.id)).toEqual(["b"]);
    const renamed = updateAgent(doc, "a1", { name: "New" });
    const third = cache.build({ ...moved, doc: renamed, issues: computeIssues(renamed.org).byAgent });
    expect(third.filter((n, i) => n !== second[i]).map((n) => n.id)).toEqual(["a1"]);
  });

  it("places agents layout hasn't reached yet below their manager, and keeps them there", () => {
    const cache = new NodeCache();
    const doc = { ...sampleDoc(), org: { ...sampleDoc().org, agents: [...sampleDoc().org.agents, { id: "new", name: "N", role: "dev", reports_to: "b", model: null, checks: [] }] } };
    const first = cache.build({ ...input(doc) }).find((n) => n.id === "new");
    expect(first?.position.y).toBeGreaterThan(100);
    const again = cache.build({ ...input(setPositions(doc, {})) }).find((n) => n.id === "new");
    expect(again?.position).toEqual(first?.position);
  });

  it("draws one edge per drawable reporting line, reusing unchanged edges", () => {
    const cache = new EdgeCache();
    const doc = sampleDoc();
    const first = cache.build(doc, new Set());
    expect(first.map((e) => [e.source, e.target])).toEqual([
      ["boss", "a"],
      ["a", "a1"],
      ["boss", "b"],
    ]);
    const next = setManager(doc, "a1", "b");
    const second = cache.build(next, new Set(["b"]));
    expect(second[0]).toBe(first[0]);
    expect(second[1]?.source).toBe("b");
    expect(second[2]?.selected).toBe(true);
  });
});
