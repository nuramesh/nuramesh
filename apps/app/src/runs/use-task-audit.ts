import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { useApi } from "@/api/context";
import type { TaskAudit } from "@/api/types";

import { lastSeq } from "./events";
import { FINAL_TASK_STATUSES } from "./task-audit-words";
import { type RunEvents, useRunEvents } from "./use-run";

/** Wait this long after a trigger so a burst of entries makes one request. */
export const DEBOUNCE_MS = 300;
/** Never two refetches closer than this (R11, O-m7). */
export const MIN_GAP_MS = 2_000;
const POLL_MS = 3_000;

export const auditKey = (runId: string, taskId: string) => ["run", runId, "task", taskId, "audit"] as const;

export interface TaskAuditState {
  query: ReturnType<typeof useQuery<TaskAudit>>;
  stream: RunEvents;
  /** True while the page still asks for updates (the task can change and the run has not ended). */
  live: boolean;
  runEnded: boolean;
}

/**
 * The audit of one task, kept fresh. The trigger is `useRunEvents` (its one socket, nothing of our own):
 * an entry of this task, or a run-level entry, refetches after 300 ms, never twice within 2 s, and a
 * reconnect refetches once. The page polls every 3 s only while the stream is not live. When the task
 * is final or the run has ended there is one last refetch and then nothing.
 */
export function useTaskAudit(runId: string, taskId: string): TaskAuditState {
  const api = useApi();
  const queryClient = useQueryClient();
  const key = auditKey(runId, taskId);
  const query = useQuery({
    queryKey: key,
    queryFn: () => api.runs.taskAudit(runId, taskId),
    retry: false,
    staleTime: Infinity,
    refetchOnMount: "always",
  });
  const data = query.data;
  const final = data !== undefined && FINAL_TASK_STATUSES.has(data.task.status);

  // The run ending is seen as an event; the stream is only opened for an audit that can still change.
  const [runEnded, setRunEnded] = useState(false);
  const active = data !== undefined && !final && !runEnded;
  const stream = useRunEvents(runId, active, { invalidate: false });
  if (!runEnded && stream.events.some((e) => e.kind === "run.ended")) setRunEnded(true); // sticky: events are kept when the stream stops
  const live = data !== undefined && !final && !runEnded;

  // The throttle: one pending timer, and the time of the last refetch it started.
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const lastRun = useRef<number | null>(null);
  const finalDone = useRef(false);
  const liveRef = useRef(live);
  useEffect(() => {
    liveRef.current = live;
  }, [live]);
  const schedule = useCallback(() => {
    if (timer.current !== undefined) return;
    if (!liveRef.current && finalDone.current) return;
    const wait = Math.max(DEBOUNCE_MS, lastRun.current === null ? 0 : lastRun.current + MIN_GAP_MS - Date.now());
    timer.current = setTimeout(() => {
      timer.current = undefined;
      lastRun.current = Date.now();
      void queryClient.refetchQueries({ queryKey: key, exact: true });
    }, wait);
  }, [queryClient, key[1], key[3]]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(
    () => () => {
      if (timer.current !== undefined) clearTimeout(timer.current);
      timer.current = undefined;
    },
    [],
  );

  // New entries of this task (or of the run) that arrive after the history was loaded.
  const baseline = useRef<number | null>(null);
  useEffect(() => {
    if (stream.loading) return;
    const max = lastSeq(stream.events);
    if (baseline.current === null) {
      baseline.current = max;
      return;
    }
    const seen = baseline.current;
    baseline.current = Math.max(seen, max);
    if (stream.events.some((e) => e.seq > seen && (e.taskId === undefined || e.taskId === null || e.taskId === taskId))) schedule();
  }, [stream.events, stream.loading, taskId, schedule]);

  // A reconnect: the stream may have dropped events.
  const previous = useRef(stream.mode);
  useEffect(() => {
    const was = previous.current;
    previous.current = stream.mode;
    if (stream.mode === "live" && (was === "reconnecting" || was === "unreachable")) schedule();
  }, [stream.mode, schedule]);

  // The poll, only while the stream is not live.
  useEffect(() => {
    if (!live || stream.mode === "live") return;
    const id = setInterval(schedule, POLL_MS);
    return () => {
      clearInterval(id);
    };
  }, [live, stream.mode, schedule]);

  // Becoming final: one last refetch (after the first load, so a final task of an ended run is fetched once).
  const wasLive = useRef(false);
  useEffect(() => {
    if (live) {
      wasLive.current = true;
      return;
    }
    if (wasLive.current && !finalDone.current) {
      schedule();
      finalDone.current = true;
    }
  }, [live, schedule]);

  return { query, stream, live, runEnded };
}

export type { RunEvents };
