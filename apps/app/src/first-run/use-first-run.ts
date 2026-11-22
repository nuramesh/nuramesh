/**
 * The data hook of the first-run checklist (spec/first-run/checklist.md FR-1, FR-4, FR-11, FR-12).
 * It reads, it never decides: three bounded reads, polled every 5 seconds while the page shows, and one
 * browser-local preference per (organization, person).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useSyncExternalStore } from "react";

import type { FirstRunReads } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { useAuthState } from "@/auth/use-session";

import {
  type Answers,
  checklistVisible,
  deriveSteps,
  doneCount,
  isComplete,
  parseApprovals,
  parseInvites,
  parseRuns,
  type Step,
} from "./derive";
import { DEFAULT_PREFS, type FirstRunPrefs, firstRunKey, readPrefs, writePrefs } from "./storage";

export const POLL_MS = 5000;

const listeners = new Set<() => void>();
/** Choices that could not be saved (blocked storage) last until reload (FR-11). */
const memory = new Map<string, string>();
const notify = (): void => {
  for (const l of listeners) l();
};
const subscribe = (l: () => void): (() => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

function snapshot(org: string, user: string): string {
  return memory.get(firstRunKey(org, user)) ?? JSON.stringify(readPrefs(org, user));
}

function save(org: string, user: string, prefs: FirstRunPrefs): void {
  writePrefs(org, user, prefs);
  const text = JSON.stringify(prefs);
  if (JSON.stringify(readPrefs(org, user)) === text) memory.delete(firstRunKey(org, user));
  else memory.set(firstRunKey(org, user), text);
  notify();
}

type Read = "runs" | "approvals" | "invites";

/** One bounded read; a failure or a bad body is an unknown answer, never a thrown page (FR-4). */
function useRead<T>(
  name: Read,
  parse: (body: unknown) => T,
  reads: FirstRunReads | undefined,
  o: { org: string; user: string; enabled: boolean; poll: () => number | false },
) {
  return useQuery({
    queryKey: keys.firstRun(o.org, o.user, name),
    queryFn: async (): Promise<T | { ok: false }> => {
      try {
        if (reads === undefined) return { ok: false };
        return parse(await reads[name]());
      } catch {
        return { ok: false };
      }
    },
    enabled: o.enabled && reads !== undefined,
    retry: false,
    refetchInterval: o.poll,
  });
}

export interface FirstRun {
  /** FR-1: a signed-in owner on the server source. */
  visible: boolean;
  prefs: FirstRunPrefs;
  /** True until every read has answered once (or failed). */
  loading: boolean;
  steps: Step[];
  done: number;
  complete: boolean;
  /** Shown in the sidebar and used by the index redirect: visible, not dismissed, not complete. */
  active: boolean;
  retry: () => void;
  dismiss: () => void;
  show: () => void;
  skipInvite: () => void;
}

/** `page`: the /start page polls even when dismissed; the sidebar polls only while the checklist is active. */
export function useFirstRun(options: { page?: boolean } = {}): FirstRun {
  const page = options.page === true;
  const api = useApi();
  const auth = useAuthState();
  const visible = checklistVisible(auth, api.source === "server" ? "server" : "sample") && api.firstRun !== undefined;
  const org = auth.kind === "signed-in" ? auth.user.orgId : "";
  const user = auth.kind === "signed-in" ? auth.user.id : "";
  const raw = useSyncExternalStore(subscribe, () => snapshot(org, user));
  const prefs = useMemo<FirstRunPrefs>(() => {
    try {
      return { ...DEFAULT_PREFS, ...(JSON.parse(raw) as FirstRunPrefs) };
    } catch {
      return { ...DEFAULT_PREFS };
    }
  }, [raw]);

  const enabled = visible && (page || !prefs.dismissed);
  // The sidebar stops polling once the checklist is complete; the page keeps showing live truth.
  const queryClient = useQueryClient();
  const poll = (): number | false => {
    if (page) return POLL_MS;
    const get = (name: Read): unknown => queryClient.getQueryData(keys.firstRun(org, user, name));
    const [r, a, i] = [get("runs"), get("approvals"), get("invites")];
    if (r === undefined || a === undefined || i === undefined) return POLL_MS;
    return isComplete(deriveSteps({ runs: r, approvals: a, invites: i } as Answers, prefs)) ? false : POLL_MS;
  };
  const runsQ = useRead("runs", parseRuns, api.firstRun, { org, user, enabled, poll });
  const approvalsQ = useRead("approvals", parseApprovals, api.firstRun, { org, user, enabled, poll });
  const invitesQ = useRead("invites", parseInvites, api.firstRun, { org, user, enabled, poll });

  const answers: Answers = {
    runs: runsQ.data ?? { ok: false },
    approvals: approvalsQ.data ?? { ok: false },
    invites: invitesQ.data ?? { ok: false },
  };
  const loading = enabled && (runsQ.data === undefined || approvalsQ.data === undefined || invitesQ.data === undefined);
  const steps = useMemo(() => deriveSteps(answers, prefs), [answers.runs, answers.approvals, answers.invites, prefs]); // eslint-disable-line react-hooks/exhaustive-deps
  const complete = !loading && isComplete(steps);
  const retry = useCallback((): void => {
    void runsQ.refetch();
    void approvalsQ.refetch();
    void invitesQ.refetch();
  }, [runsQ, approvalsQ, invitesQ]);

  return {
    visible,
    prefs,
    loading,
    steps,
    done: doneCount(steps),
    complete,
    active: visible && !prefs.dismissed && !complete,
    retry,
    dismiss: () => {
      save(org, user, { ...prefs, dismissed: true });
    },
    show: () => {
      save(org, user, { ...prefs, dismissed: false });
    },
    skipInvite: () => {
      save(org, user, { ...prefs, invite_skipped: true });
    },
  };
}
