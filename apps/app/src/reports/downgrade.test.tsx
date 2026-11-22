import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NotAvailableError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { type RawDowngradeReport, downgradeReport, formatPerTask, formatRate, formatCostShare } from "@/api/reports";
import { createServerClient } from "@/api/server-client";
import { renderAt, serverLike } from "@/test/server-like";

const raw: RawDowngradeReport = {
  org: "demo",
  read_only: true,
  window: { from: null, to: null, as_of: null },
  min_sample: 30,
  excluded: { mixed_model: 0, not_evidence: 2 },
  runs_scanned: 4,
  truncated: false,
  roles: [
    {
      role: "dev",
      models: [
        {
          model: "azure/big",
          tasks: 20,
          calls: 20,
          gate: { passed: 18, failed: 2, error: 0 },
          verifier: { passed: 0, failed: 0, error: 0 },
          results: 20,
          pass_rate: "0.9000",
          cost_per_task: { amount: "0.10", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
        {
          model: "azure/small",
          tasks: 25,
          calls: 25,
          gate: { passed: 20, failed: 2, error: 1 },
          verifier: { passed: 3, failed: 0, error: 0 },
          results: 25,
          pass_rate: "0.9200",
          cost_per_task: { amount: "0.02", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
      ],
      evidence: {
        "azure/big": { tasks: 20, attempts: 20, passed: 18, first_try: 18, pass_rate: "0.900000", first_try_rate: "0.900000", known_total: "20", unknown_calls: 0, cost_per_accepted: "1.111111" },
        "azure/small": { tasks: 25, attempts: 25, passed: 23, first_try: 23, pass_rate: "0.920000", first_try_rate: "0.920000", known_total: "11.5", unknown_calls: 0, cost_per_accepted: "0.500000" },
      },
      recommendation: { status: "recommend", current: "azure/big", candidate: "azure/small", refused: [], saving: "0.800000", projected_saving: "36.000000", pass_rate_current: "0.900000", pass_rate_candidate: "0.920000", tasks_current: 20, tasks_candidate: 25 },
    },
    {
      role: "qa",
      models: [
        {
          model: "lab/mystery",
          tasks: 3,
          calls: 3,
          gate: { passed: 3, failed: 0, error: 0 },
          verifier: { passed: 0, failed: 0, error: 0 },
          results: 3,
          pass_rate: "1.0000",
          cost_per_task: null,
          cost_known: false,
          unknown_cost_calls: 3,
        },
        {
          model: "lab/cheap",
          tasks: 4,
          calls: 4,
          gate: { passed: 4, failed: 0, error: 0 },
          verifier: { passed: 0, failed: 0, error: 0 },
          results: 4,
          pass_rate: "1.0000",
          cost_per_task: { amount: "0.00052050", currency: "USD" },
          cost_known: true,
          unknown_cost_calls: 0,
        },
      ],
      evidence: {
        "lab/mystery": { tasks: 3, attempts: 3, passed: 3, first_try: 3, pass_rate: "1.000000", first_try_rate: "1.000000", known_total: "0", unknown_calls: 3, cost_per_accepted: null },
        "lab/cheap": { tasks: 4, attempts: 4, passed: 4, first_try: 4, pass_rate: "1.000000", first_try_rate: "1.000000", known_total: "0.002082", unknown_calls: 0, cost_per_accepted: "0.000521" },
      },
      recommendation: { status: "insufficient_evidence", current: "lab/cheap", candidate: null, refused: [{ model: "lab/mystery", reason: "not_eligible" }] },
    },
  ],
};

describe("the downgrade report data", () => {
  it("maps the server's answer without rounding or inventing anything", () => {
    const report = downgradeReport(raw);
    expect(report).toMatchObject({ org: "demo", minSample: 30, runsScanned: 4, truncated: false });
    const big = report.roles[0]?.models[0];
    expect(big).toBeDefined();
    if (big === undefined) return;
    expect(big).toMatchObject({ model: "azure/big", results: 20, passRate: "0.9000", costKnown: true });
    expect(big.costPerTask).toEqual({ amount: "0.10", currency: "USD" });
    expect(report.roles[0]?.recommendation).toMatchObject({ status: "recommend", candidate: "azure/small", saving: "0.800000" });
    expect(report.excluded).toEqual({ mixedModel: 0, notEvidence: 2 });
    expect(report.roles[1]?.models[0]?.costPerTask).toBeNull(); // unknown stays unknown
  });

  it("formats money and rates from text, never through floats", () => {
    expect(formatPerTask({ amount: "0.10", currency: "USD" })).toBe("$0.10");
    expect(formatPerTask({ amount: "0.00052050", currency: "USD" })).toBe("$0.0005205");
    expect(formatPerTask({ amount: "12", currency: "USD" })).toBe("$12.00");
    expect(formatPerTask(null)).toBe("unknown");
    expect(formatRate("0.9000")).toBe("90.00%");
    expect(formatRate("0.8333")).toBe("83.33%");
    expect(formatRate("1.0000")).toBe("100.00%");
    expect(formatRate(null)).toBe("no results");
    expect(formatCostShare("0.814906")).toBe("81.49%");
    expect(formatCostShare("0.800000")).toBe("80.00%");
    expect(formatCostShare("0.999950")).toBe("100.00%");
  });
});

describe("the clients", () => {
  it("the server client reads /v1/reports/downgrade and passes the sample size", async () => {
    const fetchFn = vi.fn((input: string) => Promise.resolve(new Response(JSON.stringify(raw), { status: input.length > 0 ? 200 : 500 })));
    const api = createServerClient({ fetch: fetchFn });
    const report = await api.reports.downgrade();
    expect(report.roles).toHaveLength(2);
    expect(fetchFn.mock.calls[0]?.[0]).toBe("/v1/reports/downgrade");
    await api.reports.downgrade({ minSample: 5 });
    expect(fetchFn.mock.calls[1]?.[0]).toBe("/v1/reports/downgrade?min_sample=5");
  });

  it("the fixture client has sample data that shows each kind of answer", async () => {
    const report = await createFixtureClient().reports.downgrade();
    const statuses = report.roles.map((r) => r.recommendation?.status);
    expect(statuses).toEqual(expect.arrayContaining(["recommend", "insufficient_evidence", "none"]));
    expect(report.roles.flatMap((r) => r.models).some((m) => m.costPerTask === null)).toBe(true);
  });
});

describe("the downgrade report page", () => {
  const client = () => serverLike({ reports: { downgrade: () => Promise.resolve(downgradeReport(raw)) } });

  it("shows pass rates next to cost per task for each role and model, and says it only advises", async () => {
    renderAt("/reports/downgrade", client());
    expect(await screen.findByRole("heading", { level: 1, name: "Model downgrade report" })).toBeInTheDocument();
    expect(screen.getByText(/never switches a model/i)).toBeInTheDocument();
    const dev = await screen.findByRole("table", { name: /dev: pass rate and cost per task/i });
    const bigRow = within(dev).getByRole("row", { name: /^azure\/big/ });
    expect(within(bigRow).getByText("90.00%")).toBeInTheDocument();
    expect(within(bigRow).getByText("$0.10")).toBeInTheDocument();
    expect(within(bigRow).getByText("18 of 20")).toBeInTheDocument(); // gate passed of decided
    const advice = screen.getAllByTestId("recommendation")[0];
    expect(advice).toHaveTextContent(/Consider azure\/small instead of azure\/big: pass rate per attempt 90\.00% vs 92\.00% over 20 and 25 tasks, saves 80\.00% per accepted result/);
    const smallRow = within(dev).getByRole("row", { name: /^azure\/small/ });
    expect(within(smallRow).getByText("3 of 3")).toBeInTheDocument(); // verifier
    expect(within(smallRow).getByText("25")).toBeInTheDocument(); // tasks
  });

  it("shows an unknown price as unknown and an unproven recommendation as 'not enough evidence'", async () => {
    renderAt("/reports/downgrade", client());
    const qa = await screen.findByRole("table", { name: /qa: pass rate and cost per task/i });
    const mystery = within(qa).getByRole("row", { name: /^lab\/mystery/ });
    expect(within(mystery).getByText("unknown")).toBeInTheDocument();
    expect(within(mystery).getByText("3 calls without a price")).toBeInTheDocument();
    const cheap = within(qa).getByRole("row", { name: /^lab\/cheap/ });
    expect(within(cheap).getByText("$0.0005205")).toBeInTheDocument();
    expect(screen.getByText("Not enough evidence")).toBeInTheDocument();
    expect(screen.getByText(/lab\/mystery was left out: not allowed for this role/)).toBeInTheDocument();
    expect(screen.getByText(/Each model needs at least 30 finished tasks/)).toBeInTheDocument();
  });

  it("has no control that switches a model, and tables scroll sideways instead of the page", async () => {
    renderAt("/reports/downgrade", client());
    const table = await screen.findByRole("table", { name: /dev: pass rate/i });
    expect(screen.queryByRole("button", { name: /switch|apply|change model|use /i })).not.toBeInTheDocument();
    expect(table.parentElement).toHaveClass("overflow-x-auto");
  });

  it("warns when the server stopped looking early, and says when there is nothing yet", async () => {
    renderAt("/reports/downgrade", serverLike({ reports: { downgrade: () => Promise.resolve({ ...downgradeReport(raw), truncated: true }) } }));
    expect(await screen.findByTestId("downgrade-truncated")).toHaveTextContent(/may be missing/);
  });

  it("explains an empty report", async () => {
    renderAt("/reports/downgrade", serverLike({ reports: { downgrade: () => Promise.resolve({ ...downgradeReport(raw), roles: [] }) } }));
    expect(await screen.findByText(/No finished work to compare yet/)).toBeInTheDocument();
  });

  it("says plainly when the data source cannot give the report", async () => {
    renderAt("/reports/downgrade", serverLike({ reports: { downgrade: () => Promise.reject(new NotAvailableError("reports.downgrade")) } }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't give this report/);
  });

  it("is in the sidebar and uses the fixtures client's sample data", async () => {
    renderAt("/reports/downgrade", createFixtureClient());
    expect(await screen.findByRole("heading", { level: 1, name: "Model downgrade report" })).toBeInTheDocument();
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    expect(nav.getByRole("link", { name: "Downgrade report" })).toHaveAttribute("aria-current", "page");
    expect((await screen.findAllByRole("table")).length).toBeGreaterThan(0);
  });
});
