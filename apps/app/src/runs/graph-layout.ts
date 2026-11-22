/**
 * A small layered layout for the live run graph: a few agents, people and verifiers, so no layout
 * engine is needed. Nodes sit on layers by the longest path from a source; each layer is centred.
 */
import type { GraphEdge, GraphNode } from "@/api/types";

export const RUN_NODE_WIDTH = 196;
export const RUN_NODE_HEIGHT = 64;
const GAP_X = 28;
const GAP_Y = 64;
const PAD = 24;

export interface PlacedNode {
  node: GraphNode;
  x: number;
  y: number;
}

export interface PlacedEdge {
  edge: GraphEdge;
  /** SVG path from the bottom of `from` to the top of `to`. */
  d: string;
  key: string;
}

export interface RunGraphLayout {
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
}

/** Layer of every node: 0 for a source, otherwise one more than its deepest predecessor (cycles are cut). */
export function layers(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): Map<string, number> {
  const ids = new Set(nodes.map((n) => n.id));
  const incoming = new Map<string, string[]>();
  for (const e of edges) {
    if (!ids.has(e.from) || !ids.has(e.to) || e.from === e.to) continue;
    incoming.set(e.to, [...(incoming.get(e.to) ?? []), e.from]);
  }
  const memo = new Map<string, number>();
  const visiting = new Set<string>();
  const depth = (id: string): number => {
    const known = memo.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0; // a cycle: treat the back edge as absent
    visiting.add(id);
    const level = Math.max(-1, ...(incoming.get(id) ?? []).map(depth)) + 1;
    visiting.delete(id);
    memo.set(id, level);
    return level;
  };
  for (const n of nodes) depth(n.id);
  return memo;
}

export function layoutRunGraph(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): RunGraphLayout {
  const level = layers(nodes, edges);
  const rows = new Map<number, GraphNode[]>();
  for (const n of nodes) rows.set(level.get(n.id) ?? 0, [...(rows.get(level.get(n.id) ?? 0) ?? []), n]);
  const widest = Math.max(1, ...[...rows.values()].map((r) => r.length));
  const width = PAD * 2 + widest * RUN_NODE_WIDTH + (widest - 1) * GAP_X;
  const depth = rows.size === 0 ? 1 : Math.max(...rows.keys()) + 1;
  const height = PAD * 2 + depth * RUN_NODE_HEIGHT + (depth - 1) * GAP_Y;
  const placed = new Map<string, PlacedNode>();
  for (const [row, members] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
    const rowWidth = members.length * RUN_NODE_WIDTH + (members.length - 1) * GAP_X;
    const left = (width - rowWidth) / 2;
    members.forEach((node, i) => {
      placed.set(node.id, { node, x: left + i * (RUN_NODE_WIDTH + GAP_X), y: PAD + row * (RUN_NODE_HEIGHT + GAP_Y) });
    });
  }
  const placedEdges: PlacedEdge[] = [];
  edges.forEach((edge, index) => {
    const a = placed.get(edge.from);
    const b = placed.get(edge.to);
    if (a === undefined || b === undefined || a === b) return;
    const x1 = a.x + RUN_NODE_WIDTH / 2;
    const y1 = a.y + RUN_NODE_HEIGHT;
    const x2 = b.x + RUN_NODE_WIDTH / 2;
    const y2 = b.y;
    const mid = (y1 + y2) / 2;
    placedEdges.push({ edge, d: `M${String(x1)} ${String(y1)} C${String(x1)} ${String(mid)} ${String(x2)} ${String(mid)} ${String(x2)} ${String(y2)}`, key: `${edge.from}>${edge.to}:${edge.kind}:${String(index)}` });
  });
  return { width, height, nodes: [...placed.values()], edges: placedEdges };
}
