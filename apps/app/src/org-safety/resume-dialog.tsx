import { useApi } from "@/api/context";
import type { OrgStatus } from "@/api/types";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { capped } from "./format";
import { useBreakers, useRefetchSafety } from "./use-org-safety";

/**
 * Resume the organization. A resume lifts only the halt: the dialog lists what stays (tripped
 * breakers, paused roles), at most three of each, then "and <n> more".
 */
export function ResumeDialog({
  open,
  onOpenChange,
  status,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  status: OrgStatus;
  onDone: (message: string | undefined) => void;
}) {
  const api = useApi();
  const refetch = useRefetchSafety();
  const breakers = useBreakers(open);
  const tripped = (breakers.data?.breakers ?? []).filter((b) => b.state === "tripped").map((b) => `Breaker ${b.name} is still tripped.`);
  const paused = status.roles.filter((r) => r.paused).map((r) => `Role ${r.role} is still paused.`);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Resume the organization?"
      description={
        <>
          <p>New work can start again. Stopped runs are not restarted, paused roles stay paused and tripped breakers stay tripped until a person resumes or resets them.</p>
          {[...capped(tripped), ...capped(paused)].map((line) => (
            <p key={line}>{line}</p>
          ))}
        </>
      }
      confirmLabel="Resume organization"
      pendingLabel="Resuming..."
      errorPrefix="Could not resume"
      onConfirm={async () => {
        try {
          return await orAlreadyDone(() => api.org.resume(), { already_running: undefined }, "Organization resumed");
        } finally {
          await refetch();
        }
      }}
      onDone={onDone}
    />
  );
}
