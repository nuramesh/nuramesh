import { useState } from "react";
import { Link } from "react-router";

import { NotAvailableError } from "@/api/client";
import { formatMoney, percentUsed } from "@/api/money";
import { describeProblem } from "@/api/problems";
import type { RunStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { FilterPill } from "@/components/ui/chip";

import { formatStarted, STATUS_LABEL } from "./format";
import { StatusPill } from "./status-pill";
import { useRuns } from "./use-run";

function Problem({ error, retry }: { error: unknown; retry: () => void }) {
  const unavailable = error instanceof NotAvailableError;
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-dashed p-6">
      <p className="text-sm">
        {unavailable ? "This data source can't list runs yet." : "Couldn't load the runs."}{" "}
        <span className="text-muted-foreground">{unavailable ? "" : describeProblem(error)}</span>
      </p>
      <Button variant="outline" className="min-h-11" onClick={retry}>
        Try again
      </Button>
    </div>
  );
}

const FILTERS: (RunStatus | "all")[] = ["all", "running", "stopping", "waiting", "done", "stopped", "failed"];

/** Every run of the organization (SPEC-app-v1 3.2): status, template, start, spend and gates. */
export function RunsPage() {
  const [filter, setFilter] = useState<RunStatus | "all">("all");
  const runs = useRuns(filter === "all" ? undefined : filter);
  const data = runs.data?.runs;
  const running = data?.filter((r) => r.status === "running" || r.status === "stopping").length ?? 0;
  const waiting = data?.filter((r) => r.status === "waiting").length ?? 0;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4 p-6">
      <title>Runs · NuraMesh</title>
      <header className="flex flex-col gap-1">
        <h1 id="page-title" className="text-2xl font-semibold">
          Runs
        </h1>
        <p className="text-sm text-muted-foreground" data-testid="runs-summary">
          {data === undefined ? "Loading…" : `${String(data.length)} run${data.length === 1 ? "" : "s"} · ${String(running)} running · ${String(waiting)} waiting for you`}
        </p>
      </header>
      <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <FilterPill
            key={f}
            pressed={filter === f}
            onClick={() => {
              setFilter(f);
            }}
          >
            {f === "all" ? "All" : STATUS_LABEL[f].replace("…", "")}
          </FilterPill>
        ))}
      </div>
      {runs.data?.truncated === true && (
        <p role="status" data-testid="runs-truncated" className="callout-warning px-4 py-3 text-sm">
          The server stopped looking before it had checked every run, so this list may be missing some. Pick a status above to narrow it.
        </p>
      )}
      {runs.isError ? (
        <Problem
          error={runs.error}
          retry={() => {
            void runs.refetch();
          }}
        />
      ) : data === undefined ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading runs…
        </p>
      ) : data.length === 0 ? (
        <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          {filter === "all" ? (
            <>
              No runs yet. Start one from the <Link to="/builder" className="text-link underline">org builder</Link>, or from a terminal with <code>nuramesh try</code>.
            </>
          ) : (
            `No ${STATUS_LABEL[filter].replace("…", "").toLowerCase()} runs.`
          )}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-panel border-card-border shadow-card">
          <table className="w-full border-collapse text-sm md:min-w-[34rem]">
            <caption className="sr-only">Runs, newest first</caption>
            <thead>
              <tr className="table-head border-b">
                <th scope="col" className="px-4 py-3">
                  Run
                </th>
                <th scope="col" className="hidden px-3 py-3 md:table-cell">
                  Status
                </th>
                <th scope="col" className="hidden px-3 py-3 md:table-cell">
                  Template / org
                </th>
                <th scope="col" className="hidden px-3 py-3 md:table-cell">
                  Started
                </th>
                <th scope="col" className="px-3 py-3">
                  Spend
                </th>
                <th scope="col" className="hidden px-4 py-3 md:table-cell">
                  Gates
                </th>
              </tr>
            </thead>
            <tbody>
              {[...data]
                .sort((a, b) => (b.startedAt === null ? 0 : Date.parse(b.startedAt)) - (a.startedAt === null ? 0 : Date.parse(a.startedAt)))
                .map((run) => (
                  <tr key={run.id} className="border-b last:border-b-0 hover:bg-panel-2">
                    <th scope="row" className="min-w-0 px-3 py-3 text-left font-normal md:px-4">
                      <Link to={`/runs/${run.id}`} className="flex min-h-11 flex-col justify-center no-underline">
                        <span className="font-mono text-xs text-small">{run.label}</span>
                        <span className="font-medium text-foreground underline-offset-2 hover:underline">{run.title}</span>
                        {/* The Status column is hidden on a phone: the pill sits under the title. */}
                        <StatusPill status={run.status} className="my-1 self-start md:hidden" />
                        <span className="text-xs text-muted-foreground">
                          {run.tasks.done} of {run.tasks.total} tasks done
                        </span>
                        {/* The Gates column is hidden on a phone, so its facts move under the title. */}
                        <span className="text-xs text-muted-foreground md:hidden" data-testid="run-gates-phone">
                          Gates {run.gates.passed}/{run.gates.total} passed
                          {run.gates.failed > 0 && <span className="ml-1 font-medium text-danger">· {run.gates.failed} failed</span>}
                        </span>
                      </Link>
                    </th>
                    <td className="hidden px-2 py-3 md:table-cell md:px-3">
                      <StatusPill status={run.status} />
                    </td>
                    <td className="hidden px-3 py-3 text-muted-foreground md:table-cell">{run.template === "" ? (run.org ?? "-") : run.template}</td>
                    <td className="hidden px-3 py-3 text-muted-foreground md:table-cell">{run.startedAt === null ? "not recorded" : formatStarted(run.startedAt)}</td>
                    <td className="px-2 py-3 md:px-3">
                      <div className="flex flex-col gap-1 md:min-w-24">
                        <span className="font-mono text-xs">
                          {formatMoney(run.spend.spent)} {run.spend.limit !== null && <span className="text-small">of {formatMoney(run.spend.limit)}</span>}
                        </span>
                        {run.spend.limit !== null && (
                          <span role="presentation" className="h-1.5 overflow-hidden rounded-full bg-track">
                            <span className="block h-full bg-link" style={{ width: `${String(percentUsed(run.spend.spent, run.spend.limit))}%` }} />
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="hidden px-4 py-3 text-muted-foreground md:table-cell">
                      {run.gates.passed}/{run.gates.total} passed
                      {run.gates.failed > 0 && <span className="ml-1 font-medium text-danger">· {run.gates.failed} failed</span>}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
