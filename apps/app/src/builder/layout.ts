/**
 * Automatic layout: maps the organization to an ELK graph (layered, top-down: managers above
 * their reports) and ELK's result back to canvas positions. Pure; the ELK run itself happens in a
 * Web Worker (layout-engine.ts) so the canvas stays responsive.
 */
import type { ElkNode } from "elkjs/lib/elk-api";

import type { Org } from "@/model/org";

import type { XY } from "./document";

/** Fixed node size, so layout needs no DOM measurement. Keep in sync with AgentNode's styles. */
export const NODE_WIDTH = 256;
export const NODE_HEIGHT = 76;
const LAYER_GAP = 72;
const SIBLING_GAP = 32;

export const ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "DOWN",
  "elk.layered.spacing.nodeNodeBetweenLayers": String(LAYER_GAP),
  "elk.spacing.nodeNode": String(SIBLING_GAP),
  "elk.spacing.componentComponent": String(SIBLING_GAP * 2),
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  // Not set on purpose: "elk.layered.considerModelOrder.strategy" (keeps sibling order) makes a
  // 500-agent layout take ~4.6 s instead of ~60 ms; ELK edge routing is unused (React Flow draws
  // the edges). Layout is still deterministic for the same organization.
};

/** The reporting lines that can be drawn: manager exists, isn't the agent itself, first id wins. */
export function reportingLines(org: Org): { manager: string; report: string }[] {
  const ids = new Set(org.agents.map((a) => a.id));
  const seen = new Set<string>();
  const lines: { manager: string; report: string }[] = [];
  for (const agent of org.agents) {
    if (seen.has(agent.id)) continue;
    seen.add(agent.id);
    if (agent.reports_to !== null && agent.reports_to !== agent.id && ids.has(agent.reports_to)) {
      lines.push({ manager: agent.reports_to, report: agent.id });
    }
  }
  return lines;
}

/** Changes only when agents or reporting lines change (not names, roles or positions). */
export function structureKey(org: Org): string {
  return org.agents.map((a) => `${a.id}<${a.reports_to ?? ""}`).join("|");
}

export function toElkGraph(org: Org): ElkNode {
  const seen = new Set<string>();
  const children: ElkNode[] = [];
  for (const agent of org.agents) {
    if (seen.has(agent.id)) continue;
    seen.add(agent.id);
    children.push({ id: agent.id, width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  return {
    id: "root",
    layoutOptions: ELK_OPTIONS,
    children,
    edges: reportingLines(org).map(({ manager, report }) => ({
      id: `${manager}->${report}`,
      sources: [manager],
      targets: [report],
    })),
  };
}

export function fromElkResult(result: ElkNode): Record<string, XY> {
  const positions: Record<string, XY> = {};
  for (const child of result.children ?? []) {
    positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  }
  return positions;
}

/**
 * A place for an agent that layout hasn't placed yet (layout runs off the main thread, so a new
 * agent appears at once and moves into place when the result arrives): below its manager, or
 * below everything.
 */
export function provisionalPosition(
  managerPosition: XY | undefined,
  siblingIndex: number,
  lowestY: number,
): XY {
  if (managerPosition !== undefined) {
    return {
      x: managerPosition.x + siblingIndex * (NODE_WIDTH + SIBLING_GAP),
      y: managerPosition.y + NODE_HEIGHT + LAYER_GAP,
    };
  }
  return { x: siblingIndex * (NODE_WIDTH + SIBLING_GAP), y: lowestY + NODE_HEIGHT + LAYER_GAP };
}
