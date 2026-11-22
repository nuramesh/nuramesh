import { Handle, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";

import { cn } from "@/lib/utils";

import type { PersonFlowNode } from "../flow";

/** A human approver: a dashed warm ring (a person, not an agent). Draw from its right handle to an agent to make them an approver for that agent's role. */
export const PersonNode = memo(function PersonNode({ data, selected }: NodeProps<PersonFlowNode>) {
  const { person, roles } = data;
  return (
    <div
      className={cn(
        "flex h-full w-full items-center gap-2.5 rounded-full border border-dashed border-warm bg-card px-3 text-card-foreground",
        selected && "ring-2 ring-ring",
      )}
    >
      <span aria-hidden="true" className="size-7 shrink-0 rounded-full border-[1.5px] border-dashed border-warm" />
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-[13px] font-semibold">{person.label}</span>
        <span className="truncate text-[11px] text-muted-foreground">
          {roles === 0 ? "approves nothing yet" : `approves ${String(roles)} role${roles === 1 ? "" : "s"}`}
        </span>
      </div>
      <Handle id="approve-out" type="source" position={Position.Right} className="!size-2.5 !bg-warm" />
    </div>
  );
});
