import { useEffect, useId, useState } from "react";

import type { BreakerRow, BreakersView } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Chip, type ChipTone } from "@/components/ui/chip";
import { formatClock } from "@/lib/time";

import { DisableDialog } from "./disable-dialog";
import { EnableDialog } from "./enable-dialog";
import { REASON_LABELS, actionLabel, localTime, observedText, relativeAgo, signalLabel, windowLabel } from "./format";
import { ResetDialog } from "./reset-dialog";
import { useBreakers } from "./use-org-safety";

/** The current time in ms, refreshed on an interval, so "Checked 4 seconds ago" does not freeze between reads. */
function useNowMs(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(timer);
    };
  }, [intervalMs]);
  return now;
}

const STATE_CHIP: Record<BreakerRow["state"], { tone: ChipTone; label: string }> = {
  ok: { tone: "success", label: "OK" },
  disabled: { tone: "neutral", label: "Disabled" },
  tripped: { tone: "danger", label: "Tripped" },
};

function trippedCell(row: BreakerRow): string {
  if (row.state === "tripped") {
    const by = row.trippedBy ?? "unknown";
    return row.manual || row.trippedAt === null ? `Manual trip by ${by}` : `${localTime(row.trippedAt)} by ${by}`;
  }
  if (row.state === "disabled") {
    const reason = row.disabledReason === null ? "" : `: ${REASON_LABELS[row.disabledReason]}`;
    return `Disabled ${row.disabledAt === null ? "at an unknown time" : localTime(row.disabledAt)} by ${row.disabledBy ?? "unknown"}${reason}`;
  }
  return "—";
}

const HEADERS = ["Rule", "Signal", "Action", "Window", "Observed / limit", "State", "Tripped"];

type Pending = { kind: "reset" | "disable" | "enable"; name: string } | null;

function Warning({ children }: { children: React.ReactNode }) {
  return <p className="callout-warning mx-4 mt-3 px-3 py-2 text-[13px]">{children}</p>;
}

function Evaluation({ view }: { view: BreakersView }) {
  const e = view.evaluation;
  if (e.state === "failing") {
    const since = e.lastErrorAt === null ? "an unknown time" : localTime(e.lastErrorAt);
    const where = e.lastFailingRule === null ? "" : ` in rule ${e.lastFailingRule}`;
    const halting = view.breakers.some((b) => !b.dryRun);
    return (
      <Warning>
        {`Breaker checks are failing since ${since} (${e.lastErrorClass ?? "an error"}${where}).`}
        {halting ? ` After ${String(e.haltAfterFailedTicks)} failed checks the organization is halted automatically.` : ""}
      </Warning>
    );
  }
  if (e.state === "stopped") return <Warning>Breaker checks are not running on this server.</Warning>;
  return null;
}

/**
 * The circuit breakers (E19, spec/e19/README.md section 12): one row per rule with its live count,
 * state in words, and, for a person only, Reset (tripped), Disable (ok) and Enable (disabled). It
 * reads every 10 s and keeps the last good table when a read fails.
 */
export function BreakersPanel() {
  const query = useBreakers();
  const view = query.data;
  const headingId = useId();
  const now = useNowMs(5_000);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const done = (message: string | undefined): void => {
    setNotice(message ?? null);
  };
  const close = (open: boolean): void => {
    if (!open) setPending(null);
  };
  const ruleFor = (kind: NonNullable<Pending>["kind"]): BreakerRow | null =>
    pending?.kind === kind ? (view?.breakers.find((b) => b.name === pending.name) ?? null) : null;

  const canAct = view?.halt.canAct === true;
  const disabled = view?.breakers.filter((b) => b.state === "disabled").length ?? 0;

  let content;
  if (view === undefined) {
    content = query.isError ? (
      <div className="flex flex-wrap items-center gap-3 px-4 py-4">
        <p className="text-sm">Could not load the circuit breakers.</p>
        <Button
          variant="outline"
          className="min-h-11"
          onClick={() => {
            void query.refetch();
          }}
        >
          Try again
        </Button>
      </div>
    ) : (
      <div className="flex flex-col gap-2 px-4 py-4">
        <p role="status" className="sr-only">
          Loading breakers
        </p>
        {[0, 1, 2].map((i) => (
          <div key={i} aria-hidden="true" className="h-9 animate-pulse rounded-md bg-track" />
        ))}
      </div>
    );
  } else if (view.evaluation.state === "not_configured" || view.breakers.length === 0) {
    content = <p className="px-4 py-4 text-sm text-muted-foreground">No circuit breakers are set. Add a breakers: section to your organization config to stop work automatically when spend or bad behaviour passes a limit.</p>;
  } else {
    content = (
      // A scroll area must take focus so the keyboard can scroll it on a narrow screen (spec/e19 section 12).
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      <div role="region" aria-label="Circuit breakers table" tabIndex={0} className="mt-3 overflow-x-auto border-t focus-visible:outline-2 focus-visible:outline-ring">
        <table className="w-full min-w-[60rem] border-collapse text-[13.5px]">
          <caption className="sr-only">Circuit breakers</caption>
          <thead>
            <tr className="table-head">
              {[...HEADERS, ...(canAct ? ["Actions"] : [])].map((h) => (
                <th key={h} scope="col" className="px-3 py-2.5 text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.breakers.map((b) => {
              const chip = STATE_CHIP[b.state];
              const label = b.state === "tripped" ? "Reset" : b.state === "disabled" ? "Enable" : "Disable";
              const kind = b.state === "tripped" ? "reset" : b.state === "disabled" ? "enable" : "disable";
              return (
                <tr key={b.name} data-state={b.state} className="border-t align-top">
                  <th scope="row" className="px-3 py-3 text-left font-medium">
                    <span className="break-all">{b.name}</span>
                    {b.dryRun && (
                      <>
                        {" "}
                        <Chip tone="info">Dry run</Chip>
                      </>
                    )}
                  </th>
                  <td className="px-3 py-3">{signalLabel(b.signal, b.role) || "—"}</td>
                  <td className="px-3 py-3">{actionLabel(b.action, b.role) || "—"}</td>
                  <td className="px-3 py-3">{b.windowSeconds === null ? "—" : windowLabel(b.windowSeconds)}</td>
                  <td className="px-3 py-3 font-mono text-xs">{observedText(b) || "—"}</td>
                  <td className="px-3 py-3">
                    <Chip tone={chip.tone}>{b.state === "tripped" && !b.inConfig ? "Tripped, removed from config" : chip.label}</Chip>
                  </td>
                  <td className="px-3 py-3 text-xs text-muted-foreground">{trippedCell(b)}</td>
                  {canAct && (
                    <td className="px-3 py-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        className="min-h-11"
                        aria-label={`${label} breaker ${b.name}`}
                        onClick={() => {
                          setPending({ kind, name: b.name });
                        }}
                      >
                        {label}
                      </Button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <section aria-labelledby={headingId} className="min-w-0 overflow-hidden rounded-xl border bg-panel border-card-border shadow-card">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b px-4 py-3">
        <h2 id={headingId} className="text-sm font-semibold">
          Circuit breakers
        </h2>
        {view !== undefined && (
          <p className="text-xs text-muted-foreground">
            {view.evaluation.lastEvaluatedAt === null ? "Not checked yet" : `Checked ${relativeAgo(view.evaluation.lastEvaluatedAt, now)} ago`}
          </p>
        )}
        {view !== undefined && query.isError && (
          <p className="text-xs text-muted-foreground">Breakers may be out of date (last read {formatClock(new Date(query.dataUpdatedAt).toISOString())})</p>
        )}
      </div>
      {view !== undefined && disabled > 0 && (
        <p className="px-4 pt-3 text-[13px]">{disabled === 1 ? "1 breaker is disabled." : `${String(disabled)} breakers are disabled.`}</p>
      )}
      {view !== undefined && !canAct && view.breakers.length > 0 && (
        <p className="px-4 pt-3 text-[13px] text-muted-foreground">Only a signed-in person can reset, disable or enable a breaker.</p>
      )}
      {notice !== null && (
        <p role="status" className="callout-success mx-4 mt-3 px-3 py-2 text-[13px]">
          {notice}
        </p>
      )}
      {view !== undefined && <Evaluation view={view} />}
      {content}
      <ResetDialog rule={ruleFor("reset")} halt={view?.halt ?? null} onOpenChange={close} onDone={done} />
      <DisableDialog rule={ruleFor("disable")} onOpenChange={close} onDone={done} />
      <EnableDialog rule={ruleFor("enable")} onOpenChange={close} onDone={done} />
    </section>
  );
}
