import { render } from "@testing-library/react";
import type { ReactNode } from "react";

import type { ApiClient } from "@/api/client";
import { createFixtureClient, type OrgSafetyScenario } from "@/api/fixture-client";
import { Providers } from "@/app/providers";

/** Renders `ui` under the app's providers with the given client (default: a fresh fixture client). */
export function renderSafety(ui: ReactNode, client: ApiClient = createFixtureClient()): ApiClient {
  render(<Providers client={client}>{ui}</Providers>);
  return client;
}

export const fixture = (scenario: OrgSafetyScenario): ApiClient => createFixtureClient({ orgSafety: scenario });

/** A promise whose settling the test controls (to see a "pending" state). */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
