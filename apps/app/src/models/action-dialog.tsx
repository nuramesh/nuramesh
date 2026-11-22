import { useMutation } from "@tanstack/react-query";
import { type ReactNode, useId, useState } from "react";

import { REASON_MAX, REASON_MIN, reasonProblem } from "@/api/model-rules";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

import { describeWrite } from "./problem-text";

export interface ActionDialogProps<R> {
  title: string;
  description?: ReactNode;
  /** The button that sends the request ("Approve", "Pause"...). */
  submit: string;
  destructive?: boolean;
  /** Fields above the reason; they own their own state and tell the dialog when they are valid. */
  children?: ReactNode;
  /** False until the fields are valid. */
  fieldsValid?: boolean;
  /** Shown above the reason in a warning callout (what this write will pause, and why). */
  warning?: ReactNode;
  /** Most writes need a reason; a connection write needs one only to repoint. */
  reason?: "required" | "optional";
  reasonLabel?: string;
  /** Turns a refusal into lines; defaults to `describeWrite`. */
  describe?: (error: unknown) => string[];
  /** The bold first line of a refusal; defaults to "Nothing was changed." (a write in two steps says what was kept). */
  failureTitle?: (error: unknown) => string;
  run: (reason: string) => Promise<R>;
  onDone: (result: R) => void;
  onClose: () => void;
}

/**
 * One dialog for every model write: its fields, a reason (3 to 500 characters, kept in the content
 * store with only a hash in the ledger), and the server's own refusals shown in place. The dialog
 * stays open on a refusal and nothing is shown as done until the server says so.
 */
export function ActionDialog<R>({ title, description, submit, destructive, children, fieldsValid = true, warning, reason: mode = "required", reasonLabel = "Reason", describe = describeWrite, failureTitle, run, onDone, onClose }: ActionDialogProps<R>) {
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const id = useId();
  const problem = mode === "optional" && reason.trim() === "" ? null : reasonProblem(reason);
  const send = useMutation({ mutationFn: () => run(reason.trim()), onSuccess: onDone });
  const blocked = problem !== null || !fieldsValid;
  const lines = send.error === null ? [] : describe(send.error);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !send.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto" aria-describedby={`${id}-desc`}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription id={`${id}-desc`} asChild>
            <div>{description}</div>
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3.5"
          onSubmit={(e) => {
            e.preventDefault();
            setTouched(true);
            if (!blocked && !send.isPending) send.mutate();
          }}
        >
          {children}
          {warning !== undefined && <div className="callout-warning px-3 py-2.5 text-[13px]">{warning}</div>}
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${id}-reason`} className="text-[13px] font-semibold">
              {reasonLabel} <span className="font-normal text-foreground/75">({mode === "optional" ? "optional here; " : ""}{String(REASON_MIN)} to {String(REASON_MAX)} characters)</span>
            </label>
            <Textarea
              id={`${id}-reason`}
              rows={3}
              value={reason}
              aria-invalid={touched && problem !== null ? true : undefined}
              aria-describedby={`${id}-reason-help ${id}-reason-error`}
              onChange={(e) => {
                setReason(e.target.value);
                if (e.target.value !== "") setTouched(true);
                if (!send.isPending) send.reset();
              }}
            />
            <p id={`${id}-reason-help`} className="text-xs text-muted-foreground">
              The reason is stored apart from the ledger, which keeps only a hash of it, and can be erased without breaking the record.
            </p>
            <p id={`${id}-reason-error`} className="min-h-4 text-xs text-destructive">
              {touched && problem !== null ? problem : ""}
            </p>
          </div>
          {lines.length > 0 && (
            <div role="alert" className="callout-danger flex flex-col gap-1 px-3 py-2.5 text-[13px]">
              <strong>{failureTitle === undefined || send.error === null ? "Nothing was changed." : failureTitle(send.error)}</strong>
              {lines.map((line) => (
                <span key={line}>{line}</span>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" className="min-h-11" onClick={onClose} disabled={send.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant={destructive === true ? "destructive" : "default"} className="min-h-11" disabled={blocked || send.isPending}>
              {send.isPending ? "Sending…" : submit}
            </Button>
          </DialogFooter>
          {blocked && touched && <p className="text-xs text-muted-foreground">Fill the required fields to continue.</p>}
        </form>
      </DialogContent>
    </Dialog>
  );
}
