import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { ApiError, NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { describeProblem } from "@/api/problems";
import type { RunPlan, StartedRun } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

import { FIRST_APPROVAL_PLAN } from "./first-plan";

const WATCH_MS = 2_000;

/** The text as a plan, or the reason it is not one. Only the shape "a JSON object"; the server checks the rest. */
function parsePlan(text: string): RunPlan | string {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return "The plan is not valid JSON.";
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) return "The plan must be a JSON object with data_class and tasks.";
  return value as RunPlan;
}

/** What the server (or this page) said about a start that did not go through, with the server's code when it has one. */
function refusal(error: unknown): string {
  if (error instanceof NotAvailableError) return "This data source can't start a run. Choose real data (a server started with --with-runner).";
  const generic = new Set(["invalid_request", "not_found", "conflict", "not_allowed", "error"]);
  const code = error instanceof ApiError && !generic.has(error.code) ? ` (${error.code})` : "";
  return `${describeProblem(error)}${code}`;
}

/**
 * The Start a run button and its dialog (the quickstart guide, section 9): a plan as JSON, sent to
 * `POST /v1/runs` as written. A refusal stays in the dialog with the server's code and the text
 * typed; an accepted plan closes it, and the page follows the request until a runner has begun the
 * run (then it links it) or refused it (then it names the code). Shown only to a caller who may act
 * (the caller decides; the server still checks the call itself).
 */
export function StartRun() {
  const api = useApi();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(FIRST_APPROVAL_PLAN);
  const [invalid, setInvalid] = useState<string | null>(null);
  const [started, setStarted] = useState<StartedRun | null>(null);

  const start = useMutation({
    mutationFn: (plan: RunPlan) => api.runs.startPlan(plan),
    onSuccess: (result) => {
      setStarted(result);
      setOpen(false);
    },
  });
  const failure = invalid ?? (start.isError ? refusal(start.error) : null);

  // An accepted plan is only a request: a runner begins it or refuses it a moment later. Until it
  // has begun the run has no page, so this asks every two seconds and shows what became of it.
  const runId = started?.runId ?? null;
  const status = useQuery({
    queryKey: ["runs", "start", runId],
    queryFn: () => api.runs.startStatus(runId ?? ""),
    enabled: runId !== null,
    retry: false,
    refetchInterval: (query) => (query.state.data?.state === "requested" ? WATCH_MS : false),
  });
  const state = status.data?.state;
  useEffect(() => {
    if (state !== "begun") return;
    void queryClient.invalidateQueries({ queryKey: keys.runsList });
    void queryClient.invalidateQueries({ queryKey: ["runs", "page"] });
    void queryClient.invalidateQueries({ queryKey: keys.approvals });
  }, [state, queryClient]);

  return (
    <>
      <Button
        className="min-h-11 self-start"
        onClick={() => {
          setInvalid(null);
          start.reset();
          setOpen(true);
        }}
      >
        Start a run
      </Button>
      {started !== null && state === "refused" && (
        <p role="alert" data-testid="run-refused" className="rounded-lg border border-dashed px-4 py-3 text-sm">
          The runner refused the plan of <span className="font-mono text-xs">{started.runId}</span>
          {status.data?.refusalCode == null ? "." : <>: <code>{status.data.refusalCode}</code>.</>} Nothing was started. Fix the cause (the quickstart guide, section 9) and start again.
        </p>
      )}
      {started !== null && state !== "refused" && (
        <p role="status" data-testid="run-started" className="rounded-lg border bg-panel px-4 py-3 text-sm">
          {started.state === "already_requested" ? "This run was already requested: " : "The server accepted the plan: "}
          <span className="font-mono text-xs">{started.runId}</span>.{" "}
          {state === "begun" ? (
            <>
              The run has begun.{" "}
              <Link to={`/runs/${started.runId}`} className="text-link underline">
                Open the run
              </Link>
              . If it asks for a decision, the request shows under{" "}
              <Link to="/approvals" className="text-link underline">
                Approvals
              </Link>
              ; another signed-in person has to decide it, not you.
            </>
          ) : status.isError ? (
            <>Couldn&apos;t read what became of it: {describeProblem(status.error)} Look for it in the list below.</>
          ) : (
            <>Waiting for a runner to begin it…</>
          )}
        </p>
      )}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next && start.isPending) return; // don't lose track of a start that is being sent
          setOpen(next);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Start a run</DialogTitle>
            <DialogDescription>
              The plan is sent to the server as written and recorded with your id. A runner on the server begins the run or refuses it; this page then says which, with the runner's code. Once a second person can decide, you cannot decide a request of a run you started: that person does. With no second person, a solo approval needs your explicit acknowledgement.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="start-plan" className="text-[13px] font-semibold">
              Plan (JSON)
            </label>
            <Textarea
              id="start-plan"
              rows={12}
              spellCheck={false}
              className="max-h-[50vh] font-mono text-xs"
              value={text}
              aria-invalid={failure !== null}
              aria-describedby="start-error"
              onChange={(e) => {
                setText(e.target.value);
                setInvalid(null);
              }}
            />
            <p id="start-error" role="alert" className="min-h-4 text-xs text-destructive">
              {failure ?? ""}
            </p>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={start.isPending}
              onClick={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              disabled={start.isPending}
              onClick={() => {
                const plan = parsePlan(text);
                if (typeof plan === "string") {
                  start.reset();
                  setInvalid(plan);
                  return;
                }
                setInvalid(null);
                start.mutate(plan);
              }}
            >
              {start.isPending ? "Starting…" : "Start"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
