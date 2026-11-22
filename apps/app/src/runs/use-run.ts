import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { NotAvailableError, NotFoundError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import type { LiveStatus, RunEvent, RunStatus } from "@/api/types";

import { isLive } from "./format";
import { mergeEvents } from "./events";

const POLL_MS = 3_000;

/** Every run (optionally one status), most recently active first, refreshed while any is still going. */
export function useRuns(status?: RunStatus) {
  const api = useApi();
  return useQuery({
    queryKey: ["runs", "page", status ?? "all"],
    queryFn: () => api.runs.listPage(status === undefined ? undefined : { status }),
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 2,
    refetchInterval: (query) => (query.state.data?.runs.some((r) => isLive(r)) === true ? 5_000 : false),
  });
}

export const runKey = (id: string) => ["run", id] as const;

/** One run, refreshed every few seconds while it can still change. */
export function useRun(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: runKey(id),
    queryFn: () => api.runs.get(id),
    // A run that isn't there, or a source that can't say, won't change by asking again.
    retry: (count, error) => !(error instanceof NotFoundError || error instanceof NotAvailableError) && count < 2,
    refetchInterval: (query) => (query.state.data !== undefined && isLive(query.state.data) ? POLL_MS : false),
  });
}

export interface RunEvents {
  /** Newest first. */
  events: RunEvent[];
  loading: boolean;
  /** Set when the data source can't give events yet (the server client before /v1/events is wired). */
  unavailable: boolean;
  /** "live" while the stream delivers; "reconnecting" while it is being re-opened; "polling" otherwise. */
  mode: "live" | "reconnecting" | "unreachable" | "polling";
}

/**
 * The run's events: the history first, then new ones as they happen. Events come from the live
 * stream when the client has one and from polling `events(id, afterSeq)` every few seconds
 * regardless (the stream may drop events; the cursor catches up). **Only a poll moves the cursor**:
 * a stream event after a dropped one must not skip past the gap, so the next poll still asks for
 * everything after the last event it fetched itself. The same event arriving twice is shown once
 * (merged by `seq`). A new event also refreshes the run's numbers and the runs list.
 *
 * `invalidate: false` leaves the run's queries alone (a page that refetches by its own rules).
 *
 * Polling and the stream run only while `active`; becoming inactive (the run finished) keeps what
 * is on screen, and a new `id` starts over.
 */
export function useRunEvents(id: string, active: boolean, options: { invalidate?: boolean } = {}): RunEvents {
  const invalidate = options.invalidate ?? true;
  const api = useApi();
  const queryClient = useQueryClient();
  const [state, setState] = useState<{ events: RunEvent[]; loading: boolean; unavailable: boolean; live: boolean }>({
    events: [],
    loading: true,
    unavailable: false,
    live: false,
  });
  const [connection, setConnection] = useState<LiveStatus | null>(null);
  const liveNow = useRef(false);
  const cursor = useRef(0);
  const stopped = useRef(false);

  // History: from the start whenever the run changes.
  useEffect(() => {
    stopped.current = false;
    cursor.current = 0;
    let current = true;
    api.runs.events(id, 0).then(
      (page) => {
        if (stopped.current || !current) return;
        cursor.current = Math.max(cursor.current, page.nextSeq);
        setState((s) => ({ ...s, events: mergeEvents(s.events, page.events), loading: false }));
      },
      (error: unknown) => {
        if (stopped.current || !current) return;
        setState((s) => ({ ...s, loading: false, unavailable: error instanceof NotAvailableError }));
      },
    );
    return () => {
      current = false;
      stopped.current = true;
    };
  }, [api, id]);

  // Updates: the stream and a poll, only while the run can still change. When it stops being
  // active (it finished), one last poll fetches whatever happened since the previous one.
  useEffect(() => {
    if (!active) return;
    let live = true;
    // One refresh for a burst of entries (a replay delivers hundreds at once), not one per entry.
    let pending: ReturnType<typeof setTimeout> | undefined;
    const refresh = (): void => {
      if (pending !== undefined) return;
      if (!invalidate) return; // the task audit page refetches on its own schedule (2 s apart at most)
      pending = setTimeout(() => {
        pending = undefined;
        void queryClient.invalidateQueries({ queryKey: runKey(id) });
        void queryClient.invalidateQueries({ queryKey: keys.runs });
      }, 300);
    };
    const poll = (): void => {
      if (liveNow.current && live) return; // the stream is delivering; polling would only repeat it
      api.runs.events(id, cursor.current).then(
        (page) => {
          if (stopped.current) return;
          const before = cursor.current;
          cursor.current = Math.max(cursor.current, page.nextSeq);
          if (page.events.length > 0) {
            setState((s) => ({ ...s, events: mergeEvents(s.events, page.events) }));
            if (before > 0 && live) refresh();
          }
        },
        () => undefined, // the next poll tries again; the history load reports "unavailable"
      );
    };
    const stop = api.runs.subscribe(
      id,
      (event) => {
        if (stopped.current || !live) return;
        setState((s) => ({ ...s, events: mergeEvents(s.events, [event]), live: true }));
        refresh();
      },
      {
        afterSeq: cursor.current,
        onStatus: (status) => {
          if (stopped.current || !live) return;
          liveNow.current = status === "live";
          setConnection(status);
        },
      },
    );
    const timer = setInterval(poll, POLL_MS);
    return () => {
      live = false;
      liveNow.current = false;
      if (pending !== undefined) clearTimeout(pending);
      stop();
      clearInterval(timer);
      poll(); // the final catch-up: events since the last poll are not lost when the run ends
    };
  }, [api, id, active, queryClient, invalidate]);

  const mode: RunEvents["mode"] =
    connection === "unreachable" ? "unreachable" : connection === "reconnecting" ? "reconnecting" : connection === "live" || (connection === null && state.live) ? "live" : "polling";
  return { events: state.events, loading: state.loading, unavailable: state.unavailable, mode };
}
