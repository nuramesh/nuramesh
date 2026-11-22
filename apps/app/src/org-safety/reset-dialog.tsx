import { useApi } from "@/api/context";
import type { BreakerRow, OrgStatus } from "@/api/types";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { useRefetchSafety } from "./use-org-safety";

/** Reset a tripped breaker. A reset never resumes: the dialog says what stays halted or paused. */
export function ResetDialog({
  rule,
  halt,
  onOpenChange,
  onDone,
}: {
  /** The rule to reset; `null` while closed. */
  rule: BreakerRow | null;
  halt: OrgStatus | null;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string | undefined) => void;
}) {
  const api = useApi();
  const refetch = useRefetchSafety();
  const name = rule?.name ?? "";
  let stays = "";
  if (rule?.action === "pause_role") {
    const paused = halt?.roles.some((r) => r.paused && (rule.role === null || r.role === rule.role)) === true;
    if (paused) stays = " The role stays paused until you resume it.";
  } else if (halt?.halted === true) {
    stays = " The organization stays halted until you resume it.";
  }
  return (
    <ConfirmDialog
      open={rule !== null}
      onOpenChange={onOpenChange}
      title={`Reset breaker ${name}?`}
      description={<p>{`The count starts afresh from now.${stays}`}</p>}
      confirmLabel="Reset breaker"
      pendingLabel="Resetting..."
      errorPrefix="Could not reset"
      onConfirm={async () => {
        try {
          return await orAlreadyDone(() => api.breakers.reset(name), { not_tripped: `Breaker ${name} was already reset.` }, `Breaker ${name} was reset.`);
        } finally {
          await refetch();
        }
      }}
      onDone={onDone}
    />
  );
}
