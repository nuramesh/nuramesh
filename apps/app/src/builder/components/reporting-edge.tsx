import { BaseEdge, EdgeLabelRenderer, type EdgeProps, getSmoothStepPath } from "@xyflow/react";
import { memo } from "react";

import type { ReportingEdge } from "../flow";

/** A reporting line; when the report's work must pass gates, a diamond with their count sits on the line. */
export const ReportingEdgeView = memo(function ReportingEdgeView(props: EdgeProps<ReportingEdge>) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, selected, data } = props;
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const gates = data?.gates ?? 0;
  return (
    <>
      <BaseEdge id={id} path={path} className={selected ? "!stroke-link" : undefined} interactionWidth={20} />
      {gates > 0 && (
        <EdgeLabelRenderer>
          <div
            data-testid="gate-diamond"
            title={`${String(gates)} gate${gates === 1 ? "" : "s"} before this work counts as done`}
            style={{ transform: `translate(-50%, -50%) translate(${String(labelX)}px, ${String(labelY)}px)` }}
            className="pointer-events-none absolute flex items-center gap-1 bg-canvas px-1 font-mono text-[11px] text-link"
          >
            <span aria-hidden="true" className="size-3 rotate-45 rounded-[3px] border-2 border-link bg-canvas" />
            <span>
              {gates}
              <span className="sr-only"> gate{gates === 1 ? "" : "s"}</span>
            </span>
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
});
