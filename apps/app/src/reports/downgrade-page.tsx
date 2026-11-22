import { useQuery } from "@tanstack/react-query";

import { NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import { formatPerTask, formatRate, formatCostShare, type RawCounts, type Recommendation } from "@/api/reports";
import { Button } from "@/components/ui/button";
import { Chip, type ChipTone } from "@/components/ui/chip";

function Problem({ error, retry }: { error: unknown; retry: () => void }) {
  const unavailable = error instanceof NotAvailableError;
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-dashed p-6">
      <p className="text-sm">
        {unavailable ? "This data source can't give this report yet." : "Couldn't load the report."}{" "}
        <span className="text-muted-foreground">{unavailable ? "" : describeProblem(error)}</span>
      </p>
      <Button variant="outline" className="min-h-11" onClick={retry}>
        Try again
      </Button>
    </div>
  );
}

/** "18 of 20" (passed of the results that were verdicts), "-" with none; errors are named, not counted. */
function Passed({ counts }: { counts: RawCounts }) {
  const decided = counts.passed + counts.failed;
  return (
    <>
      <span>{decided === 0 ? "-" : `${String(counts.passed)} of ${String(decided)}`}</span>
      {counts.error > 0 && (
        <span className="block text-xs text-muted-foreground">
          {counts.error} error{counts.error === 1 ? "" : "s"}
        </span>
      )}
    </>
  );
}

const TONE: Record<Recommendation["status"], ChipTone> = {
  recommend: "success",
  none: "neutral",
  insufficient_evidence: "warning",
};

const LABEL: Record<Recommendation["status"], string> = {
  recommend: "Cheaper model supported",
  none: "No cheaper model is supported by the evidence",
  insufficient_evidence: "Not enough evidence",
};

const REFUSED: Record<string, string> = {
  not_eligible: "not allowed for this role",
  same_family_as_verifier: "same model family as the verifier",
};

/** The one answer for a role, with the evidence next to the claim. It never says "safe". */
function RecommendationBlock({ recommendation, minSample }: { recommendation: Recommendation | null; minSample: number }) {
  if (recommendation === null) return null;
  const { status, candidate, saving, passRateCurrent, passRateCandidate, tasksCurrent, tasksCandidate, refused } = recommendation;
  return (
    <div className="flex flex-col items-start gap-1" data-testid="recommendation">
      <Chip tone={TONE[status]}>{LABEL[status]}</Chip>
      {status === "recommend" && candidate !== null && (
        <span className="text-sm">
          {`Consider ${candidate} instead of ${recommendation.current}: pass rate per attempt ${formatCostShare(passRateCurrent ?? "")} vs ${formatCostShare(passRateCandidate ?? "")} over ${String(tasksCurrent)} and ${String(tasksCandidate)} tasks${saving === null ? "" : `, saves ${formatCostShare(saving)} per accepted result`}`}
        </span>
      )}
      {status === "insufficient_evidence" && (
        <span className="text-xs text-muted-foreground">Each model needs at least {minSample} finished tasks.</span>
      )}
      {refused.map((r) => (
        <span key={r.model} className="text-xs text-muted-foreground">
          {`${r.model} was left out: ${REFUSED[r.reason] ?? r.reason}`}
        </span>
      ))}
    </div>
  );
}

/**
 * The evidence-based downgrade report (ND63): for each role and model, how often its work passed
 * the gates and the verifiers, next to what a task costs. It only reads and advises: nothing on
 * this page changes a model.
 */
export function DowngradePage() {
  const api = useApi();
  const report = useQuery({
    queryKey: ["reports", "downgrade"],
    queryFn: () => api.reports.downgrade(),
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 2,
  });
  const data = report.data;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4 p-4 sm:p-6">
      <title>Model downgrade report · NuraMesh</title>
      <header className="flex flex-col gap-1">
        <h1 id="page-title" className="text-2xl font-semibold">
          Model downgrade report
        </h1>
        <p className="text-sm text-muted-foreground">
          How often each model&apos;s work passed the gates and verifiers, next to what a task costs. This report only advises; it never switches a
          model. A cheaper model is recommended only when its price is known, it has at least {data?.minSample ?? 30} finished tasks, and it passed as
          often per attempt.
        </p>
        {data !== undefined && (
          <p className="text-xs text-muted-foreground" data-testid="downgrade-summary">
            {data.runsScanned} run{data.runsScanned === 1 ? "" : "s"} read for {data.org}.{" "}
            {data.excluded.mixedModel + data.excluded.notEvidence > 0 &&
              `${String(data.excluded.mixedModel + data.excluded.notEvidence)} task${data.excluded.mixedModel + data.excluded.notEvidence === 1 ? "" : "s"} left out (mixed models or no clear result yet).`}
          </p>
        )}
      </header>
      {data?.truncated === true && (
        <p role="status" data-testid="downgrade-truncated" className="callout-warning px-4 py-3 text-sm">
          The server stopped looking before it had read every run, so this report may be missing some work.
        </p>
      )}
      {report.isError ? (
        <Problem
          error={report.error}
          retry={() => {
            void report.refetch();
          }}
        />
      ) : data === undefined ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading the report…
        </p>
      ) : data.roles.length === 0 ? (
        <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          No finished work to compare yet. The report fills in as runs record gate results for work done with a model.
        </p>
      ) : (
        data.roles.map((role) => (
          <div key={role.role} className="flex flex-col gap-2">
            <h2 className="text-lg font-semibold">{role.role}</h2>
            <RecommendationBlock recommendation={role.recommendation} minSample={data.minSample} />
            <div className="overflow-x-auto rounded-xl border bg-panel border-card-border shadow-card">
              <table className="w-full min-w-[40rem] border-collapse text-sm">
                <caption className="sr-only">{role.role}: pass rate and cost per task, by model</caption>
                <thead>
                  <tr className="table-head border-b">
                    <th scope="col" className="px-4 py-3 text-left">
                      Model
                    </th>
                    <th scope="col" className="px-3 py-3 text-left">
                      Tasks
                    </th>
                    <th scope="col" className="px-3 py-3 text-left">
                      Gates passed
                    </th>
                    <th scope="col" className="px-3 py-3 text-left">
                      Verifier passed
                    </th>
                    <th scope="col" className="px-3 py-3 text-left">
                      Pass rate
                    </th>
                    <th scope="col" className="px-3 py-3 text-left">
                      Cost per task
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {role.models.map((m) => (
                    <tr key={m.model} className="border-b align-top last:border-b-0">
                      <th scope="row" className="px-4 py-3 text-left font-normal">
                        <span className="font-mono text-xs">{m.model}</span>
                      </th>
                      <td className="px-3 py-3">{m.tasks}</td>
                      <td className="px-3 py-3">
                        <Passed counts={m.gate} />
                      </td>
                      <td className="px-3 py-3">
                        <Passed counts={m.verifier} />
                      </td>
                      <td className="px-3 py-3">
                        <span>{formatRate(m.passRate)}</span>
                        <span className="block text-xs text-muted-foreground">{m.results} results</span>
                      </td>
                      <td className="px-3 py-3">
                        <span className="font-mono text-xs">{formatPerTask(m.costPerTask)}</span>
                        {m.unknownCostCalls > 0 && (
                          <span className="block text-xs text-muted-foreground">
                            {m.unknownCostCalls} call{m.unknownCostCalls === 1 ? "" : "s"} without a price
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </section>
  );
}
