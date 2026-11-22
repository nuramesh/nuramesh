import { useApi } from "@/api/context";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { useRefetchSafety } from "./use-org-safety";

/** Halt the whole organization (spec/e19/README.md section 12). A 409 `already_halted` counts as done. */
export function HaltDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (open: boolean) => void; onDone: (message: string | undefined) => void }) {
  const api = useApi();
  const refetch = useRefetchSafety();
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Halt the whole organization?"
      description={<p>Every open run is asked to stop and no new task or model call starts until a person resumes. Steps already running finish and are charged.</p>}
      confirmLabel="Halt organization"
      pendingLabel="Halting..."
      danger
      errorPrefix="Could not halt"
      onConfirm={async () => {
        try {
          return await orAlreadyDone(() => api.org.halt(), { already_halted: undefined }, "Organization halted");
        } finally {
          await refetch();
        }
      }}
      onDone={onDone}
    />
  );
}
