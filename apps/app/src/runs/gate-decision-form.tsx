import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { ProblemError } from "@/api/client";
import { describeDecisionProblem } from "@/api/problems";
import type { HumanGate, RunTask } from "@/api/types";
import { reasonProblem } from "@/approvals/rules";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useOrgStatus } from "@/org-safety/use-org-safety";

import { gateCriteria } from "./human-gates";

type Verdict = "approve" | "reject";

/**
 * A person's verdict on a human gate of a waiting task (SA-19): one finding per criterion the
 * gate checks, each with a reason. Nothing is chosen for the person. When this person is the only
 * one who can decide and every finding passes, the gate needs the acknowledgement ("Nobody else
 * will review this."), which is sent as `acknowledgeSolo`. A gate has no digest, so none is sent.
 * A failing decision never needs the acknowledgement.
 */
export function GateDecisionForm({ runId, task, gate }: { runId: string; task: RunTask; gate: HumanGate }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const org = useOrgStatus();
  const gateId = gate.id;
  const criteria = gateCriteria(task, gate);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict | undefined>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [acknowledged, setAcknowledged] = useState(false);
  const [serverSaidSolo, setServerSaidSolo] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const complete = criteria.length > 0 && criteria.every((c) => verdicts[c.id] !== undefined && reasonProblem(reasons[c.id] ?? "") === null);
  const passes = criteria.length > 0 && criteria.every((c) => verdicts[c.id] === "approve");
  const solo = org.data?.soloNow === true || serverSaidSolo;
  const needsAck = solo && passes;
  const attempt = task.attempt;
  const submission = task.submission;

  const decide = useMutation({
    mutationFn: () => {
      if (submission === undefined) throw new Error("This server did not say which round to decide");
      return api.gates.decide(runId, task.id, gateId, {
        attempt,
        submission,
        findings: Object.fromEntries(criteria.map((c) => [c.id, { approved: verdicts[c.id] === "approve", reason: reasons[c.id] ?? "" }])),
        ...(needsAck ? { acknowledgeSolo: true } : {}),
      });
    },
    onSuccess: (done) => {
      setProblem(null);
      setResult(done.outcome === "passed" ? "Gate passed. Your decision is in the ledger." : "Gate failed. Your decision is in the ledger.");
    },
    onError: (error) => {
      if (error instanceof ProblemError && error.title === "acknowledge_required") setServerSaidSolo(true);
      setProblem(describeDecisionProblem(error));
    },
    onSettled: async () => {
      await Promise.all([queryClient.invalidateQueries({ queryKey: keys.runs }), queryClient.invalidateQueries({ queryKey: keys.ledger })]);
    },
  });

  if (result !== null) {
    return (
      <p role="status" className="callout-warning px-3 py-2 text-[13.5px]">
        {result}
      </p>
    );
  }
  const disabled = !complete || (needsAck && !acknowledged) || decide.isPending || submission === undefined;
  return (
    <form
      aria-label={`Decide gate ${gateId} of ${task.title}`}
      className="callout-warning flex flex-col gap-3 p-3"
      onSubmit={(e) => {
        e.preventDefault();
      }}
    >
      <h4 className="text-[13.5px] font-semibold">Your decision on gate {gateId}</h4>
      {criteria.map((c) => (
        <fieldset key={c.id} className="m-0 flex flex-col gap-1.5 border-0 p-0">
          <legend className="text-[13.5px]">{c.text}</legend>
          <div className="flex gap-4 text-[13px]">
            {(["approve", "reject"] as const).map((v) => (
              <label key={v} className="inline-flex min-h-11 items-center gap-1.5">
                <input
                  type="radio"
                  name={`verdict-${c.id}`}
                  checked={verdicts[c.id] === v}
                  onChange={() => {
                    setVerdicts((cur) => ({ ...cur, [c.id]: v }));
                    setAcknowledged(false);
                  }}
                />
                {v === "approve" ? "Approve" : "Reject"}
              </label>
            ))}
          </div>
          <Textarea
            rows={2}
            aria-label={`Reason for: ${c.text}`}
            value={reasons[c.id] ?? ""}
            onChange={(e) => {
              setReasons((cur) => ({ ...cur, [c.id]: e.target.value }));
            }}
          />
        </fieldset>
      ))}
      {needsAck && (
        <label className="inline-flex items-start gap-2 text-[13.5px]">
          <input
            type="checkbox"
            className="mt-1"
            checked={acknowledged}
            onChange={(e) => {
              setAcknowledged(e.target.checked);
            }}
          />
          <span>
            You are the only person who can decide this gate. Nobody else will review this.
          </span>
        </label>
      )}
      {problem !== null && (
        <p role="alert" className="text-xs text-destructive">
          {problem}
        </p>
      )}
      <div>
        <Button
          type="button"
          size="lg"
          className="min-h-11 px-5"
          disabled={disabled}
          onClick={() => {
            decide.mutate();
          }}
        >
          Submit decision
        </Button>
      </div>
    </form>
  );
}
