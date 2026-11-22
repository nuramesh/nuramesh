import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { NotAllowedError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { formatMoney } from "@/api/money";
import { describeProblem } from "@/api/problems";
import type { Approval } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { formatLocalWithRelative } from "@/lib/time";

import { consequenceFor, costLine, escapeInert } from "./sentence";
import {
  ACTION_CHANGED,
  ALREADY_DECIDED,
  budgetLeft,
  isHighRisk,
  NO_PROOF_HELPS,
  NOBODY_ELSE,
  passwordProblem,
  proofsAfter,
  SIGN_IN_AGAIN,
  SIGN_IN_AGAIN_MFA,
  soloRefusal,
  YOU_STARTED,
} from "./solo-words";

export interface SoloApproveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** A pending request whose `solo` terms are set: the caller is the only person who can decide it. */
  approval: Approval;
  /** The reason the person wrote on the page; it is sent with the approve. */
  reason: string;
  /** Called after the dialog closed because the approve was recorded. */
  onApproved: () => void;
  /** Called after the dialog closed without an approve, with what the page should say (the request changed, or was decided meanwhile). */
  onNotice: (text: string) => void;
}

/**
 * What a sole decider reads before approving alone (spec/rbac/solo-approval.md SA-17): the agent, its
 * role, the task, the kind, the whole target as inert text, the amount, the rule's reason, the
 * request's age, the run's spend and the budget left, the fixed line of what the kind does, and
 * "Nobody else will review this.". Approve is never the default action: focus starts on Cancel,
 * Escape cancels and Enter in a field never approves. A high-risk kind needs its kind word typed,
 * and when the server would ask for a fresh proof (SA-7) the proof comes first. This dialog's confirm
 * is the only place the app sends `acknowledge_solo`. Closed means unmounted: every opening starts clean.
 */
export function SoloApproveDialog(props: SoloApproveDialogProps) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      open={props.open}
      onOpenChange={(next) => {
        if (!busy) props.onOpenChange(next); // nothing closes a request that is out
      }}
    >
      {props.open && props.approval.solo != null && <Body {...props} solo={props.approval.solo} busy={busy} setBusy={setBusy} />}
    </Dialog>
  );
}

function Line({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className="grid grid-cols-[9.5rem_minmax(0,1fr)] gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={className ?? "min-w-0 [overflow-wrap:anywhere]"}>{children}</dd>
    </div>
  );
}

type BodyProps = SoloApproveDialogProps & { solo: NonNullable<Approval["solo"]>; busy: boolean; setBusy: (busy: boolean) => void };

function Body({ onOpenChange, onApproved, onNotice, reason, busy, setBusy, ...props }: BodyProps) {
  const api = useApi();
  const queryClient = useQueryClient();
  const cancel = useRef<HTMLButtonElement>(null);
  // Radix returns focus only to a `Dialog.Trigger`; the element that had focus at opening is focused again on close.
  const opener = useRef<Element | null>(document.activeElement);
  const out = useRef(false);
  // What the person reads is what the digest covers: a re-read while the dialog is open never swaps the
  // text under them. If the request changed on the server, the approve is refused (409) and the page reloads it.
  const [approval] = useState(props.approval);
  const [solo] = useState(props.solo);
  const [word, setWord] = useState("");
  const [password, setPassword] = useState("");
  const [proof, setProof] = useState<"none" | "needed" | "done">(solo.freshProofNeeded ? "needed" : "none");
  const [accepted, setAccepted] = useState<readonly string[] | undefined>(solo.proofs);
  const [error, setError] = useState<string | null>(null);

  const action = approval.action ?? { kind: "", target: null, amount: null };
  const highRisk = isHighRisk(action.kind);
  const typed = !highRisk || word.trim() === action.kind;
  const canConfirmPassword = api.auth?.confirmPassword !== undefined;
  // How the missing proof can be given (SA-7): in the dialog (the password), by signing in again, or not at all.
  const how =
    proof !== "needed"
      ? null
      : accepted === undefined || accepted.includes("password")
        ? canConfirmPassword
          ? "password"
          : "sign-in"
        : accepted.includes("sign_in")
          ? "sign-in"
          : accepted.includes("mfa")
            ? "mfa"
            : "none";

  const reread = (): Promise<unknown> =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: keys.approvals }),
      queryClient.invalidateQueries({ queryKey: ["approval", approval.id] }),
      queryClient.invalidateQueries({ queryKey: keys.ledger }),
      queryClient.invalidateQueries({ queryKey: keys.runs }),
    ]);

  const close = (): void => {
    out.current = false;
    setBusy(false);
    onOpenChange(false);
  };

  const confirmPassword = async (): Promise<void> => {
    if (out.current || password === "") return;
    out.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.auth?.confirmPassword?.(password);
      setProof("done");
    } catch (problem) {
      setError(passwordProblem(problem));
    }
    setPassword("");
    out.current = false;
    setBusy(false);
  };

  const approve = async (): Promise<void> => {
    if (out.current || proof === "needed" || !typed) return;
    out.current = true; // one click, one approve
    setBusy(true);
    setError(null);
    try {
      await api.approvals.decide(approval.id, { decision: "approve", reason, solo: { actionDigest: solo.actionDigest } });
    } catch (problem) {
      if (problem instanceof NotAllowedError || (problem instanceof ProblemError && problem.status === 409 && problem.title === "action_changed")) {
        close();
        onNotice(problem instanceof NotAllowedError ? ALREADY_DECIDED : ACTION_CHANGED);
      } else {
        const left = proofsAfter(problem);
        if (left !== null) {
          setProof("needed");
          setAccepted(left);
        }
        setError(soloRefusal(problem) ?? `Couldn't save the decision: ${describeProblem(problem)}`);
        out.current = false;
        setBusy(false);
      }
      void reread();
      return;
    }
    close();
    onApproved();
    void reread();
  };

  return (
    <DialogContent
      showCloseButton={false}
      className="max-h-[90dvh] overflow-y-auto"
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
        <DialogTitle>Approve this on your own?</DialogTitle>
        <DialogDescription asChild>
          <div className="flex flex-col gap-1 text-[14.5px] font-semibold text-foreground">
            {approval.startedByYou === true && <p>{YOU_STARTED}</p>}
            <p>{NOBODY_ELSE}</p>
          </div>
        </DialogDescription>
      </DialogHeader>

      <dl className="m-0 flex flex-col gap-1.5 text-[13.5px]">
        <Line label="Agent">{escapeInert(approval.requester)}</Line>
        <Line label="Role">{approval.role == null || approval.role === "" ? "not available" : escapeInert(approval.role)}</Line>
        <Line label="Task">{approval.taskId == null || approval.taskId === "" ? "no task" : escapeInert(approval.taskId)}</Line>
        <Line label="Kind">{action.kind === "" ? "not available" : escapeInert(action.kind)}</Line>
        <Line label="Target" className="min-w-0 font-mono text-[12.5px] whitespace-pre-wrap [overflow-wrap:anywhere]">
          {action.target === null || action.target === "" ? "(no target given)" : escapeInert(action.target)}
        </Line>
        <Line label="Amount">{action.amount === null ? "none" : formatMoney(action.amount)}</Line>
        <Line label="Why you were asked">{escapeInert(approval.rule)}</Line>
        <Line label="Asked">{formatLocalWithRelative(approval.requestedAt)}</Line>
        <Line label="Run spend so far">{approval.runSpend === null ? "not available" : formatMoney(approval.runSpend.spent)}</Line>
        <Line label="Budget left">{budgetLeft(approval.runSpend)}</Line>
      </dl>

      <div className="flex flex-col gap-1 text-[13.5px]">
        <p>If you approve: {consequenceFor(action.kind)}.</p>
        <p>
          <span className="font-semibold">Cost: </span>
          {costLine(action, approval.runSpend)}
        </p>
        <p className="text-muted-foreground">Works once, for this action. There is no undo.</p>
      </div>

      {highRisk && (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="solo-kind-word" className="text-[13.5px] font-semibold">
            Type {action.kind} to confirm
          </label>
          <Input
            id="solo-kind-word"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            value={word}
            onChange={(e) => {
              setWord(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.preventDefault(); // Enter never approves
            }}
          />
        </div>
      )}

      {how === "password" && (
        <form
          className="callout-warning flex flex-col gap-2 p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void confirmPassword();
          }}
        >
          <label htmlFor="solo-password" className="text-[13.5px] font-semibold">
            Your password
          </label>
          <p className="text-xs text-muted-foreground">Approving this kind of action alone needs your password again. It counts for 10 minutes, in this browser session only.</p>
          <Input
            id="solo-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
          <Button type="submit" variant="outline" className="min-h-11 self-start" disabled={busy || password === ""}>
            Confirm password
          </Button>
        </form>
      )}
      {how === "mfa" && <p className="callout-warning p-3 text-[13.5px]">{SIGN_IN_AGAIN_MFA}</p>}
      {how === "sign-in" && <p className="callout-warning p-3 text-[13.5px]">{SIGN_IN_AGAIN}</p>}
      {how === "none" && <p className="callout-warning p-3 text-[13.5px]">{NO_PROOF_HELPS}</p>}
      {proof === "done" && (
        <p role="status" className="text-[13.5px]">
          Password confirmed. You can approve for the next 10 minutes.
        </p>
      )}

      {error !== null && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <DialogFooter>
        <Button
          ref={cancel}
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={busy}
          onClick={() => {
            onOpenChange(false);
          }}
        >
          Cancel
        </Button>
        {(how === null || how === "password") && (
          <Button
            type="button"
            className="min-h-11"
            disabled={busy || proof === "needed" || !typed}
            onClick={() => {
              void approve();
            }}
          >
            Approve on my own
          </Button>
        )}
      </DialogFooter>
    </DialogContent>
  );
}
