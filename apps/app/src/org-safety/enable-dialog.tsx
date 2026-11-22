import { useApi } from "@/api/context";
import type { BreakerRow } from "@/api/types";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { useRefetchSafety } from "./use-org-safety";

/** Switch a disabled breaker back on; its count starts afresh at that moment. */
export function EnableDialog({
  rule,
  onOpenChange,
  onDone,
}: {
  /** The rule to enable; `null` while closed. */
  rule: BreakerRow | null;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string | undefined) => void;
}) {
  const api = useApi();
  const refetch = useRefetchSafety();
  const name = rule?.name ?? "";
  return (
    <ConfirmDialog
      open={rule !== null}
      onOpenChange={onOpenChange}
      title={`Enable breaker ${name}?`}
      description={<p>The rule is checked again and its count starts afresh from now.</p>}
      confirmLabel="Enable breaker"
      pendingLabel="Enabling..."
      errorPrefix="Could not enable"
      onConfirm={async () => {
        try {
          return await orAlreadyDone(() => api.breakers.enable(name), { not_disabled: `Breaker ${name} was already enabled.` }, `Breaker ${name} was enabled.`);
        } finally {
          await refetch();
        }
      }}
      onDone={onDone}
    />
  );
}
