import { QueryClient } from "@tanstack/react-query";

import { NotAvailableError } from "@/api/client";

/** Server-state cache defaults: one retry, no refetch storms when the window regains focus. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // one retry, but not for "this source can't do that": waiting won't change the answer
        retry: (count, error) => !(error instanceof NotAvailableError) && count < 1,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: 0 },
    },
  });
}
