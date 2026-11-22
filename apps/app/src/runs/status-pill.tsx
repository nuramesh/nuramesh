import { Chip } from "@/components/ui/chip";
import type { RunStatus } from "@/api/types";

import { STATUS_LABEL, STATUS_TONE } from "./format";

/** The run's status as a word with a dot: the word carries the meaning, the colour only helps. */
export function StatusPill({ status, className }: { status: RunStatus; className?: string }) {
  return (
    <Chip tone={STATUS_TONE[status]} className={className}>
      <span aria-hidden="true" className="size-[7px] rounded-full bg-current" />
      {STATUS_LABEL[status]}
    </Chip>
  );
}
