import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { AuthApi } from "@/auth/types";
import { Providers } from "@/app/providers";

import { Timeline } from "./timeline";

const auth: AuthApi = {
  state: () => ({ kind: "open" }),
  subscribe: () => () => undefined,
  load: () => Promise.resolve(),
  login: () => Promise.resolve({ kind: "signed-in" as const }),
  completeMfa: () => Promise.resolve(),
  logout: () => Promise.resolve(),
  setupStatus: () => Promise.resolve(false),
  redeemSetup: () => Promise.resolve(),
};

function show(client: ApiClient) {
  render(
    <Providers client={client}>
      <MemoryRouter>
        <Timeline runId="r1" events={[]} loading={false} unavailable={false} mode="unreachable" />
      </MemoryRouter>
    </Providers>,
  );
  return screen.getByTestId("timeline-mode");
}

describe("G7 the unreachable-stream text", () => {
  it("adds 'or sign in again' only for a source with accounts", () => {
    const withAccounts = show({ ...createFixtureClient(), auth });
    expect(withAccounts).toHaveTextContent("cannot reach the server (check the address and that the server allows this origin, or sign in again)");
  });

  it("does not mention signing in for a source without accounts", () => {
    const text = show(createFixtureClient());
    expect(text).toHaveTextContent("cannot reach the server (check the address and that the server allows this origin)");
    expect(text).not.toHaveTextContent("sign in again");
  });
});
