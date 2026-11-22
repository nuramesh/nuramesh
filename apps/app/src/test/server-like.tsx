import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { runDetail } from "@/api/server/map";
import type { RawRunDetail } from "@/api/server/raw";
import type { RunDetail } from "@/api/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

import { rawDone, rawNeedsHuman } from "./contract";
import { createUnavailableClient } from "./unavailable-client";

/** A client that says it is the server's and answers only what a test gives it; everything else is "not available". */
export function serverLike(parts: { [K in keyof ApiClient]?: Partial<ApiClient[K]> }): ApiClient {
  const base = createUnavailableClient();
  // A server whose caller may act (`can_act`): the action buttons show only after the status says so.
  const merged: Record<string, unknown> = { ...base, org: { ...base.org, status: () => createFixtureClient().org.status() } };
  for (const [name, value] of Object.entries(parts)) {
    const current = (base as unknown as Record<string, unknown>)[name];
    merged[name] = typeof value === "function" ? value : { ...(current as object), ...(value as object) };
  }
  return merged as unknown as ApiClient;
}

/** The detail of a real run's shape, as the server client would map it, with the given changes to the raw answer. */
export function runLike(over: Partial<RawRunDetail> & Record<string, unknown> = {}, from: "done" | "human" = "human"): RunDetail {
  const raw = { ...((from === "done" ? rawDone : rawNeedsHuman) as object), ...over } as RawRunDetail;
  return runDetail(raw, new Date("2026-10-24T12:00:00Z"));
}

export function renderAt(path: string, client: ApiClient) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}
