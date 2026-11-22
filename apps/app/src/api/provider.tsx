import { type ReactNode, useEffect, useState } from "react";

import type { ApiClient } from "./client";
import { ApiContext } from "./context";
import { createApiClient } from "./index";

/** Gives every screen the app's API client (loaded once). `client` lets tests pass their own. */
export function ApiProvider({ children, client }: { children: ReactNode; client?: ApiClient }) {
  const [loaded, setLoaded] = useState<ApiClient | null>(client ?? null);
  const [failure, setFailure] = useState<Error | null>(null);
  useEffect(() => {
    if (client !== undefined) return;
    let live = true;
    createApiClient().then(
      (made) => {
        if (live) setLoaded(made);
      },
      (error: unknown) => {
        if (live) setFailure(error instanceof Error ? error : new Error(String(error)));
      },
    );
    return () => {
      live = false;
    };
  }, [client]);
  if (failure !== null) throw failure; // the error boundary shows it
  if (loaded === null) {
    return (
      <p role="status" className="p-8 text-sm text-muted-foreground">
        Loading NuraMesh…
      </p>
    );
  }
  return <ApiContext value={loaded}>{children}</ApiContext>;
}
