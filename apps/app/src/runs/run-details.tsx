import { useQuery } from "@tanstack/react-query";
import { Check, X } from "lucide-react";
import { Link } from "react-router";

import { NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import { formatMoney } from "@/api/money";
import { describeProblem } from "@/api/problems";
import type { BudgetAlert, RunDetail, RunTask } from "@/api/types";
import { formatUtc, WINDOW_NAME } from "@/budgets/windows";
import { Chip } from "@/components/ui/chip";
import { ChainChip } from "@/ledger/anchor";
import { summarizeChain } from "@/ledger/anchor-words";
import { entryHref, entryLabel } from "@/ledger/format";
import { atLeastWords, COST_UNKNOWN_WORDS } from "@/models/words";

import { GateDecisionForm } from "./gate-decision-form";
import { openHumanGates } from "./human-gates";

const Panel = ({ id, title, children }: { id: string; title: string; children: React.ReactNode }) => (
  <section aria-labelledby={id} className="rounded-xl border bg-panel border-card-border shadow-card">
    <h2 id={id} className="border-b px-4 py-3 text-sm font-semibold">
      {title}
    </h2>
    {children}
  </section>
);

/** The words for a task status; the engine's own names, spaced. */
const taskWord = (status: string): string => status.replace(/_/g, " ");

function TaskItem({ task, runId }: { task: RunTask; runId: string }) {
  const attempts = [...new Set(task.gates.map((g) => g.attempt))].sort((a, b) => a - b);
  return (
    <li data-testid={`task-${task.id}`} className="flex flex-col gap-2 border-t px-4 py-3 first:border-t-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[14px] font-semibold">{task.title}</span>
        <Chip tone={task.status === "done" ? "success" : task.status === "needs_human" || task.status === "blocked" ? "warning" : task.status === "cancelled" || task.status === "failed" ? "danger" : "neutral"}>{taskWord(task.status)}</Chip>
        <span className="ml-auto font-mono text-xs text-small">
          {task.assignee} · attempt {task.attempt} of {task.maxAttempts} · {formatMoney(task.spend)} in {task.calls} call{task.calls === 1 ? "" : "s"}
        </span>
      </div>
      <Link to={`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(task.id)}`} className="inline-flex min-h-11 w-fit items-center text-[13px] text-link underline">
        Audit trail<span className="sr-only"> for {task.title}</span>
      </Link>
      {typeof task.note === "string" && task.note !== "" && <p className="m-0 text-[12.5px] text-muted-foreground">{task.note}</p>}
      {attempts.map((attempt) => (
        <div key={attempt} className="flex flex-col gap-1.5 rounded-lg border bg-panel-2 px-3 py-2">
          <h3 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">Attempt {attempt}</h3>
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
            {task.gates
              .filter((g) => g.attempt === attempt)
              .map((g) => (
                <li key={`${g.gate}:${g.ledgerEntry}`} className="flex flex-col gap-1 text-[13px]">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{g.gate}</span>
                    <span className="font-mono text-[11.5px] text-small">
                      {g.kind}
                      {g.required ? "" : " (optional)"} · {g.checkedBy}
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
              ))}
          </ul>
        </div>
      ))}
      {attempts.length === 0 && <p className="m-0 text-[12.5px] text-small">No gate has run on this task.</p>}
      {openHumanGates(task).map((gate) => (
        <GateDecisionForm key={gate.id} runId={runId} task={task} gate={gate} />
      ))}
    </li>
  );
}

/** Every task with its attempts, gates and per-criterion verdicts (the ledger keeps verdicts, never the verifier's text). */
export function TaskGates({ tasks, runId }: { tasks: RunTask[]; runId: string }) {
  return (
    <Panel id="tasks-title" title="Tasks and gates">
      {tasks.length === 0 ? (
        <p className="p-4 text-sm text-muted-foreground">No tasks yet.</p>
      ) : (
        <ul aria-label="Tasks" className="m-0 list-none p-0">
          {tasks.map((t) => (
            <TaskItem key={t.id} task={t} runId={runId} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function alertText(a: BudgetAlert): string {
  const what = a.kind === "hard_stop" ? "stopped the run at its limit" : "was passed";
  const limit = a.limit === null ? "" : ` (${formatMoney(a.limit)}${a.window === "none" ? "" : ` ${WINDOW_NAME[a.window].toLowerCase()}`}${a.spent === null || a.spent === undefined ? "" : `, spent ${formatMoney(a.spent)}`})`;
  const after = a.windowClosed === true
    ? "Overspent in a window that has ended: nothing is blocked, flagged for review."
    : a.held
      ? "Held: stays blocked until a person lifts it."
      : a.resetsAt === null
        ? "Only a person can lift it."
        : `${Date.parse(a.resetsAt) <= Date.now() ? "Reset" : "Resets"} ${formatUtc(a.resetsAt)}.`;
  return `The ${a.scope} budget ${a.budget}${limit} ${what}. ${after}`;
}

/** The budget stops and overspends this run hit, with when each resets or that it is held. */
export function BudgetAlerts({ alerts }: { alerts: BudgetAlert[] }) {
  if (alerts.length === 0) return null;
  return (
    <section aria-labelledby="alerts-title" className="callout-danger flex flex-col gap-2 px-4 py-3">
      <h2 id="alerts-title" className="text-sm font-semibold text-danger">
        Budget alerts
      </h2>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
        {alerts.map((a) => (
          <li key={`${a.ledgerEntry}:${a.budget}`} className="text-[13.5px]" data-held={a.held}>
            {alertText(a)}{" "}
            <Link to={entryHref(a.ledgerEntry)} className="font-mono text-xs text-link underline">
              ledger #{entryLabel(a.ledgerEntry)}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Where the run's money went: by agent and by model. */
export function SpendBreakdown({ run }: { run: RunDetail }) {
  const rows = (items: { name: string; spend: RunDetail["spend"]["spent"]; calls: number }[] | undefined, label: string) =>
    items === undefined || items.length === 0 ? null : (
      <div className="flex flex-col gap-1.5">
        <h3 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">{label}</h3>
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px]">
          {items.map((i) => (
            <li key={i.name} className="flex gap-2">
              <span>{i.name}</span>
              <span className="ml-auto font-mono text-xs">
                {formatMoney(i.spend)} · {i.calls} call{i.calls === 1 ? "" : "s"}
              </span>
            </li>
          ))}
        </ul>
      </div>
    );
  const agents = rows(run.spendByAgent, "By agent");
  const models = rows(run.spendByModel, "By model");
  if (agents === null && models === null) return null;
  return (
    <Panel id="spend-title" title="Where the money went">
      <div className="grid grid-cols-[repeat(auto-fit,minmax(15rem,1fr))] gap-5 p-4">
        {agents}
        {models}
        {(run.costUnknownCalls ?? 0) > 0 && (
          <p role="status" className="m-0 text-[12.5px] text-warm">
            {COST_UNKNOWN_WORDS}: {run.costUnknownCalls} call{run.costUnknownCalls === 1 ? "" : "s"}.
            {run.atLeast != null && ` Total: ${atLeastWords(run.atLeast)}.`}
          </p>
        )}
      </div>
    </Panel>
  );
}

/** Chain and anchor state of this run's own ledger stream. Says "verified" only when the server does. */
export function RunLedgerState({ run }: { run: RunDetail }) {
  const api = useApi();
  const check = useQuery({
    queryKey: ["ledger", "run", run.id, run.ended === true],
    queryFn: () => api.ledger.verifyRun(run.id),
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 1,
    enabled: run.ledger !== undefined,
  });
  if (run.ledger === undefined) return null;
  return (
    <div data-testid="run-ledger-state" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      {check.data !== undefined ? (
        <ChainChip summary={summarizeChain(check.data)} />
      ) : check.isError ? (
        <span role="alert" className="text-danger">
          Couldn't check this run's ledger. {describeProblem(check.error)}
        </span>
      ) : (
        <span role="status" className="text-small">
          Checking this run's ledger…
        </span>
      )}
      <span className="font-mono text-small">{run.ledger.entries} entries</span>
      <Link to="/ledger" className="inline-flex min-h-11 items-center text-link underline">
        Open the ledger
      </Link>
    </div>
  );
}
