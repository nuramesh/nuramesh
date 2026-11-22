import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { dashboardPath, formatShare } from "@/api/dashboards";
import { DASHBOARD_FIXTURES } from "@/api/fixtures/dashboards";
import { createServerClient } from "@/api/server-client";
import { renderAt, serverLike } from "@/test/server-like";

const verification = DASHBOARD_FIXTURES.verification;

describe("dashboard data", () => {
  it("formats rates from text and shows a missing figure as an em dash, never 0%", () => {
    expect(formatShare("0.7500")).toBe("75.00%");
    expect(formatShare("0.0312")).toBe("3.12%");
    expect(formatShare(null)).toBe("—");
  });

  it("builds routes with the window only, never an organization", () => {
    expect(dashboardPath("verification")).toBe("/v1/dashboards/verification");
    expect(dashboardPath("cost-latency", { from: "2026-01-01T00:00:00Z", asOf: "2026-01-08T00:00:00Z" })).toBe(
      "/v1/dashboards/cost-latency?from=2026-01-01T00%3A00%3A00Z&as_of=2026-01-08T00%3A00%3A00Z",
    );
  });

  it("the server client fetches only /v1/dashboards/* (R211-14)", async () => {
    const urls: string[] = [];
    const fetchFn = vi.fn((input: string) => {
      urls.push(input);
      return Promise.resolve(new Response(JSON.stringify(verification), { status: 200 }));
    });
    const api = createServerClient({ fetch: fetchFn });
    await api.dashboards.verification();
    await api.dashboards.verifiers();
    await api.dashboards.costLatency();
    await api.dashboards.approvals();
    expect(urls).toEqual([
      "/v1/dashboards/verification",
      "/v1/dashboards/verifiers",
      "/v1/dashboards/cost-latency",
      "/v1/dashboards/approvals",
    ]);
  });

  it("the fixture client has sample data for every view", async () => {
    const api = createFixtureClient();
    expect((await api.dashboards.approvals()).waiting).toBe(2);
    expect((await api.dashboards.costLatency()).currencies.USD?.total).toBe("4.35");
  });
});

describe("the dashboards page", () => {
  const client = (over: Partial<typeof verification> = {}) =>
    serverLike({ dashboards: { verification: () => Promise.resolve({ ...verification, ...over }) } });

  it("shows the window, the figures and the verification table", async () => {
    renderAt("/dashboards/verification", client());
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboards" })).toBeInTheDocument();
    expect(await screen.findByTestId("headline")).toHaveTextContent("81.82%");
    expect(screen.getByTestId("dashboard-window")).toHaveTextContent("as of 2026-10-24T12:00:00Z");
    const gates = screen.getByRole("table", { name: /by gate/i });
    expect(within(gates).getByRole("row", { name: /^review/ })).toHaveTextContent("72.73%");
    expect(screen.queryByTestId("dashboard-partial")).not.toBeInTheDocument();
  });

  it("a null figure is an em dash with 'no data', never 0%", async () => {
    renderAt("/dashboards/verification", client({ passed: 0, failed: 0, error: 2, pass_rate: null, gates: [], roles: [] }));
    const headline = await screen.findByTestId("headline");
    expect(within(headline).getByLabelText("no data")).toHaveTextContent("—");
    expect(headline).not.toHaveTextContent("0%");
  });

  it("a partial answer shows a visible banner", async () => {
    renderAt("/dashboards/verification", client({ partial: true, scanned_events: 5 }));
    expect(await screen.findByTestId("dashboard-partial")).toHaveTextContent("first 5 entries");
  });

  it("the verifiers view uses the wording \"Verifier fail rate\"", async () => {
    const api = serverLike({
      dashboards: {
        verifiers: () => Promise.resolve(DASHBOARD_FIXTURES.verifiers),
      },
    });
    renderAt("/dashboards/verifiers", api);
    expect(await screen.findByTestId("headline")).toHaveTextContent("Verifier fail rate 27.27%");
  });

  it("the verifiers view says when results have an unknown kind (no recorded gate start)", async () => {
    const api = serverLike({
      dashboards: {
        verifiers: () => Promise.resolve({ ...DASHBOARD_FIXTURES.verifiers, unknown_kind_results: 4 }),
      },
    });
    renderAt("/dashboards/verifiers", api);
    expect(await screen.findByTestId("verifier-unknown")).toHaveTextContent("4 gate results have no recorded start");
  });

  it("says plainly when the data source cannot give a dashboard", async () => {
    renderAt("/dashboards/approvals", serverLike({}));
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't give this dashboard/i);
  });
});
