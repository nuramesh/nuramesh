import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";

import { NotAvailableError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { type DataClass, isAdmin } from "@/api/models";

/** Query keys of the model screens; any write invalidates the whole `models` family. */
export const modelKeys = {
  all: ["models"] as const,
  session: ["models", "session"] as const,
  catalog: ["models", "catalog"] as const,
  events: ["models", "events"] as const,
  assignments: ["models", "assignments"] as const,
  connections: ["models", "connections"] as const,
  resolution: (dataClass: DataClass) => ["models", "resolution", dataClass] as const,
};

/** Retry a failed read twice, except when retrying cannot help (not available here, or a refusal). */
export const retryRead = (count: number, error: unknown): boolean =>
  !(error instanceof NotAvailableError) && !(error instanceof ProblemError && error.status >= 400 && error.status < 500) && count < 1;

export function useSession() {
  const api = useApi();
  const query = useQuery({ queryKey: modelKeys.session, queryFn: () => api.session.current(), retry: false, staleTime: 60_000 });
  return { ...query, admin: isAdmin(query.data) };
}

export function useCatalog() {
  const api = useApi();
  return useQuery({ queryKey: modelKeys.catalog, queryFn: () => api.models.catalog(), retry: retryRead });
}

export function useAssignments() {
  const api = useApi();
  return useQuery({ queryKey: modelKeys.assignments, queryFn: () => api.models.assignments(), retry: retryRead });
}

/** Admin-only reads run only for an admin, so a viewer never makes a request the server will refuse. */
export function useConnections(enabled: boolean) {
  const api = useApi();
  return useQuery({ queryKey: modelKeys.connections, queryFn: () => api.models.connections(), enabled, retry: retryRead });
}

/** The catalog history, newest page first; `fetchNextPage` loads the next older page (`before = nextBefore`). */
export function useEvents(enabled: boolean) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: modelKeys.events,
    queryFn: ({ pageParam }) => api.models.events(pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.nextBefore ?? undefined,
    enabled,
    retry: retryRead,
  });
}

/** One connection's history, newest page first. */
export function useConnectionHistory(query: { provider: string; endpointId?: string }) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: [...modelKeys.all, "connection-history", query.provider, query.endpointId ?? null] as const,
    queryFn: ({ pageParam }) => api.models.connectionHistory({ provider: query.provider, ...(query.endpointId === undefined ? {} : { endpointId: query.endpointId }), ...(pageParam === undefined ? {} : { before: pageParam }) }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.nextBefore ?? undefined,
    retry: retryRead,
  });
}

export function useResolution(dataClass: DataClass, enabled: boolean) {
  const api = useApi();
  return useQuery({ queryKey: modelKeys.resolution(dataClass), queryFn: () => api.models.resolution(dataClass), enabled, retry: retryRead });
}

/** After any write: every model read is stale (a pause can change a resolution, a repoint a catalog). */
export function useRefreshModels(): () => Promise<void> {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: modelKeys.all });
    await client.invalidateQueries({ queryKey: ["ledger"] });
  };
}
