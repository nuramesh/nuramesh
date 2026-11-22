import { Check, X } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { NotAvailableError, NotFoundError } from "@/api/client";
import { formatMoneyExact } from "@/api/money";
import { describeProblem } from "@/api/problems";
import type { AuditApproval, GateOutcome, TaskAudit } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Chip, FilterPill } from "@/components/ui/chip";
import { ChainChip } from "@/ledger/anchor";
import { summarizeChain } from "@/ledger/anchor-words";
import { entryHref, entryLabel } from "@/ledger/format";
import { atLeastWords } from "@/models/words";
import { NotFoundPage } from "@/routes/not-found";

import { formatClock } from "./format";
import {
  AUDIT_FILTERS,
  type AuditFilter,
  attemptSpendWords,
  costWords,
  decisionWords,
  FINAL_TASK_STATUSES,
  hashWords,
  inFilter,
  reworkWords,
  summaryCalls,
} from "./task-audit-words";
import { useTaskAudit } from "./use-task-audit";

const Panel = ({ id, title, children }: { id: string; title: string; children: React.ReactNode }) => (
  <section aria-labelledby={id} className="rounded-xl border bg-panel border-card-border shadow-card">
    <h2 id={id} className="border-b px-4 py-3 text-sm font-semibold">
      {title}
    </h2>
    <div className="flex flex-col gap-3 px-4 py-3">{children}</div>
  </section>
);

const taskWord = (status: string): string => status.replace(/_/g, " ");
const statusTone = (status: string) =>
  status === "done" ? "success" : status === "needs_human" || status === "blocked" ? "warning" : status === "cancelled" || status === "failed" ? "danger" : "neutral";
const Empty = ({ children }: { children: string }) => <p className="m-0 text-[13px] text-muted-foreground">{children}</p>;

function Gate({ g }: { g: GateOutcome }) {
  return (
    <li className="flex flex-col gap-1 text-[13px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{g.gate}</span>
        <span className="font-mono text-[11.5px] text-small">
          {g.kind}
          {g.required ? ", required" : ", optional"} · checked by {g.checkedBy}
        </span>
        <Chip tone={g.outcome === "passed" ? "success" : "danger"} className="ml-auto">
          {g.outcome === "passed" ? <Check aria-hidden="true" className="size-3.5" /> : <X aria-hidden="true" className="size-3.5" />}
          {g.outcome}
        </Chip>
        <Link to={entryHref(g.ledgerEntry)} className="inline-flex min-h-11 items-center font-mono text-xs text-link underline">
          #{entryLabel(g.ledgerEntry)}
        </Link>
      </div>
      {g.verdicts.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-0.5 p-0 pl-1">
          {g.verdicts.map((v) => (
            <li key={v.criterion} className="flex gap-2 text-muted-foreground">
              <span aria-hidden="true" className={v.met ? "text-link" : "text-danger"}>
                {v.met ? "✓" : "✗"}
              </span>
              <span>
                <span className="sr-only">{v.met ? "Met: " : "Not met: "}</span>
                {v.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Summary({ audit, runId }: { audit: TaskAudit; runId: string }) {
  const t = audit.task;
  const chain = summarizeChain({ verified: audit.chain.ok, gaps: 0, entries: audit.chain.headSeq, anchor: null, checkedAt: "" });
  return (
    <Panel id="audit-summary" title="Summary">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Chip tone={statusTone(t.status)}>{taskWord(t.status)}</Chip>
        <span className="font-mono text-xs text-small">{t.assignee ?? "not assigned"}</span>
        <span className="font-mono text-xs text-small">{`attempt ${String(t.attempt)} of ${String(t.maxAttempts)}`}</span>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[22px] font-semibold">{audit.spend.atLeast === null ? formatMoneyExact(audit.spend.total) : atLeastWords(audit.spend.atLeast)}</span>
        <span className="text-[13px] text-muted-foreground">{summaryCalls(audit.spend)}</span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <ChainChip summary={chain} />
        <span className="font-mono text-small">{`head: entry ${String(audit.chain.headSeq)}`}</span>
        <Link to={`/runs/${encodeURIComponent(runId)}`} className="inline-flex min-h-11 items-center text-link underline">
          Open run
        </Link>
      </div>
    </Panel>
  );
}

function Attempts({ audit }: { audit: TaskAudit }) {
  const t = audit.task;
  const withRounds = new Set(audit.attempts.map((a) => a.attempt));
  const final = FINAL_TASK_STATUSES.has(t.status);
  const spendLines = audit.spendByAttempt.filter((s) => s.calls > 0 || (s.attempt === t.attempt && !final && !withRounds.has(s.attempt) && s.attempt > 0));
  return (
    <Panel id="audit-attempts" title="Attempts">
      {audit.attempts.length === 0 && spendLines.length === 0 && <Empty>Nothing yet</Empty>}
      {spendLines.length > 0 && (
        <ul aria-label="Spend by attempt" className="m-0 flex list-none flex-col gap-0.5 p-0 text-[13px]">
          {spendLines.map((s) => (
            <li key={s.attempt}>{`${s.attempt === 0 ? "Before work started" : `Attempt ${String(s.attempt)}`}: ${attemptSpendWords(s, s.attempt === t.attempt && s.attempt > 0 && !final && !withRounds.has(s.attempt))}`}</li>
          ))}
        </ul>
      )}
      {[...withRounds].sort((a, b) => a - b).map((attempt) => (
        <div key={attempt} className="flex flex-col gap-2 rounded-lg border bg-panel-2 px-3 py-2">
          <h3 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">{`Attempt ${String(attempt)}`}</h3>
          {audit.attempts
            .filter((a) => a.attempt === attempt)
            .map((a) => (
              <div key={a.submission} className="flex flex-col gap-1.5">
                {audit.attempts.filter((x) => x.attempt === attempt).length > 1 && <p className="m-0 text-[12px] text-small">{`Submission ${String(a.submission)}`}</p>}
                <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                  {a.gates.map((g) => (
                    <Gate key={`${g.gate}:${g.ledgerEntry}`} g={g} />
                  ))}
                </ul>
                {a.movedTo !== null && (
                  <p className="m-0 text-[13px] text-muted-foreground">
                    {`Then moved to ${taskWord(a.movedTo)}`}
                    {a.reason !== null ? `: ${a.reason}` : a.reasonHash !== null ? `: A person's reason (hash ${hashWords(a.reasonHash)})` : ""}
                  </p>
                )}
              </div>
            ))}
        </div>
      ))}
    </Panel>
  );
}

function Rework({ audit }: { audit: TaskAudit }) {
  return (
    <Panel id="audit-rework" title="Rework">
      {audit.rework.length === 0 ? (
        <Empty>Not sent back.</Empty>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px]">
          {audit.rework.map((r) => (
            <li key={r.seq} className="flex flex-col gap-0.5">
              <span>
                <time dateTime={r.at} className="font-mono text-xs text-small">
                  {formatClock(r.at)}
                </time>{" "}
                {reworkWords(r)}
              </span>
              {r.reasonHash !== null && <span className="text-muted-foreground">{`reason kept as hash ${hashWords(r.reasonHash)}`}</span>}
              {r.feedbackHash !== null && <span className="text-muted-foreground">{`feedback kept as hash ${hashWords(r.feedbackHash)}`}</span>}
              {r.attemptAfter !== null && r.maxAttempts !== null && <span className="text-muted-foreground">{`attempt ${String(r.attemptAfter)} of ${String(r.maxAttempts)}`}</span>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ApprovalLine({ a }: { a: AuditApproval }) {
  return (
    <li data-testid="approval" className="flex flex-col gap-0.5 text-[13px]">
      <span>
        <span className="font-mono">{a.id}</span> {a.status}
        {a.decidedBy !== null ? ` by ${a.decidedBy}` : ""} · asked by {a.agent} because {a.askedBecause === "" ? "policy asks a person" : a.askedBecause}
      </span>
      {a.reasonHash !== null ? <span className="text-muted-foreground">{`Reason kept in the content store (hash ${hashWords(a.reasonHash)})`}</span> : a.decidedBy !== null && <span className="text-muted-foreground">No reason kept</span>}
    </li>
  );
}

function Decisions({ audit }: { audit: TaskAudit }) {
  const none = audit.approvals.length === 0 && audit.decisions.length === 0;
  return (
    <Panel id="audit-decisions" title="Approvals and decisions">
      {none ? (
        <Empty>Nothing yet.</Empty>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {audit.approvals.map((a) => (
            <ApprovalLine key={a.id} a={a} />
          ))}
          {audit.decisions.map((d) => (
            <li key={d.seq} data-testid="decision" className="flex flex-col gap-0.5 text-[13px]">
              <span>{decisionWords(d)}</span>
              {typeof d.fields.reason_hash === "string" && <span className="text-muted-foreground">{`reason hash ${hashWords(d.fields.reason_hash)}`}</span>}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Entries({ audit }: { audit: TaskAudit }) {
  const [filter, setFilter] = useState<AuditFilter>("all");
  const reworkSeqs = new Set(audit.rework.map((r) => r.seq));
  const shown = audit.entries.filter((e) => inFilter(e, filter, reworkSeqs));
  return (
    <Panel id="audit-entries" title="Ledger entries">
      {audit.truncated && (
        <p className="m-0 text-[13px] text-warm">
          {`Showing the first ${String(audit.entries.length)} of ${String(audit.totalEntries)} entries; the rest are in the `}
          <Link to={`/ledger?q=${encodeURIComponent(audit.task.id)}`} className="inline-flex min-h-11 items-center text-link underline">
            ledger
          </Link>
        </p>
      )}
      <div role="group" aria-label="Filter the entries" className="flex flex-wrap gap-1.5">
        {AUDIT_FILTERS.map((f) => (
          <FilterPill
            key={f.id}
            pressed={filter === f.id}
            onClick={() => {
              setFilter(f.id);
            }}
            className="text-[12.5px]"
          >
            {f.label}
          </FilterPill>
        ))}
      </div>
      <div role="log" aria-live="polite" aria-relevant="additions" aria-label="Ledger entries of this task, oldest first">
        <ol className="m-0 flex list-none flex-col p-0">
          {shown.map((e) => {
            const cost = costWords(e);
            return (
              <li key={e.n} data-seq={e.label} className="flex flex-col gap-0.5 border-t py-2.5 first:border-t-0">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-mono text-xs">{e.kind}</span>
                  <time dateTime={e.at} className="font-mono text-[11.5px] text-small">
                    {formatClock(e.at)}
                  </time>
                  <span className="font-mono text-[11.5px] text-small">{e.actor}</span>
                  <Link to={entryHref(e.n)} className="ml-auto inline-flex min-h-11 items-center font-mono text-xs text-link underline">
                    #{e.label}
                  </Link>
                </div>
                <span className="text-[13px] text-muted-foreground">{e.summary}</span>
                {cost !== null && <span className="font-mono text-xs">{cost}</span>}
              </li>
            );
          })}
        </ol>
      </div>
    </Panel>
  );
}

const MODE_TEXT = {
  live: "live",
  reconnecting: "connection lost, reconnecting…",
  unreachable: "cannot reach the server (check the address and that the server allows this origin)",
  polling: "updating every 3 s",
} as const;

/** One task's whole story, in order, with evidence (R1.27, PLAN G.3): attempts, gates, spend, rework, approvals and the ledger entries. */
export function TaskAuditPage() {
  const { runId = "", taskId = "" } = useParams();
  return <TaskAuditView key={`${runId}/${taskId}`} runId={runId} taskId={taskId} />;
}

function TaskAuditView({ runId, taskId }: { runId: string; taskId: string }) {
  const { query, stream, live, runEnded } = useTaskAudit(runId, taskId);
  const audit = query.data;

  if (query.isError && audit === undefined) {
    if (query.error instanceof NotFoundError) return <NotFoundPage />;
    const unavailable = query.error instanceof NotAvailableError;
    return (
      <section aria-labelledby="page-title" className="mx-auto flex max-w-2xl flex-col gap-3 p-6 md:p-10">
        <title>Task audit · NuraMesh</title>
        <h1 id="page-title" className="text-2xl font-semibold">
          {unavailable ? "Audit trail not available" : "Couldn't load the audit trail"}
        </h1>
        <p role="alert" className="text-muted-foreground">
          {unavailable ? "This data source cannot give a task's audit trail" : describeProblem(query.error)}
        </p>
        <div className="flex gap-3">
          {!unavailable && (
            <Button
              onClick={() => {
                void query.refetch();
              }}
            >
              Try again
            </Button>
          )}
          <Link to={`/runs/${encodeURIComponent(runId)}`} className="inline-flex min-h-11 items-center text-link underline">
            Open run
          </Link>
        </div>
      </section>
    );
  }
  if (query.isError && query.error instanceof NotFoundError) return <NotFoundPage />;
  if (audit === undefined) {
    return (
      <p role="status" className="p-6 text-muted-foreground">
        Loading the audit trail…
      </p>
    );
  }

  const mode = live ? MODE_TEXT[stream.mode] : runEnded ? "not live (the run has ended)" : "not live (the task is finished)";
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-4 md:p-8">
      <title>{`${audit.task.title} · audit · NuraMesh`}</title>
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold">{audit.task.title}</h1>
        <span role="status" data-testid="audit-mode" className="font-mono text-[11px] text-small">
          {mode}
        </span>
        {query.isError && (
          <p role="alert" className="m-0 text-[13px] text-warm">
            {`Could not refresh; showing the data from ${formatClock(new Date(query.dataUpdatedAt).toISOString())}`}
          </p>
        )}
      </div>
      <Summary audit={audit} runId={runId} />
      <Attempts audit={audit} />
      <Rework audit={audit} />
      <Decisions audit={audit} />
      <Entries audit={audit} />
    </div>
  );
}
