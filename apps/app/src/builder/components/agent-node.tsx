import { Handle, type NodeProps, Position } from "@xyflow/react";
import { AlertCircle, AlertTriangle } from "lucide-react";
import { memo } from "react";

import { cn } from "@/lib/utils";

import type { AgentFlowNode } from "../flow";
import { accentBarClass, avatarClass, avatarText } from "./appearance";
import { KindBadge } from "./kind-badge";

/**
 * One agent on the canvas: avatar, name, role kind and a model chip (dashed outline for an
 * open-weight or self-hosted model). Memoized: it re-renders only when its own node object changes
 * (the node cache keeps unchanged nodes identical), which keeps 500-node canvases at 60 fps.
 * Size is fixed (NODE_WIDTH × NODE_HEIGHT) so layout needs no measuring; NODE_WIDTH fits the longest
 * built-in model name on one line.
 *
 * Handles: top and bottom draw reporting lines; a verifier also has a right handle to draw its
 * verification lines; the left handle receives verification and approval lines.
 */
export const AgentNode = memo(function AgentNode({ data, selected }: NodeProps<AgentFlowNode>) {
  const { agent, roleName, kind, errors, warnings, issueText, model, openWeight, appearance } = data;
  return (
    <div
      className={cn(
        // Paint containment: 500 nodes re-raster on every pan/zoom frame without a GPU. The light-theme
        // shadow is on the wrapper `.react-flow__node-agent` (index.css), not here, so containment does not
        // clip it. Speed checks pass locally; on CI runners they are informational (T14).
        "flex h-full w-full items-center gap-2 rounded-lg border border-node-border bg-node px-2.5 text-card-foreground [contain:layout_paint_style]",
        accentBarClass(kind),
        selected && "border-ring ring-2 ring-ring",
        errors > 0 && !selected && "border-destructive",
      )}
    >
      <Handle type="target" position={Position.Top} className="!size-2.5 !bg-muted-foreground" />
      <Handle id="side-in" type="target" position={Position.Left} className="!size-2.5 !bg-muted-foreground" />
      <span
        aria-hidden="true"
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-lg font-mono text-[11px] font-semibold",
          avatarClass(appearance, kind),
        )}
      >
        {avatarText(appearance, agent.name)}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-sm font-semibold">{agent.name}</span>
          {errors > 0 ? (
            <span title={issueText} className="flex shrink-0 items-center gap-0.5 text-xs font-medium text-destructive">
              <AlertCircle aria-hidden="true" className="size-3.5" />
              {errors}
            </span>
          ) : warnings > 0 ? (
            <span title={issueText} className="flex shrink-0 items-center gap-0.5 text-xs font-medium text-warning">
              <AlertTriangle aria-hidden="true" className="size-3.5" />
              {warnings}
            </span>
          ) : null}
          {kind !== null && kind !== "worker" ? (
            <KindBadge kind={kind} className="ml-auto" />
          ) : (
            <span className="sr-only">{roleName ?? `Unknown role: ${agent.role}`}</span>
          )}
        </div>
        {/* The model chip is never ellipsised: it has a row of its own and wraps if a name is very long. */}
        <span
          data-testid="model-chip"
          data-open-weight={openWeight ? "true" : undefined}
          title={model === null ? "No model chosen" : openWeight ? `${model} (open weights)` : model}
          className={cn(
            "w-fit max-w-full rounded-full border px-1.5 py-px font-mono text-[10.5px] leading-tight break-all text-muted-foreground",
            openWeight ? "border-dashed border-border-strong" : "border-border-strong",
          )}
        >
          {model ?? "no model"}
        </span>
      </div>
      {(kind === "verifier" || agent.checks.length > 0) && <Handle id="check-out" type="source" position={Position.Right} className="!size-2.5 !bg-link" />}
      <Handle type="source" position={Position.Bottom} className="!size-2.5 !bg-muted-foreground" />
    </div>
  );
});
