import { Link } from "react-router";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";

import { COPY, STEP_LABELS } from "./copy";
import type { Step } from "./derive";
import { useFirstRun } from "./use-first-run";

const LINK = "text-sm font-medium text-link underline underline-offset-2";

/** `/start` (FR-2): the four steps, live from the server. Anyone FR-1 excludes sees only a way to the Builder. */
export function FirstRunPage() {
  const fr = useFirstRun({ page: true });
  const [announce, setAnnounce] = useState("");
  const seen = useRef<Map<string, string>>(new Map());

  // A step that becomes done is said once, politely (C36).
  useEffect(() => {
    for (const s of fr.steps) {
      const before = seen.current.get(s.id);
      if (before !== undefined && before !== "done" && s.status === "done") {
        setAnnounce(`${STEP_LABELS[s.id]}: done`);
      }
      seen.current.set(s.id, s.status);
    }
  }, [fr.steps]);

  if (!fr.visible) {
    return (
      <section aria-labelledby="page-title" className="flex max-w-2xl flex-col gap-4 p-6">
        <title>Get started · NuraMesh</title>
        <h1 id="page-title" className="text-2xl font-semibold">
          {COPY.title}
        </h1>
        <p>{COPY.nothingToSetUp}</p>
        <Link to="/builder" className={LINK}>
          {COPY.openBuilder}
        </Link>
      </section>
    );
  }

  return (
    <section aria-labelledby="page-title" className="flex max-w-2xl flex-col gap-6 p-6">
      <title>Get started · NuraMesh</title>
      <h1 id="page-title" className="text-2xl font-semibold">
        {COPY.title}
      </h1>
      <p role="status" aria-live="polite" data-testid="first-run-live" className="sr-only">
        {announce}
      </p>
      {fr.complete && (
        <div className="flex flex-col gap-2 rounded-lg border bg-panel px-4 py-3">
          <h2 className="text-lg font-semibold">{COPY.complete}</h2>
          <nav aria-label="Next" className="flex flex-wrap gap-4">
            <Link to="/ledger" className={LINK}>
              {COPY.ledger}
            </Link>
            <Link to="/builder" className={LINK}>
              {COPY.builder}
            </Link>
            <Link to="/approvals" className={LINK}>
              {COPY.approvals}
            </Link>
          </nav>
        </div>
      )}
      <ol aria-label={COPY.steps} className="flex flex-col gap-3">
        {fr.steps.map((s) => (
          <StepItem key={s.id} step={s} onRetry={fr.retry} onSkip={fr.skipInvite} />
        ))}
      </ol>
      {!fr.prefs.dismissed && (
        <div>
          <Button variant="outline" className="min-h-11" onClick={fr.dismiss}>
            {COPY.hide}
          </Button>
        </div>
      )}
    </section>
  );
}

function StepItem({ step, onRetry, onSkip }: { step: Step; onRetry: () => void; onSkip: () => void }) {
  const status =
    step.status === "done" ? COPY.done : step.status === "skipped" ? COPY.skipped : COPY.notDone;
  return (
    <li
      data-step={step.id}
      data-testid={`step-${step.id}`}
      className="flex min-h-11 flex-col gap-1 rounded-lg border bg-panel px-4 py-3"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">{STEP_LABELS[step.id]}</span>
        <span className="text-[13px] text-muted-foreground">{status}</span>
      </div>
      {step.status === "unknown" && (
        <p className="flex flex-wrap items-center gap-2 text-sm">
          {COPY.couldNotCheck}
          <Button variant="outline" size="sm" className="min-h-11" onClick={onRetry}>
            {COPY.retry}
          </Button>
        </p>
      )}
      {step.status !== "done" && step.status !== "skipped" && <StepAction step={step} onSkip={onSkip} />}
    </li>
  );
}

function StepAction({ step, onSkip }: { step: Step; onSkip: () => void }) {
  if (step.id === "run") {
    return (
      <p className="text-sm text-muted-foreground">
        {COPY.cliIntro} <code className="font-mono">{COPY.cliLine}</code>
      </p>
    );
  }
  if (step.id === "approve") {
    return (
      <div className="flex flex-col gap-1 text-sm">
        <p className="text-muted-foreground">{COPY.approvalWhat}</p>
        {step.note === "no-approval-asked" && <p>{COPY.noApprovalAsked}</p>}
        <Link to="/approvals" className={LINK}>
          {COPY.openApprovals}
        </Link>
      </div>
    );
  }
  if (step.id === "invite") {
    return (
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <p className="text-muted-foreground">{COPY.invitesNotYet}</p>
        <Button variant="outline" size="sm" className="min-h-11" onClick={onSkip}>
          {COPY.skip}
        </Button>
      </div>
    );
  }
  return null;
}
