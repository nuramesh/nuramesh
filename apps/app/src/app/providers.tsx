import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";

import type { ApiClient } from "@/api/client";
import { ApiProvider } from "@/api/provider";
import { ErrorBoundary } from "@/components/error-boundary";
import { createQueryClient } from "@/lib/query-client";
import { watchSystemTheme } from "@/lib/theme";

/** App-wide providers: error boundary, data cache, API client and theme sync. `client` is for tests. */
export function Providers({ children, client }: { children: ReactNode; client?: ApiClient }) {
  const [queryClient] = useState(createQueryClient);
  useEffect(() => watchSystemTheme(), []);
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ApiProvider {...(client ? { client } : {})}>{children}</ApiProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
