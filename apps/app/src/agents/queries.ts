import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { NotAllowedError } from "@/api/client";
import { useApi } from "@/api/context";

/** Every agent-credential read starts with this key, so one invalidation refreshes them all. */
export const CREDENTIALS = ["agent-credentials"] as const;

/** One agent's credentials. A refusal (not an owner) is an answer, not a failure: `notOwner`. */
export function useAgentCredentials(agent: string) {
  const api = useApi();
  const query = useQuery({
    queryKey: [...CREDENTIALS, "agent", agent],
    queryFn: () => api.agentCredentials.list({ agent }),
    retry: false,
  });
  return { ...query, notOwner: query.error instanceof NotAllowedError };
}

/** The organization's live credentials whose issuer was erased (the owners' banner). Calls only the list route. */
export function useErasedIssuerCredentials() {
  const api = useApi();
  return useQuery({
    queryKey: [...CREDENTIALS, "issuer-erased"],
    queryFn: () => api.agentCredentials.list({ issuerErased: true }),
    retry: false,
  });
}

/** The signed-in person's role; `null` when the app cannot tell (the server then decides). */
export function useCallerRole() {
  const api = useApi();
  return useQuery({ queryKey: ["caller-role"], queryFn: () => api.agentCredentials.callerRole(), retry: false });
}

/** The team's agents (owner only). Not asked for anyone known not to be an owner. */
export function useAgentList(enabled: boolean) {
  const api = useApi();
  const query = useQuery({ queryKey: [...CREDENTIALS, "agents"], queryFn: () => api.agentCredentials.agents(), retry: false, enabled });
  return { ...query, notOwner: query.error instanceof NotAllowedError };
}

export function useRefreshCredentials(): () => Promise<void> {
  const queryClient = useQueryClient();
  return useCallback(() => queryClient.invalidateQueries({ queryKey: CREDENTIALS }), [queryClient]);
}
