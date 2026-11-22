import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import type { ProposalItem } from "@/api/types";
import { reasonProblem } from "@/approvals/rules";
import { ConfirmDialog } from "@/org-safety/confirm-dialog";
import { Textarea } from "@/components/ui/textarea";

import { proposalTarget } from "./proposal-words";
import { soloLines } from "./solo-line";

export interface GrantDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: ProposalItem;
  /** What the person read: the proposals' `as_of` and `ledger_position`; the server refuses the grant if the evidence moved. */
  asOf: string;
  ledgerPosition: number;
  onDone?: () => void;
}

/**
 * The grant dialog (SA-18 item 2): before autonomy is granted it prints how many of the approved
 * asks were solo decisions (nobody else reviewed them), then asks for the reason. A grant stays
 * the owner's decision; the server still requires its own evidence and refuses a stale one.
 */
export function GrantDialog({ open, onOpenChange, item, asOf, ledgerPosition, onDone }: GrantDialogProps) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const lines = item.approvedAsks === null ? [] : soloLines(item.approvedAsks, item.solo.approvedAsksSolo, item.solo.soloShare);
  const target = proposalTarget(item);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Grant autonomy to ${target}`}
      description={
        <>
          <p>The agent will act without asking for this kind of action until the grant expires or you revoke it.</p>
          {lines.map((line) => (
            <p key={line} className={line.startsWith("approved asks") ? "font-mono text-[13px]" : "font-medium text-warm"}>
              {line}
            </p>
          ))}
        </>
      }
      confirmLabel="Grant autonomy"
      pendingLabel="Granting..."
      errorPrefix="Could not grant"
      confirmDisabled={reasonProblem(reason) !== null}
      onConfirm={async () => {
        try {
          await api.autonomy.grant({
            subject: item.subject,
            ruleKey: item.ruleKey,
            asOf,
            ledgerPosition,
            evidenceHash: item.evidenceHash,
            reason,
          });
        } catch (error) {
          throw new Error(describeProblem(error));
        }
        await queryClient.invalidateQueries({ queryKey: ["autonomy"] });
        return undefined;
      }}
      {...(onDone === undefined ? {} : { onDone: () => { onDone(); } })}
    >
      <label htmlFor="grant-reason" className="text-[13.5px] font-semibold">
        Your reason <span className="font-normal text-muted-foreground">(required)</span>
      </label>
      <Textarea
        id="grant-reason"
        rows={3}
        value={reason}
        onChange={(e) => {
          setReason(e.target.value);
        }}
      />
    </ConfirmDialog>
  );
}
