import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { ApiError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";

/** How often the status bar and the breakers panel read (the organization safety notes section 12). */
export const STATUS_POLL_MS = 5_000;
export const BREAKERS_POLL_MS = 10_000;

/**
 * The halt state and role pauses, every 5 s and when the window comes back to the front. A failed
 * poll keeps the last good data (react-query keeps `data` and sets `isError`), which is what the
 * "may be out of date" notice is for. No retries: the next poll is the retry.
 */
export function useOrgStatus() {
  const api = useApi();
  return useQuery({
    queryKey: keys.orgStatus,
    queryFn: () => api.org.status(),
    refetchInterval: STATUS_POLL_MS,
    refetchOnWindowFocus: true,
    staleTime: 0,
    retry: false,
  });
}

/** The breakers, every 10 s and on focus; `enabled: false` reads nothing (the resume dialog turns it on when open). */
export function useBreakers(enabled = true) {
  const api = useApi();
  return useQuery({
    queryKey: keys.breakers,
    queryFn: () => api.breakers.list(),
    refetchInterval: BREAKERS_POLL_MS,
    refetchOnWindowFocus: true,
    staleTime: 0,
    retry: false,
    enabled,
  });
}

/**
 * May the signed-in caller take an action (halt, resume, pause, reset, unblock, decide, stop)?
 * The server's `can_act` answer; unknown (not read yet, or the read failed) counts as no, so a
 * person who cannot act never sees an action button flash up (default deny, ND106). The server
 * still refuses the call itself; this only hides what would fail.
 */
export function useCanAct(): boolean {
  return useOrgStatus().data?.canAct === true;
}

/** Re-reads everything an action can change (the status bar, the breakers, the ledger); used after any action, a 409 included. */
export function useRefetchSafety(): () => Promise<void> {
  const queryClient = useQueryClient();
  return useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: keys.orgStatus }),
      queryClient.invalidateQueries({ queryKey: keys.breakers }),
      queryClient.invalidateQueries({ queryKey: keys.ledger }),
    ]);
  }, [queryClient]);
}

/** A problem as one short phrase for a dialog: the server's problem title, else the error's own text. */
export function problemText(error: unknown): string {
  if (error instanceof ProblemError) return error.title;
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "Something went wrong";
}
