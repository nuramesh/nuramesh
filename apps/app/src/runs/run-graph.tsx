/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- a scrollable region must be focusable so a keyboard can scroll it */
import { useMemo } from "react";

import type { GraphEdge, GraphNode } from "@/api/types";
import { cn } from "@/lib/utils";

import { NODE_STYLE } from "./format";
import { layoutRunGraph, RUN_NODE_HEIGHT, RUN_NODE_WIDTH } from "./graph-layout";

const EDGE_NOTE: Record<GraphEdge["kind"], string> = { reports: "reports to", checks: "is checked by", approval: "asks for approval" };

function edgeStyle(edge: GraphEdge): { stroke: string; width: number; dash?: string } {
  const color = edge.state === "done" ? "var(--t-link)" : edge.state === "active" ? "var(--t-warm)" : "var(--t-line)";
  // Approval lines are dashed (a person, not an agent); a check line is solid in the accent colour.
  if (edge.kind === "approval") return { stroke: color, width: 1.6, dash: "5 5" };
  return { stroke: color, width: edge.state === "idle" ? 1.4 : 2 };
}

/** The run's agents as they work: status dot and words on each node, line colour by state. */
export function RunGraph({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const layout = useMemo(() => layoutRunGraph(nodes, edges), [nodes, edges]);
  const names = new Map(nodes.map((n) => [n.id, n.name]));
  return (
    <div data-phone-scroll="" tabIndex={0} role="region" aria-label="Run graph (scrolls sideways on narrow screens)" className="overflow-x-auto bg-canvas">
      <div className="relative mx-auto" style={{ width: layout.width, height: layout.height }}>
        <svg width={layout.width} height={layout.height} viewBox={`0 0 ${String(layout.width)} ${String(layout.height)}`} aria-hidden="true" className="absolute inset-0">
          {layout.edges.map(({ edge, d, key }) => {
            const style = edgeStyle(edge);
            return <path key={key} d={d} fill="none" stroke={style.stroke} strokeWidth={style.width} strokeDasharray={style.dash} strokeLinecap="round" />;
          })}
        </svg>
        <ul aria-label="Agents in this run" className="m-0 list-none p-0">
          {layout.nodes.map(({ node, x, y }) => {
            const style = NODE_STYLE[node.status];
            const links = edges.filter((e) => e.to === node.id).map((e) => `${EDGE_NOTE[e.kind]} ${names.get(e.from) ?? e.from}`);
            return (
              <li
                key={node.id}
                data-testid={`run-node-${node.id}`}
                data-status={node.status}
                style={{ left: x, top: y, width: RUN_NODE_WIDTH, height: RUN_NODE_HEIGHT }}
                className={cn(
                  "absolute flex items-center gap-2.5 rounded-xl border-[1.5px] bg-panel px-3",
                  node.status === "running" || node.status === "waiting" ? "border-warm" : "border-border-strong",
                )}
              >
                <span
                  className={cn(
                    "relative flex size-[34px] shrink-0 items-center justify-center border-[1.5px] bg-secondary font-mono text-[10.5px] font-semibold",
                    node.isHuman ? "rounded-full border-dashed border-warm text-warm" : node.isVerifier ? "rounded-md border-link text-link" : "rounded-lg border-border-strong text-secondary-foreground",
                  )}
                >
                  {node.abbr}
                  <span aria-hidden="true" className={cn("absolute -top-1 -right-1 size-2.5 rounded-full", style.dot)} />
                </span>
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="truncate text-[13.5px] font-semibold">{node.name}</span>
                  <span className={cn("truncate font-mono text-[11px]", style.text)}>{node.statusText}</span>
                  {links.length > 0 && <span className="sr-only">{links.join(", ")}</span>}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

/** What the colours mean, in words (the design's legend). */
export function GraphLegend() {
  return (
    <ul className="m-0 flex list-none flex-wrap items-center gap-x-4 gap-y-1 p-0 text-xs text-muted-foreground">
      <li className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2 rounded-full bg-link" />
        done
      </li>
      <li className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2 rounded-full bg-warm" />
        running or waiting
      </li>
      <li className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="size-2 rounded-full border-[1.5px] border-small" />
        not started
      </li>
    </ul>
  );
}
