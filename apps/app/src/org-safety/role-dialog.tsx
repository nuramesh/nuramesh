import { useApi } from "@/api/context";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { useRefetchSafety } from "./use-org-safety";

/** Pause or resume one role (kill-switch scope: a role). A 409 (`already_paused`, `not_paused`) counts as done. */
export function RoleDialog({
  role,
  mode,
  onOpenChange,
  onDone,
}: {
  /** The role the dialog is about; `null` while closed. */
  role: string | null;
  mode: "pause" | "resume";
  onOpenChange: (open: boolean) => void;
  onDone: (message: string | undefined) => void;
}) {
  const api = useApi();
  const refetch = useRefetchSafety();
  // Keep the last role while the dialog closes, so the title does not blank out mid-animation.
  const name = role ?? "";
  const pause = mode === "pause";
  return (
    <ConfirmDialog
      open={role !== null}
      onOpenChange={onOpenChange}
      title={`${pause ? "Pause" : "Resume"} role ${name}?`}
      description={
        pause ? (
          <p>This role's running tasks are asked to stop and none of its tasks or model calls start until a person resumes it. Other roles keep working. Steps already running finish and are charged.</p>
        ) : (
          <p>The role can start tasks and model calls again. Cancelled tasks are not restarted.</p>
        )
      }
      confirmLabel={pause ? "Pause role" : "Resume role"}
      pendingLabel={pause ? "Pausing..." : "Resuming..."}
      danger={pause}
      errorPrefix={pause ? "Could not pause" : "Could not resume"}
      onConfirm={async () => {
        try {
          return pause
            ? await orAlreadyDone(() => api.org.pauseRole(name), { already_paused: undefined }, `Role ${name} paused`)
            : await orAlreadyDone(() => api.org.resumeRole(name), { not_paused: undefined }, `Role ${name} resumed`);
        } finally {
          await refetch();
        }
      }}
      onDone={onDone}
    />
  );
}
