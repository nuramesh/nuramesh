import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import type { ProposalItem } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useCallerRole } from "@/agents/queries";
import { Chip } from "@/components/ui/chip";
import { useOrgStatus } from "@/org-safety/use-org-safety";

import { GrantDialog } from "./grant-dialog";
import { PROPOSABLE, proposalTarget, reasonWords, statusWords } from "./proposal-words";
import { soloLines } from "./solo-line";

/**
 * Autonomy (SA-18 item 2): what each role has earned, with the solo line, and the grant dialog.
 * The role ids come from the organization status (the ids the proposals route knows), and Grant
 * is offered only to an owner (the grant route is the owner's) for a `proposable` cell.
 */
export function AutonomyPage() {
  const api = useApi();
  const org = useOrgStatus();
  const isOwner = useCallerRole().data === "owner";
  const roles = (org.data?.roles ?? []).filter((r) => r.inConfig).map((r) => r.role);
  const [picked, setPicked] = useState<string | null>(null);
  const role = picked ?? roles[0];
  const list = useQuery({
    queryKey: ["autonomy", "proposals", role],
    queryFn: () => api.autonomy.proposals(role ?? ""),
    enabled: role !== undefined,
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 2,
  });
  const [open, setOpen] = useState<ProposalItem | null>(null);
  const items = list.data?.items ?? [];

  return (
    <div className="flex flex-col">
      <title>Autonomy · NuraMesh</title>
      <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 border-b bg-panel px-6 py-4">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-[21px] font-semibold">Autonomy</h1>
          <p className="text-[13px] text-muted-foreground">
            Agents earn the right to skip a question by getting approved. Granting it stays your decision.
          </p>
        </div>
        <span className="flex-1" />
        {roles.length > 0 && (
          <label className="flex items-center gap-2 text-[13px]">
            Role
            <select
              className="min-h-11 rounded-md border bg-panel px-2"
              value={role}
              onChange={(e) => {
                setPicked(e.target.value);
              }}
            >
              {roles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
        )}
      </header>
      {(org.isError && org.data === undefined) || list.isError ? (
        <p role="alert" className="p-6 text-sm">
          {list.error instanceof NotAvailableError
            ? "This data source can't list autonomy proposals yet."
            : `Couldn't load the proposals. ${describeProblem(list.error ?? org.error)}`}{" "}
          <button
            type="button"
            className="underline"
            onClick={() => {
              void org.refetch();
              void list.refetch();
            }}
          >
            Try again
          </button>
        </p>
      ) : org.isPending || role === undefined || list.data === undefined ? (
        role === undefined && org.data !== undefined ? (
          <p className="p-6 text-sm text-muted-foreground">This organization has no roles yet.</p>
        ) : (
          <p role="status" className="p-6 text-sm text-muted-foreground">
            Loading proposals…
          </p>
        )
      ) : items.length === 0 ? (
        <p className="p-6 text-sm text-muted-foreground">No agent has earned autonomy yet for this role. Proposals appear after enough approved asks.</p>
      ) : (
        <ul aria-label="Proposals" className="m-0 flex list-none flex-col p-0">
          {items.map((it) => (
            <li key={`${it.subject.kind}:${it.subject.id}:${String(it.ruleKey)}`} className="flex flex-col gap-2 border-b px-6 py-4">
              <div className="flex flex-wrap items-center gap-2.5">
                <span className="text-sm font-semibold">{proposalTarget(it)}</span>
                <Chip tone={it.status === PROPOSABLE ? "success" : "warning"}>{statusWords(it.status)}</Chip>
                <span className="flex-1" />
                {it.status === PROPOSABLE && !isOwner && <span className="text-[13px] text-muted-foreground">Only an owner can grant autonomy.</span>}
                {it.status === PROPOSABLE && isOwner && (
                  <Button
                    type="button"
                    className="min-h-11"
                    onClick={() => {
                      setOpen(it);
                    }}
                  >
                    Grant autonomy
                  </Button>
                )}
              </div>
              {/* a cell that counts nothing (never grantable, no such rule) has no line, as in the CLI */}
              {(it.approvedAsks === null ? [] : soloLines(it.approvedAsks, it.solo.approvedAsksSolo, it.solo.soloShare)).map((line) => (
                <p key={line} className={line.startsWith("approved asks") ? "font-mono text-[13px]" : "text-[13px] font-medium text-warm"}>
                  {line}
                </p>
              ))}
              <ul className="m-0 list-disc pl-5 text-[13px] text-muted-foreground">
                {it.reasons.map((r) => (
                  <li key={r}>{reasonWords(r)}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
      {open !== null && list.data !== undefined && (
        <GrantDialog
          open
          onOpenChange={(next) => {
            if (!next) setOpen(null);
          }}
          item={open}
          asOf={list.data.asOf}
          ledgerPosition={list.data.ledgerPosition}
          onDone={() => {
            setOpen(null);
          }}
        />
      )}
    </div>
  );
}
