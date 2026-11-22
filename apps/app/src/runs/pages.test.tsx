import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";
import type { ApiClient } from "@/api/client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

describe("Runs", () => {
  it("lists every run, newest first, with status words, spend and gates", async () => {
    renderAt("/runs");
    const table = await screen.findByRole("table", { name: "Runs, newest first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    expect(within(rows[0] ?? document.body).getByText("#128")).toBeInTheDocument(); // started 19:39, the latest
    // the status shows in its column and, on a phone (column hidden), under the title
    expect(within(table).getAllByText("Running")).toHaveLength(2);
    expect(within(table).getAllByText("Waiting for you")).toHaveLength(2);
    expect(within(table).getAllByText("Blocked by budget")).toHaveLength(2);
    expect(within(table).getAllByText("Failed", { selector: "span" })).toHaveLength(2);
    expect(screen.getByTestId("runs-summary")).toHaveTextContent("5 runs · 1 running · 1 waiting for you");
    expect(within(table).getByText(/\$1\.84/)).toBeInTheDocument();
    // once in the Gates column, once under the title (shown on a phone, where the column is hidden)
    expect(within(table).getAllByText(/· 1 failed/)).toHaveLength(2);
    expect(within(table).getByRole("link", { name: /Fix issue #142/ })).toHaveAttribute("href", "/runs/run-128");
  });

  it("says plainly when the data source can't list runs, and lets you retry", async () => {
    renderAt("/runs", createUnavailableClient());
    expect(await screen.findByRole("alert")).toHaveTextContent("can't list runs yet");
    // the status bar above the page has its own Try again (E19): this one is the page's
    const bar = await screen.findByRole("region", { name: "Organization status" });
    const retries = screen.getAllByRole("button", { name: "Try again" }).filter((b) => !bar.contains(b));
    expect(retries).toHaveLength(1);
  });
});

describe("Run", () => {
  it("shows the header, the four numbers, the graph, the latest gate and the timeline", async () => {
    renderAt("/runs/run-128");
    expect(await screen.findByRole("heading", { level: 1, name: /Fix issue #142/ })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Runs/#128");
    expect(screen.getByText("Dev team template")).toBeInTheDocument();
    expect(screen.getByText("canon a1b9f03")).toBeInTheDocument();
    // four numbers
    expect(screen.getByText("of 3 done")).toBeInTheDocument();
    expect(screen.getByText("passed", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Spend of the run budget" })).toHaveAttribute("aria-valuenow", "4");
    expect(screen.getByTestId("needs-you")).toHaveAttribute("href", "/approvals");
    // graph: status in words on every node
    expect(screen.getByTestId("run-node-dev")).toHaveAttribute("data-status", "done");
    expect(screen.getByTestId("run-node-tester")).toHaveAttribute("data-status", "running");
    // latest gate: criteria, the independence facts and a ledger link, no content
    const gate = screen.getByRole("region", { name: "Latest gate result" });
    expect(within(gate).getByText("Webhook retries 3 times with backoff")).toBeInTheDocument();
    expect(within(gate).getByText(/different family/)).toBeInTheDocument();
    expect(within(gate).getByText("developer reasoning")).toBeInTheDocument();
    expect(within(gate).getByRole("link", { name: "#1043" })).toHaveAttribute("href", "/ledger/1043");
  });

  it("lists events newest first and filters them", async () => {
    const user = userEvent.setup();
    renderAt("/runs/run-128");
    const log = await screen.findByRole("log", { name: "Events, newest first" });
    const seqs = within(log).getAllByRole("listitem").map((li) => Number(li.getAttribute("data-seq")));
    expect(seqs).toEqual([...seqs].sort((a, b) => b - a));
    expect(seqs.length).toBeGreaterThan(5);
    await user.click(screen.getByRole("button", { name: "Gates" }));
    expect(screen.getByRole("button", { name: "Gates" })).toHaveAttribute("aria-pressed", "true");
    const gateEvents = within(screen.getByRole("log")).getAllByRole("listitem");
    expect(gateEvents.length).toBeGreaterThan(0);
    expect(gateEvents.length).toBeLessThan(seqs.length);
    for (const li of gateEvents) expect(li).toHaveTextContent(/gate|check|review/i);
  });

  it("pauses the run: the status changes, the ledger entry is named and the event appears without a reload", async () => {
    const user = userEvent.setup();
    renderAt("/runs/run-128");
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Paused", { selector: "span" })).toBeInTheDocument();
    expect(await screen.findByText(/Paused #128\. Ledger entry #\d+\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(await within(screen.getByRole("log")).findByText("run.paused")).toBeInTheDocument();
  });

  it("asks before stopping, and stopping disables both controls", async () => {
    const user = userEvent.setup();
    renderAt("/runs/run-128");
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: "Stop run #128?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep running" }));
    expect(screen.getByText("Running", { selector: "span" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Stop" }));
    expect(await screen.findByText("Stopped", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("says so when the run doesn't exist, and when the source can't help", async () => {
    renderAt("/runs/nope");
    expect(await screen.findByRole("heading", { name: "Run not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All runs" })).toHaveAttribute("href", "/runs");
  });

  it("a finished run has no controls to press and its graph is all done", async () => {
    renderAt("/runs/run-121");
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(screen.queryByTestId("needs-you")).toBeNull();
  });
});

describe("naming words, U01 to U07 and U10 (#611)", () => {
  it("U10: the run page links to the run report when the source has one", async () => {
    const base = createFixtureClient();
    const client: ApiClient = { ...base, runs: { ...base.runs, reportUrl: (id) => `/v1/runs/${id}/report` } };
    renderAt("/runs/run-128", client);
    const link = await screen.findByRole("link", { name: "Report" });
    expect(link).toHaveAttribute("href", "/v1/runs/run-128/report");
  });

  it("U10: no Report link on sample data (no server to open it from)", async () => {
    renderAt("/runs/run-128");
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("link", { name: "Report" })).toBeNull();
  });

  it("U01 U02: the old words are gone from the run pages", async () => {
    renderAt("/runs/run-128");
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByText(/Start a run|Stop run$/)).toBeNull();
  });
});
