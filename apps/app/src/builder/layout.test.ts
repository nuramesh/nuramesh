import { describe, expect, it } from "vitest";

import { createInlineEngine } from "./layout-engine";
import { fromElkResult, NODE_HEIGHT, NODE_WIDTH, provisionalPosition, reportingLines, structureKey, toElkGraph } from "./layout";
import { agent, sampleDoc } from "./test-data";
import { BUILT_IN_TEMPLATES } from "./templates";

describe("layout mapping", () => {
  it("maps agents to fixed-size ELK nodes and reporting lines to manager → report edges", () => {
    const org = sampleDoc().org;
    const graph = toElkGraph(org);
    expect(graph.layoutOptions?.["elk.algorithm"]).toBe("layered");
    expect(graph.layoutOptions?.["elk.direction"]).toBe("DOWN");
    expect(graph.children?.map((c) => [c.id, c.width, c.height])).toEqual(
      ["boss", "a", "a1", "b"].map((id) => [id, NODE_WIDTH, NODE_HEIGHT]),
    );
    expect(graph.edges?.map((e) => [e.sources[0], e.targets[0]])).toEqual([
      ["boss", "a"],
      ["a", "a1"],
      ["boss", "b"],
    ]);
  });

  it("skips lines that can't be drawn and duplicate ids", () => {
    const org = {
      ...sampleDoc().org,
      agents: [agent("x", "dev", "x"), agent("y", "dev", "ghost"), agent("z", "dev", "x"), agent("z", "dev", null)],
    };
    expect(reportingLines(org)).toEqual([{ manager: "x", report: "z" }]);
    expect(toElkGraph(org).children).toHaveLength(3);
  });

  it("structure key changes only with agents and lines", () => {
    const doc = sampleDoc();
    const renamed = { ...doc.org, name: "Other", agents: doc.org.agents.map((a) => ({ ...a, name: "N" })) };
    expect(structureKey(renamed)).toBe(structureKey(doc.org));
    const moved = { ...doc.org, agents: doc.org.agents.map((a) => (a.id === "a1" ? { ...a, reports_to: "b" } : a)) };
    expect(structureKey(moved)).not.toBe(structureKey(doc.org));
  });

  it("reads positions from ELK output and places pending agents below their manager", () => {
    expect(fromElkResult({ id: "root", children: [{ id: "a", x: 5, y: 6 }, { id: "b" }] })).toEqual({
      a: { x: 5, y: 6 },
      b: { x: 0, y: 0 },
    });
    const below = provisionalPosition({ x: 100, y: 50 }, 1, 0);
    expect(below.y).toBeGreaterThan(50 + NODE_HEIGHT);
    expect(below.x).toBeGreaterThan(100);
    expect(provisionalPosition(undefined, 0, 400).y).toBeGreaterThan(400);
  });
});

describe("ELK layout (real engine)", () => {
  it("places every manager above its reports and keeps siblings apart", async () => {
    const engine = createInlineEngine();
    const template = BUILT_IN_TEMPLATES[0];
    if (template === undefined) throw new Error("no template");
    const positions = await engine.layout(toElkGraph(template.document.org));
    for (const { manager, report } of reportingLines(template.document.org)) {
      expect(positions[report]?.y ?? 0).toBeGreaterThanOrEqual((positions[manager]?.y ?? 0) + NODE_HEIGHT);
    }
    const reports = template.document.org.agents.filter((a) => a.reports_to !== null).map((a) => positions[a.id]?.x ?? 0);
    const sorted = [...reports].sort((a, b) => a - b);
    sorted.slice(1).forEach((x, i) => {
      expect(x - (sorted[i] ?? 0)).toBeGreaterThanOrEqual(NODE_WIDTH);
    });
    engine.dispose();
  });
});
