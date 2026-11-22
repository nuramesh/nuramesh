import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { NotFoundError } from "@/api/client";
import { describeProblem } from "@/api/problems";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { formatMoney, percentUsed } from "@/api/money";
import type { RunDetail } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useCanAct } from "@/org-safety/use-org-safety";

import { GateCard } from "./gate-card";
import { canPause, canStop, formatElapsed, formatStarted, isLive } from "./format";
import { BudgetAlerts, RunLedgerState, SpendBreakdown, TaskGates } from "./run-details";
import { GraphLegend, RunGraph } from "./run-graph";
import { StatusPill } from "./status-pill";
import { Timeline } from "./timeline";
import { runKey, useRun, useRunEvents } from "./use-run";

function Stat({ label, children, detail, className }: { label: string; children: React.ReactNode; detail?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-1.5 rounded-xl border bg-panel px-4 py-3.5 border-card-border shadow-card", className)}>
      <span className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">{label}</span>
      <span className="text-[22px] font-semibold">{children}</span>
      {detail !== undefined && <span className="text-[12.5px] text-muted-foreground">{detail}</span>}
    </div>
  );
}

function Unit({ children }: { children: React.ReactNode }) {
  return <span className="text-sm font-normal text-muted-foreground">{children}</span>;
}

function StatCards({ run }: { run: RunDetail }) {
  const running = run.graph.nodes.filter((n) => !n.isHuman && n.status === "running").length;
  const waiting = run.graph.nodes.filter((n) => n.status === "waiting").length;
  const toCome = Math.max(0, run.gates.total - run.gates.passed - run.gates.failed);
  const limit = run.spend.limit;
  const percent = limit === null ? 0 : percentUsed(run.spend.spent, limit);
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(12.5rem,1fr))] gap-3.5">
      <Stat label="Tasks" detail={`${String(running)} running · ${String(waiting)} waiting`}>
        {run.tasks.done} <Unit>of {run.tasks.total} done</Unit>
      </Stat>
      <Stat label="Gates" detail={`${String(run.gates.failed)} failed · ${String(toCome)} to come`}>
        {run.gates.passed} <Unit>passed</Unit>
      </Stat>
      <Stat label="Spend">
        {formatMoney(run.spend.spent)} {limit !== null && <Unit>of {formatMoney(limit)}</Unit>}
        {limit === null ? (
          <span className="mt-1.5 block text-[12.5px] font-normal text-muted-foreground">No run budget recorded</span>
        ) : (
          <span role="progressbar" aria-label="Spend of the run budget" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} className="mt-1.5 block h-[5px] overflow-hidden rounded-full bg-track">
            <span className={cn("block h-full", percent >= 90 ? "bg-warm" : "bg-link")} style={{ width: `${String(percent)}%` }} />
          </span>
        )}
      </Stat>
      {run.needsYou > 0 ? (
        <Link to="/approvals" data-testid="needs-you" className="callout-warning flex flex-col gap-1.5 px-4 py-3.5 text-foreground no-underline">
          <span className="font-mono text-[11px] tracking-[0.1em] text-warm uppercase">Needs you</span>
          <span className="text-[22px] font-semibold">
            {run.needsYou} <Unit>{run.needsYou === 1 ? "approval" : "approvals"}</Unit>
          </span>
          <span className="text-[12.5px] text-muted-foreground">Open the approvals inbox →</span>
        </Link>
      ) : (
        <Stat label="Needs you" detail="Nothing is waiting for a person">
          0 <Unit>approvals</Unit>
        </Stat>
      )}
    </div>
  );
}

/** One run live (SPEC-app-v1 3.2): header and controls, four numbers, the graph, the latest gate and the timeline. */
export function RunPage() {
  const { id = "" } = useParams();
  return <RunView key={id} id={id} />;
}

function RunView({ id }: { id: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const run = useRun(id);
  const canAct = useCanAct();
  const current = run.data;
  const stream = useRunEvents(id, !run.isError && current !== undefined && isLive(current));
  const [stopOpen, setStopOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");

  const control = useMutation({
    mutationFn: ({ action }: { action: "pause" | "stop" }) => (action === "pause" ? api.runs.pause(id) : api.runs.stop(id, reason)),
    onSuccess: (result, { action }) => {
      const entry = result.ledgerEntry === null ? "" : ` Ledger entry #${result.ledgerEntry}.`;
      if (action === "pause") setNote(`Paused ${result.result.label}.${entry}`);
      else if (result.result.status === "stopping") {
        setNote(`Stop requested for ${result.result.label}${result.result.stopRequestedBy ? ` by ${result.result.stopRequestedBy}` : ""}${result.result.stopReasonWithheld ? ` (${result.result.stopReasonWithheld})` : ""}.${entry} The run shows "Stopping…" until it ends; a model call already in flight finishes and is charged.`);
      } else setNote(`Stopped ${result.result.label}.${entry}`);
      setStopOpen(false);
      setReason("");
    },
    onError: (error, { action }) => {
      if (action === "pause") setNote(`Couldn't pause the run: ${describeProblem(error)}`);
      // A stop that was refused keeps its dialog open, with the reason, so nothing typed is lost.
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: runKey(id) }),
        queryClient.invalidateQueries({ queryKey: keys.runs }),
        queryClient.invalidateQueries({ queryKey: keys.ledger }),
        queryClient.invalidateQueries({ queryKey: keys.budgetSummary }),
      ]);
    },
  });
  const stopFailure = control.isError && control.variables.action === "stop" ? describeProblem(control.error) : null;

  if (run.isError && run.data === undefined) {
    const missing = run.error instanceof NotFoundError;
    return (
      <section aria-labelledby="page-title" className="mx-auto flex max-w-2xl flex-col gap-3 p-6 md:p-10">
        <title>Run · NuraMesh</title>
        <h1 id="page-title" className="text-2xl font-semibold">
          {missing ? "Run not found" : "Couldn't load the run"}
        </h1>
        <p role="alert" className="text-muted-foreground">
          {missing ? `There is no run ${id}.` : run.error.message}
        </p>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link to="/runs">All runs</Link>
          </Button>
          {!missing && (
            <Button
              variant="outline"
              onClick={() => {
                void run.refetch();
              }}
            >
              Try again
            </Button>
          )}
        </div>
      </section>
    );
  }
  const data = run.data;
  if (data === undefined) {
    return (
      <p role="status" className="p-6 text-sm text-muted-foreground">
        Loading run…
      </p>
    );
  }
  return (
    <div className="flex flex-col">
      <title>{`Run ${data.label} · NuraMesh`}</title>
      <header className="flex flex-col gap-2.5 border-b bg-panel px-6 py-4">
        <nav aria-label="Breadcrumb" className="flex gap-2 text-[13.5px] text-muted-foreground">
          <Link to="/runs" className="inline-flex min-h-11 items-center text-muted-foreground no-underline hover:underline">
            Runs
          </Link>
          <span aria-hidden="true" className="text-small">
            /
          </span>
          <span className="text-foreground">{data.label}</span>
        </nav>
        <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5">
          <h1 id="page-title" className="text-[21px] font-semibold tracking-tight">
            {data.title}
          </h1>
          <StatusPill status={data.status} />
          <span className="flex-1" />
          {api.runs.reportUrl !== undefined && (
            <a
              href={api.runs.reportUrl(data.id)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex min-h-11 items-center text-sm text-link underline"
            >
              Report
            </a>
          )}
          {!canAct && isLive(data) && <span className="text-xs text-muted-foreground">Only a signed-in person can stop a run.</span>}
          {canAct && api.source !== "server" && (
            <Button
              variant="outline"
              className="min-h-11"
              disabled={!canPause(data.status, data.ended) || control.isPending}
              onClick={() => {
                control.mutate({ action: "pause" });
              }}
            >
              Pause
            </Button>
          )}
          {canAct && (
            <Button
              variant="outline"
              className="min-h-11 border-danger text-danger hover:bg-danger-soft hover:text-danger"
              disabled={!canStop(data.status, data.ended) || control.isPending}
              onClick={() => {
                control.reset();
                setStopOpen(true);
              }}
            >
              {data.status === "stopping" ? "Stopping…" : "Stop"}
            </Button>
          )}
        </div>
        <p className="m-0 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-small">
          <span>{data.template === "" ? `organization ${data.org ?? "unknown"}` : `${data.template} template`}</span>
          <span>
            {data.startedAt === null ? "start not recorded" : `started ${formatStarted(data.startedAt)}`} · {data.elapsedSeconds === null ? "time not recorded" : formatElapsed(data.elapsedSeconds)}
          </span>
          <span>canon {data.canonRef}</span>
          {data.sandbox !== "not recorded" && <span>sandbox: {data.sandbox}</span>}
          <span>started by {data.startedBy}</span>
        </p>
        <p role="status" aria-live="polite" className="m-0 min-h-4 text-xs text-muted-foreground">
          {note}
        </p>
        <RunLedgerState run={data} />
      </header>

      <div className="flex flex-col gap-5 px-6 pt-5 pb-7">
        {data.status === "stopping" && (
          <div role="status" data-testid="stopping-banner" className="callout-warning flex flex-col gap-1 px-4 py-3">
            <p className="m-0 text-sm font-semibold">Stopping…{data.stopRequestedBy ? ` requested by ${data.stopRequestedBy}` : ""}{data.stopReasonWithheld ? ` (${data.stopReasonWithheld})` : ""}</p>
            <p className="m-0 text-[13px] text-muted-foreground">
              The run ends at its next checkpoint. A model call that is already in flight finishes and is charged, so the final spend can include one more call.
            </p>
          </div>
        )}
        {data.status === "stopped" && data.ended === true && (
          <p role="status" className="m-0 text-[13px] text-muted-foreground">
            This run was stopped{data.stopRequestedBy ? ` by ${data.stopRequestedBy}` : ""}. The spend shown is final.
          </p>
        )}
        <BudgetAlerts alerts={data.budgetAlerts ?? []} />
        <StatCards run={data} />
        <div className="flex flex-wrap items-start gap-5">
          <div className="flex min-w-0 flex-[999_1_32rem] flex-col gap-5">
            <section aria-labelledby="graph-title" className="overflow-hidden rounded-xl border bg-panel border-card-border shadow-card">
              <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
                <h2 id="graph-title" className="text-sm font-semibold">
                  Live graph
                </h2>
                <span className="flex-1" />
                <GraphLegend />
              </div>
              <RunGraph nodes={data.graph.nodes} edges={data.graph.edges} />
            </section>
            <section aria-labelledby="gate-title" className="rounded-xl border bg-panel border-card-border shadow-card">
              <h2 id="gate-title" className="sr-only">
                Latest gate result
              </h2>
              <GateCard gate={data.latestGate} />
            </section>
            {data.taskList !== undefined && <TaskGates tasks={data.taskList} runId={data.id} />}
            <SpendBreakdown run={data} />
          </div>
          <Timeline runId={data.id} events={stream.events} loading={stream.loading} unavailable={stream.unavailable} mode={stream.mode} />
        </div>
      </div>

      <Dialog
        open={stopOpen}
        onOpenChange={(open) => {
          if (!open && control.isPending) return; // don't lose track of a stop that is being sent
          setStopOpen(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop run {data.label}?</DialogTitle>
            <DialogDescription>
              The run stops at its next checkpoint and unfinished tasks are cancelled. A model call that is already in flight cannot be cancelled: it finishes and is charged. Money already spent stays spent. This is recorded in the ledger under your name and can't be undone.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="stop-reason" className="text-[13px] font-semibold">
              Reason <span className="font-normal text-muted-foreground">(optional; the ledger keeps only a hash of it)</span>
            </label>
            <Textarea
              id="stop-reason"
              rows={2}
              value={reason}
              maxLength={1000}
              onChange={(e) => {
                setReason(e.target.value);
              }}
            />
            <p id="stop-error" role="alert" className="min-h-4 text-xs text-destructive">
              {stopFailure ?? ""}
            </p>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={control.isPending}
              onClick={() => {
                setStopOpen(false);
              }}
            >
              Keep running
            </Button>
            <Button
              variant="destructive"
              disabled={control.isPending}
              onClick={() => {
                control.mutate({ action: "stop" });
              }}
            >
              {control.isPending ? "Stopping…" : "Stop"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
