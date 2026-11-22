import { useState } from "react";
import { Link } from "react-router";

import type { RunEvent } from "@/api/types";
import { useAuth } from "@/auth/use-session";
import { entryHref, entryLabel } from "@/ledger/format";
import { FilterPill } from "@/components/ui/chip";
import { cn } from "@/lib/utils";

import { filterEvents, FILTERS, type TimelineFilter } from "./events";
import { formatClock, TONE_STYLE } from "./format";

/**
 * The run's events, newest first, with filters (All, Gates, Spend, Approvals). A polite live
 * region, so a screen reader hears new events without being interrupted.
 */
export function Timeline({
  runId,
  events,
  loading,
  unavailable,
  mode,
}: {
  runId: string;
  events: RunEvent[];
  loading: boolean;
  unavailable: boolean;
  mode: "live" | "reconnecting" | "unreachable" | "polling";
}) {
  const [filter, setFilter] = useState<TimelineFilter>("all");
  const shown = filterEvents(events, filter);
  // A source with accounts (G7): an unreachable stream may also mean the session ended.
  const hasAccounts = useAuth() !== undefined;
  return (
    <section aria-labelledby="timeline-title" className="min-w-0 flex-[1_1_20rem] rounded-xl border bg-panel border-card-border shadow-card">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h2 id="timeline-title" className="text-sm font-semibold">
          Timeline
        </h2>
        <span role="status" data-testid="timeline-mode" className="font-mono text-[11px] text-small">
          {mode === "live" ? "live" : mode === "reconnecting" ? "connection lost, reconnecting…" : mode === "unreachable" ? `cannot reach the server (check the address and that the server allows this origin${hasAccounts ? ", or sign in again" : ""})` : "updating every 3 s"}
        </span>
        <Link to={`/ledger?q=${encodeURIComponent(`run:${runId}`)}`} className="ml-auto inline-flex min-h-11 items-center text-[12.5px] text-link no-underline hover:underline">
          Open in ledger
        </Link>
      </div>
      <div role="group" aria-label="Filter the timeline" className="flex flex-wrap gap-1.5 px-4 py-2.5">
        {FILTERS.map((f) => (
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
      {unavailable ? (
        <p className="px-4 pb-4 text-sm text-muted-foreground">This data source doesn't stream events yet.</p>
      ) : loading ? (
        <p role="status" className="px-4 pb-4 text-sm text-muted-foreground">
          Loading events…
        </p>
      ) : shown.length === 0 ? (
        <p className="px-4 pb-4 text-sm text-muted-foreground">Nothing here yet.</p>
      ) : (
        <div role="log" aria-live="polite" aria-relevant="additions" aria-label="Events, newest first" className="px-4 pb-4">
          <ol className="m-0 flex list-none flex-col p-0">
          {shown.map((e) => {
            const tone = TONE_STYLE[e.tone];
            return (
              <li key={e.seq} data-seq={e.seq} className="flex gap-3 border-t py-2.5 first:border-t-0">
                <span aria-hidden="true" className={cn("mt-1.5 size-2 shrink-0 rounded-full", tone.dot)} />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <div className="flex items-baseline gap-2">
                    <span className={cn("font-mono text-xs", tone.kind)}>{e.kind}</span>
                    <time dateTime={e.at} className="ml-auto font-mono text-[11.5px] text-small">
                      {formatClock(e.at)}
                    </time>
                  </div>
                  <span className="text-[13px] text-muted-foreground">{e.text}</span>
                  {(e.entry !== undefined || (e.taskId !== undefined && e.taskId !== null)) && (
                    <span className="flex flex-wrap gap-x-3 text-xs">
                      {e.entry !== undefined && (
                        <Link to={entryHref(e.entry)} className="inline-flex min-h-11 items-center font-mono text-link underline">
                          #{entryLabel(e.entry)}
                        </Link>
                      )}
                      {e.taskId !== undefined && e.taskId !== null && (
                        <Link to={`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(e.taskId)}`} className="inline-flex min-h-11 items-center text-link underline">
                          Audit trail
                        </Link>
                      )}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
          </ol>
        </div>
      )}
    </section>
  );
}
