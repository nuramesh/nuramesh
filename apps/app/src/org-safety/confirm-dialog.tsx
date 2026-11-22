import { type ReactNode, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

import { problemText } from "./use-org-safety";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** The body text (and any extra lines): it is the dialog's accessible description. */
  description: ReactNode;
  confirmLabel: string;
  /** The confirm button's text while the request is out. */
  pendingLabel: string;
  /** Danger styling for the confirm button (halting or pausing); resuming and enabling are not. */
  danger?: boolean;
  /** Start of the error line: `Could not halt` -> `Could not halt: <problem title>`. */
  errorPrefix: string;
  /** Turns the confirm button off (a required field is empty). */
  confirmDisabled?: boolean;
  /** Extra form fields between the text and the buttons. */
  children?: ReactNode;
  /**
   * Does the action. Rejects with the problem to show in the dialog. Resolves with a message to
   * leave behind after the dialog closes (a 409 that means "already done"), or nothing.
   */
  onConfirm: () => Promise<string | undefined>;
  /** Called after the dialog closed because the action finished (or was already done). */
  onDone?: (message: string | undefined) => void;
}

/**
 * The one confirmation dialog of the kill switch, role pause and breakers (spec/e19/README.md
 * section 12): focus starts on Cancel, Escape cancels, the confirm button shows its pending text
 * and both buttons are off while the request is out, and a failure stays in the dialog as a
 * `role="alert"` line. Closed means unmounted, so every opening starts clean.
 */
export function ConfirmDialog(props: ConfirmDialogProps) {
  const [sending, setSending] = useState(false);
  return (
    <Dialog
      open={props.open}
      onOpenChange={(next) => {
        if (!sending) props.onOpenChange(next); // nothing closes a request that is out
      }}
    >
      {props.open && <Body {...props} sending={sending} setSending={setSending} />}
    </Dialog>
  );
}

function Body({ sending, setSending, ...props }: ConfirmDialogProps & { sending: boolean; setSending: (sending: boolean) => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  // Radix returns focus only to a `Dialog.Trigger`; these dialogs open from many buttons, so the
  // element that had focus when the dialog opened is remembered here and focused again on close.
  const opener = useRef<Element | null>(document.activeElement);
  const [error, setError] = useState<string | null>(null);

  const confirm = async (): Promise<void> => {
    if (sending) return;
    setSending(true);
    setError(null);
    try {
      const message = await props.onConfirm();
      setSending(false);
      props.onOpenChange(false);
      props.onDone?.(message);
    } catch (problem) {
      setSending(false);
      setError(`${props.errorPrefix}: ${problemText(problem)}`);
    }
  };

  return (
    <DialogContent
      showCloseButton={false}
      onOpenAutoFocus={(event) => {
        event.preventDefault();
        cancel.current?.focus();
      }}
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        const element = opener.current;
        if (element instanceof HTMLElement && element.isConnected) element.focus();
      }}
    >
      <DialogHeader>
        <DialogTitle>{props.title}</DialogTitle>
        <DialogDescription asChild>
          <div className="flex flex-col gap-1.5">{props.description}</div>
        </DialogDescription>
      </DialogHeader>
      {props.children}
      {error !== null && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button
          ref={cancel}
          variant="outline"
          className="min-h-11"
          disabled={sending}
          onClick={() => {
            props.onOpenChange(false);
          }}
        >
          Cancel
        </Button>
        <Button
          variant={props.danger === true ? "destructive" : "default"}
          className="min-h-11"
          disabled={sending || props.confirmDisabled === true}
          onClick={() => {
            void confirm();
          }}
        >
          {sending ? props.pendingLabel : props.confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}
