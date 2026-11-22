import { useId, useState } from "react";

import { useApi } from "@/api/context";
import { DISABLE_REASONS, type BreakerRow, type DisableReason } from "@/api/types";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

import { orAlreadyDone } from "./already-done";
import { ConfirmDialog } from "./confirm-dialog";
import { REASON_LABELS } from "./format";
import { useRefetchSafety } from "./use-org-safety";

/** The reason select lives in its own component so every opening starts with nothing chosen. */
function ReasonField({ value, onChange }: { value: DisableReason | ""; onChange: (value: DisableReason | "") => void }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>Reason</Label>
      <NativeSelect
        id={id}
        className="min-h-11"
        value={value}
        onChange={(event) => {
          onChange(event.target.value as DisableReason | "");
        }}
      >
        <option value="">Choose a reason</option>
        {DISABLE_REASONS.map((code) => (
          <option key={code} value={code}>
            {REASON_LABELS[code]}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

/** Switch a breaker off, with one of four reason codes (spec 6.1). Nothing is stopped or resumed by it. */
export function DisableDialog({
  rule,
  onOpenChange,
  onDone,
}: {
  /** The rule to disable; `null` while closed. */
  rule: BreakerRow | null;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string | undefined) => void;
}) {
  const api = useApi();
  const refetch = useRefetchSafety();
  const [reason, setReason] = useState<DisableReason | "">("");
  const name = rule?.name ?? "";
  return (
    <ConfirmDialog
      open={rule !== null}
      onOpenChange={(open) => {
        if (!open) setReason("");
        onOpenChange(open);
      }}
      title={`Disable breaker ${name}?`}
      description={<p>This rule stops being checked and cannot trip until a person enables it. Nothing is stopped or resumed by this.</p>}
      confirmLabel="Disable breaker"
      pendingLabel="Disabling..."
      danger
      errorPrefix="Could not disable"
      confirmDisabled={reason === ""}
      onConfirm={async () => {
        if (reason === "") throw new Error("Choose a reason");
        try {
          return await orAlreadyDone(
            () => api.breakers.disable(name, reason),
            { already_disabled: `Breaker ${name} was already disabled.`, breaker_tripped: `Breaker ${name} is tripped. Reset it first.` },
            `Breaker ${name} was disabled.`,
          );
        } finally {
          await refetch();
        }
      }}
      onDone={(message) => {
        setReason("");
        onDone(message);
      }}
    >
      <ReasonField value={reason} onChange={setReason} />
    </ConfirmDialog>
  );
}
