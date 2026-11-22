import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode, RunEvent } from "@/api/types";

import { filterEvents, lastSeq, mergeEvents } from "./events";
import { canPause, canStop, formatElapsed, formatStarted, isActive } from "./format";
import { layers, layoutRunGraph, RUN_NODE_HEIGHT, RUN_NODE_WIDTH } from "./graph-layout";

const event = (seq: number, category: RunEvent["category"] = "tasks"): RunEvent => ({
  seq,
  at: "2026-10-23T19:39:18Z",
  kind: "x",
  category,
  text: `event ${String(seq)}`,
  tone: "muted",
});

const node = (id: string): GraphNode => ({ id, name: id, abbr: id.slice(0, 2), status: "idle", statusText: "", isHuman: false, isVerifier: false });

describe("events", () => {
  it("merges by seq, newest first, without duplicates", () => {
    const first = mergeEvents([], [event(1), event(2)]);
    expect(first.map((e) => e.seq)).toEqual([2, 1]);
    const next = mergeEvents(first, [event(2), event(3), event(1)]);
    expect(next.map((e) => e.seq)).toEqual([3, 2, 1]);
  });

  it("returns the same list when nothing is new (no needless re-render)", () => {
    const first = mergeEvents([], [event(1)]);
    expect(mergeEvents(first, [event(1)])).toBe(first);
  });

  it("filters by category and keeps tasks only under All", () => {
    const all = [event(1, "tasks"), event(2, "gates"), event(3, "spend"), event(4, "approvals")];
    expect(filterEvents(all, "all")).toHaveLength(4);
    expect(filterEvents(all, "gates").map((e) => e.seq)).toEqual([2]);
    expect(filterEvents(all, "spend").map((e) => e.seq)).toEqual([3]);
    expect(filterEvents(all, "approvals").map((e) => e.seq)).toEqual([4]);
  });

  it("knows the cursor", () => {
    expect(lastSeq([])).toBe(0);
    expect(lastSeq([event(3), event(9), event(4)])).toBe(9);
  });
});

describe("format", () => {
  it.each([
    [0, "0s"],
    [45, "45s"],
    [192, "3m 12s"],
    [3_840, "1h 04m"],
    [-5, "0s"],
  ])("formats %i seconds as %s", (seconds, text) => {
    expect(formatElapsed(seconds)).toBe(text);
  });

  it("shows today's runs by time and older ones by date", () => {
    const now = new Date("2026-10-23T20:00:00");
    expect(formatStarted("2026-10-23T19:39:00", now)).toMatch(/^today 19:39$/);
    expect(formatStarted("2026-10-21T16:05:00", now)).toMatch(/^21 Oct 16:05$/);
    expect(formatStarted("not a date", now)).toBe("not a date");
  });

  it("allows pause and stop only where they make sense", () => {
    expect(canPause("running")).toBe(true);
    expect(canPause("waiting")).toBe(true);
    for (const s of ["paused", "done", "failed", "stopped", "blocked"] as const) expect(canPause(s)).toBe(false);
    expect(canStop("paused")).toBe(true);
    expect(canStop("blocked")).toBe(true);
    for (const s of ["done", "failed", "stopped"] as const) expect(canStop(s)).toBe(false);
    expect(isActive("done")).toBe(false);
  });
});

describe("run graph layout", () => {
  it("puts nodes on layers by their longest path and centres each layer", () => {
    const nodes = ["owner", "cos", "dev", "tester", "ver"].map(node);
    const edges: GraphEdge[] = [
      { from: "owner", to: "cos", kind: "approval", state: "active" },
      { from: "cos", to: "dev", kind: "reports", state: "done" },
      { from: "cos", to: "tester", kind: "reports", state: "active" },
      { from: "dev", to: "ver", kind: "checks", state: "done" },
      { from: "tester", to: "ver", kind: "checks", state: "idle" },
    ];
    const level = layers(nodes, edges);
    expect([...level.entries()]).toEqual([["owner", 0], ["cos", 1], ["dev", 2], ["tester", 2], ["ver", 3]]);
    const layout = layoutRunGraph(nodes, edges);
    const at = (id: string) => layout.nodes.find((n) => n.node.id === id);
    expect(at("dev")?.y).toBe(at("tester")?.y);
    expect(at("ver")?.y).toBeGreaterThan(at("dev")?.y ?? 0);
    // the two workers sit side by side, centred on the page, and nothing overlaps
    const dev = at("dev");
    const tester = at("tester");
    if (dev === undefined || tester === undefined) throw new Error("missing");
    expect(Math.abs(dev.x - tester.x)).toBeGreaterThanOrEqual(RUN_NODE_WIDTH);
    expect(layout.width).toBeGreaterThanOrEqual(2 * RUN_NODE_WIDTH);
    for (const n of layout.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(0);
      expect(n.x + RUN_NODE_WIDTH).toBeLessThanOrEqual(layout.width);
      expect(n.y + RUN_NODE_HEIGHT).toBeLessThanOrEqual(layout.height);
    }
    expect(layout.edges).toHaveLength(5);
    expect(layout.edges[0]?.d).toMatch(/^M[\d.]+ [\d.]+ C/);
  });

  it("survives loops, unknown ends and an empty graph", () => {
    const nodes = ["a", "b"].map(node);
    const edges: GraphEdge[] = [
      { from: "a", to: "b", kind: "reports", state: "idle" },
      { from: "b", to: "a", kind: "reports", state: "idle" },
      { from: "a", to: "ghost", kind: "reports", state: "idle" },
      { from: "a", to: "a", kind: "reports", state: "idle" },
    ];
    expect(() => layoutRunGraph(nodes, edges)).not.toThrow();
    expect(layoutRunGraph([], []).nodes).toEqual([]);
  });
});
