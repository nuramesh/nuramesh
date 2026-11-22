import { act, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { Providers } from "@/app/providers";

import { AuthGate } from "./auth-gate";

/** Counts how often it is mounted: a remount while a screen holds secrets would drop that screen's state. */
function Probe({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount();
  }, [onMount]);
  return <p>page</p>;
}

describe("AuthGate shape", () => {
  it("the page below is mounted once: a hold starting or ending never remounts it", async () => {
    const client = createFixtureClient({ signIn: { account: { email: "ada@example.org", password: "obviously-fake-test-password", role: "owner" }, signedIn: true } });
    const auth = client.auth;
    if (auth?.hold === undefined) throw new Error("the fixture client has no hold");
    const mounts = { count: 0 };
    const onMount = (): void => { mounts.count += 1; };
    render(
      <Providers client={client}>
        <MemoryRouter>
          <AuthGate>
            <Probe onMount={onMount} />
          </AuthGate>
        </MemoryRouter>
      </Providers>,
    );
    expect(await screen.findByText("page")).toBeInTheDocument();
    expect(mounts.count).toBe(1);
    let release = (): void => undefined;
    act(() => { release = auth.hold?.() ?? release; });
    expect(auth.held?.()).toBe(true);
    expect(screen.getByText("page")).toBeInTheDocument();
    act(() => { release(); });
    expect(auth.held?.()).toBe(false);
    expect(screen.getByText("page")).toBeInTheDocument();
    expect(mounts.count).toBe(1);
  });
});
