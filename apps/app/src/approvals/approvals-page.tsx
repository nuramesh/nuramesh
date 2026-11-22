import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useParams } from "react-router";

import { NotAllowedError, NotAvailableError, NotFoundError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { describeDecisionProblem, describeProblem } from "@/api/problems";
import { formatMoney } from "@/api/money";
import type { Approval } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useCanAct } from "@/org-safety/use-org-safety";
import { formatClock, formatStarted } from "@/lib/time";

import { entryHref, entryLabel } from "@/ledger/format";

import { canDecide, reasonProblem, sortApprovals, STATE_LABEL, STATE_STYLE, STATE_TONE } from "./rules";
import { consequenceFor, costLine, escapeInert, MAX_TARGET, sentenceOf } from "./sentence";
import { SoloApproveDialog } from "./solo-approve-dialog";

/** Agent-chosen text wraps inside its column and never widens the page (AE-6). */
const WRAP = "min-w-0 [overflow-wrap:anywhere]";

function Section({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 rounded-xl border bg-panel px-4 py-3.5 border-card-border shadow-card", className)}>
      <h3 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">{label}</h3>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-[13px]">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={cn("ml-auto text-right", WRAP)}>{children}</dd>
    </div>
  );
}

const SELF_APPROVAL = "You started this run, so someone else has to decide it.";

/** The decision form: a reason is required; Approve and Reject stay disabled until there is one (AE-9, AE-10). */
function DecisionForm({ approval, onRefused, onDone }: { approval: Approval; onRefused: (text: string) => void; onDone: (decision: "approve" | "reject") => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const missing = reasonProblem(reason);
  const decide = useMutation({
    // A peer's approve and every reject. A sole decider's approve goes through the confirm dialog below, the only sender of the solo terms (SA-17).
    mutationFn: (decision: "approve" | "reject") => api.approvals.decide(approval.id, { decision, reason }),
    onSuccess: (_result, decision) => {
      setProblem(null);
      onDone(decision);
    },
    onError: (error) => {
      if (error instanceof ProblemError && error.status === 403 && error.title === "self_approval") setProblem(SELF_APPROVAL);
      else if (error instanceof NotAllowedError) onRefused("Your decision was not recorded: this request was already decided (perhaps in another window). It is read-only now.");
      else setProblem(`Couldn't save the decision: ${describeDecisionProblem(error, false)}`);
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.approvals }),
        queryClient.invalidateQueries({ queryKey: ["approval", approval.id] }),
        queryClient.invalidateQueries({ queryKey: keys.ledger }),
        queryClient.invalidateQueries({ queryKey: keys.runs }),
      ]);
    },
  });
  const submit = (decision: "approve" | "reject"): void => {
    setTouched(true);
    if (missing !== null) return;
    if (decision === "approve" && approval.solo != null) {
      setProblem(null);
      setConfirming(true); // nothing is sent until the dialog's own confirm
      return;
    }
    decide.mutate(decision);
  };
  return (
    <form
      className="callout-warning flex flex-col gap-2.5 p-4"
      onSubmit={(e) => {
        e.preventDefault();
      }}
    >
      <label htmlFor="reason" className="text-[13.5px] font-semibold">
        Your reason <span className="font-normal text-muted-foreground">(required)</span>
      </label>
      <Textarea
        id="reason"
        rows={3}
        className="text-base md:text-sm"
        value={reason}
        placeholder="For example: reviewed the diff, tests and verifier verdict"
        aria-invalid={touched && missing !== null ? true : undefined}
        aria-describedby="reason-help reason-error"
        onChange={(e) => {
          setReason(e.target.value);
        }}
      />
      <p id="reason-help" className="text-xs text-muted-foreground">
        Your reason is saved with your decision. The record keeps a fingerprint of it, not the text.
      </p>
      <p id="reason-error" role="alert" className="min-h-4 text-xs text-destructive">
        {touched && missing !== null ? missing : (problem ?? "")}
      </p>
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
        <Button
          type="button"
          size="lg"
          className="min-h-11 px-5"
          disabled={missing !== null || decide.isPending}
          aria-describedby={missing !== null ? "approve-hint" : undefined}
          onClick={() => {
            submit("approve");
          }}
        >
          Approve
        </Button>
        <Button
          type="button"
          size="lg"
          variant="outline"
          className="min-h-11 border-danger px-5 text-danger hover:bg-danger-soft hover:text-danger"
          disabled={missing !== null || decide.isPending}
          onClick={() => {
            submit("reject");
          }}
        >
          Reject
        </Button>
        {missing !== null && (
          <span id="approve-hint" className="text-[12.5px] font-medium text-foreground">
            Write a reason to answer
          </span>
        )}
      </div>
      <p className="text-[12.5px] text-muted-foreground">
        Works once, for this action. The agent never decides for itself, and your answer is recorded.{" "}
        <Link to="/help/approving" className="inline-flex min-h-11 items-center text-link underline">
          What am I approving?
        </Link>
      </p>
      <SoloApproveDialog
        open={confirming}
        onOpenChange={setConfirming}
        approval={approval}
        reason={reason}
        onApproved={() => {
          onDone("approve");
        }}
        onNotice={onRefused}
      />
    </form>
  );
}

function Detail({ approval, nextId, notice, onNotice }: { approval: Approval; nextId: string | null; notice: string | null; onNotice: (text: string) => void }) {
  const own = approval.state === "pending" && !canDecide(approval);
  const canAct = useCanAct();
  const [done, setDone] = useState<"approve" | "reject" | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const action = approval.action ?? { kind: "", target: null, amount: null };
  const fullTarget = action.target !== null && Array.from(action.target).length > MAX_TARGET ? action.target : null;
  return (
    <section aria-label="Request detail" className="flex min-w-0 flex-[999_1_32rem] flex-col gap-5 px-6 py-5">
      <p role="status" aria-live="polite" className={notice === null ? "sr-only" : "callout-warning px-4 py-3 text-sm"}>
        {notice}
      </p>
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 ref={heading} tabIndex={-1} className={cn("text-xl font-semibold outline-none", WRAP)}>
            {sentenceOf(approval)}
          </h2>
          <Chip tone={STATE_TONE[approval.state]}>{STATE_LABEL[approval.state]}</Chip>
        </div>
        {fullTarget !== null && <p className={cn("text-[13px] text-muted-foreground", WRAP)}>Full text: {escapeInert(fullTarget)}</p>}
        <p className="text-[13.5px] text-muted-foreground">
          Requested by <span className={WRAP}>{escapeInert(approval.requester)}</span> in run <Link to={`/runs/${approval.runId}`} className="text-link underline">{approval.runLabel}</Link> · {formatStarted(approval.requestedAt)}
        </p>
      </div>

      <div className="flex flex-col gap-1.5 text-[14.5px]">
        <p>If you approve: {consequenceFor(action.kind)}.</p>
        <p>If you reject: the agent is told no and the run goes on without this action.</p>
      </div>

      <p className="text-[14.5px]">
        <span className="font-semibold">Cost: </span>
        {costLine(action, approval.runSpend)}
      </p>

      {done !== null && (
        <div role="status" className="callout-success flex flex-col gap-1.5 p-4">
          <p className="text-[14.5px] font-semibold">{done === "approve" ? "Done. The run continues." : "Done. The agent was told no."}</p>
          {nextId === null ? (
            <p className="text-[13.5px] text-muted-foreground">Nothing else is waiting.</p>
          ) : (
            <Link to={`/approvals/${nextId}`} className="inline-flex min-h-11 items-center self-start text-link underline">
              Next waiting request
            </Link>
          )}
        </div>
      )}

      {approval.state === "pending" ? (
        own ? (
          <p role="status" className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
            You made this request, so someone else has to answer it. Nobody can decide their own request.
          </p>
        ) : !canAct ? (
          <p role="status" className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
            Only a signed-in person can decide an approval. You can read it here.
          </p>
        ) : (
          <DecisionForm key={approval.id} approval={approval} onRefused={onNotice} onDone={setDone} />
        )
      ) : (
        approval.decision !== null && (
          <div role="status" className={cn("flex flex-col gap-1.5 p-4", approval.state === "approved" ? "callout-success" : "callout-danger")}>
            <p className={cn("text-[14.5px] font-semibold", approval.state === "approved" ? "text-link" : "text-danger")}>
              {STATE_LABEL[approval.state]} by {escapeInert(approval.decision.by)} at {formatClock(approval.decision.at)}
            </p>
            <p className="text-[13.5px] text-muted-foreground">A person's reason (not shown here)</p>
            {approval.decision.reasonHash !== null && (
              <p className="font-mono text-xs text-muted-foreground">kept in the content store (hash {approval.decision.reasonHash.slice(0, 12)})</p>
            )}
            {approval.decision.ledgerEntry !== "" && (
              <Link to={entryHref(approval.decision.ledgerEntry)} className="self-start font-mono text-[12.5px] text-link no-underline hover:underline">
                recorded as ledger #{entryLabel(approval.decision.ledgerEntry)} →
              </Link>
            )}
            <p className="text-xs text-foreground/80">Decided requests are read-only.</p>
          </div>
        )
      )}

      <div className="grid grid-cols-[repeat(auto-fit,minmax(15rem,1fr))] gap-3.5">
        <Section label="Evidence">
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {approval.evidence.map((e, i) => (
              <li key={`${String(i)}:${e.text}`} className="flex gap-2 text-[13px]">
                <span aria-hidden="true" className={e.state === "ok" ? "text-link" : "text-warm"}>
                  {e.state === "ok" ? "✓" : "…"}
                </span>
                <span className="text-muted-foreground">
                  <span className="sr-only">{e.state === "ok" ? "Done: " : "Still waiting: "}</span>
                  {escapeInert(e.text)}
                  {i === 0 && approval.taskId !== undefined && approval.taskId !== null && (
                    <>
                      {" "}for task{" "}
                      {approval.runId === "" ? (
                        approval.taskId
                      ) : (
                        <Link to={`/runs/${encodeURIComponent(approval.runId)}/tasks/${encodeURIComponent(approval.taskId)}`} className="inline-flex min-h-11 items-center text-link underline">
                          {approval.taskId}
                        </Link>
                      )}
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </Section>
        <Section label="Policy and context">
          <dl className="m-0 flex flex-col gap-2">
            <Row label="Why you were asked">
              <span className="font-mono text-[12.5px]">{escapeInert(approval.rule)}</span>
            </Row>
            <Row label="Run spend">
              {approval.runSpend === null
                ? "not available"
                : approval.runSpend.limit === null
                  ? `${formatMoney(approval.runSpend.spent)} (no run budget recorded)`
                  : `${formatMoney(approval.runSpend.spent)} of ${formatMoney(approval.runSpend.limit)}`}
            </Row>
            <Row label="Applies to">one action, this run only</Row>
            <Row label="Who can decide">Owner</Row>
          </dl>
        </Section>
      </div>
    </section>
  );
}

/** Approvals (SPEC-app-v1 3.3): the inbox and one request, side by side (stacked on narrow screens). */
export function ApprovalsPage() {
  const api = useApi();
  const { id } = useParams();
  const list = useQuery({
    queryKey: keys.approvals,
    queryFn: () => api.approvals.list(),
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 2,
    refetchInterval: 10_000,
  });
  const items = sortApprovals(list.data ?? []);
  const selectedId = id ?? items[0]?.id;
  // /approvals opens the first request by pinning it in the URL, so a decision (which re-sorts the list) never moves the panel to another request.
  const detail = useQuery({
    queryKey: ["approval", selectedId],
    queryFn: () => api.approvals.get(selectedId ?? ""),
    enabled: selectedId !== undefined,
    retry: (count, error) => !(error instanceof NotFoundError || error instanceof NotAvailableError) && count < 2,
  });
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);
  const pending = items.filter((a) => a.state === "pending").length;
  // The oldest other request still waiting (AE-12); a plain link, never opened or answered for the person.
  const next = items
    .filter((a) => a.state === "pending" && a.id !== selectedId)
    .sort((a, b) => a.requestedAt.localeCompare(b.requestedAt))[0];

  if (id === undefined && selectedId !== undefined) return <Navigate to={`/approvals/${selectedId}`} replace />;

  return (
    <div className="flex flex-col">
      <title>{`${pending > 0 ? `(${String(pending)}) ` : ""}Approvals · NuraMesh`}</title>
      <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 border-b bg-panel px-6 py-4">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-[21px] font-semibold">Approvals</h1>
          <p className="text-[13px] text-muted-foreground">Agents ask here when their policy says a person must decide. Each approval works once, for one action.</p>
        </div>
        <span className="flex-1" />
        <Chip tone="warning" data-testid="pending-count" className="px-3 py-0.5 text-[13px]">
          {pending} waiting
        </Chip>
      </header>
      {list.isError ? (
        <p role="alert" className="p-6 text-sm">
          {list.error instanceof NotAvailableError ? "This data source can't list approvals yet." : `Couldn't load the approvals. ${describeProblem(list.error)}`}{" "}
          <button type="button" className="underline" onClick={() => void list.refetch()}>
            Try again
          </button>
        </p>
      ) : list.data === undefined ? (
        <p role="status" className="p-6 text-sm text-muted-foreground">
          Loading approvals…
        </p>
      ) : items.length === 0 ? (
        <p className="p-6 text-sm text-muted-foreground">Nothing is waiting for you, and nothing was decided recently.</p>
      ) : (
        <div className="flex flex-wrap items-stretch">
          <section aria-label="Requests" className="flex min-w-0 flex-[1_1_20rem] flex-col border-r">
            <h2 className="table-head px-4 py-3">Waiting and recent</h2>
            <ul className="m-0 flex list-none flex-col p-0">
              {items.map((a) => {
                const style = STATE_STYLE[a.state];
                const current = a.id === selectedId;
                return (
                  <li key={a.id}>
                    <Link
                      to={`/approvals/${a.id}`}
                      aria-current={current ? "page" : undefined}
                      className={cn("flex min-h-11 min-w-0 flex-col gap-1.5 border-t border-l-[3px] px-4 py-3.5 text-foreground no-underline hover:bg-panel-2", style.bar, current && "bg-panel ring-1 ring-border-strong ring-inset")}
                    >
                      <span className="flex items-center gap-2">
                        <span className={cn("text-sm font-semibold", WRAP)}>{sentenceOf(a)}</span>
                        <Chip tone={STATE_TONE[a.state]} className="ml-auto">{STATE_LABEL[a.state]}</Chip>
                      </span>
                      <span className={cn("text-[12.5px] text-muted-foreground", WRAP)}>
                        {escapeInert(a.requester)} · run {a.runLabel}
                      </span>
                      <span className="font-mono text-[11.5px] text-small">{formatStarted(a.requestedAt)}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </section>
          {detail.isError ? (
            <section aria-label="Request detail" className="flex-[999_1_32rem] p-6">
              <p role="alert" className="text-sm">
                {detail.error instanceof NotFoundError ? `There is no approval ${selectedId ?? ""}.` : "Couldn't load this request."}{" "}
                <Link to="/approvals" className="text-link underline">
                  Back to the inbox
                </Link>
              </p>
            </section>
          ) : detail.data === undefined ? (
            <p role="status" className="flex-[999_1_32rem] p-6 text-sm text-muted-foreground">
              Loading the request…
            </p>
          ) : (
            <Detail key={detail.data.id} approval={detail.data} nextId={next?.id ?? null} notice={notice?.id === detail.data.id ? notice.text : null} onNotice={(text) => { setNotice({ id: detail.data.id, text }); }} />
          )}
        </div>
      )}
    </div>
  );
}
