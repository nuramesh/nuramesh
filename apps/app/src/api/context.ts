import { createContext, use } from "react";

import type { ApiClient } from "./client";

export const ApiContext = createContext<ApiClient | null>(null);

/** The API client. Only works under `ApiProvider`. */
export function useApi(): ApiClient {
  const client = use(ApiContext);
  if (client === null) throw new Error("useApi needs an <ApiProvider>");
  return client;
}

/** The API client, or `null` outside an `ApiProvider` (the builder works without one, minus presets and runs). */
export function useOptionalApi(): ApiClient | null {
  return use(ApiContext);
}
