import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { type ApiClient, ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";


import { FIRST_APPROVAL_PLAN } from "./first-plan";

function renderRuns(client: ApiClient) {
  const router = createMemoryRouter(routes, { initialEntries: ["/runs"] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

/** The sample client with its plan start replaced, so the page's own behaviour is what is tested. */
function withStart(
  startPlan: ApiClient["runs"]["startPlan"],
  options?: Parameters<typeof createFixtureClient>[0],
  startStatus: ApiClient["runs"]["startStatus"] = () => Promise.resolve({ state: "begun", refusalCode: null }),
): ApiClient {
  const base = createFixtureClient(options);
  return { ...base, runs: { ...base.runs, startPlan, startStatus } };
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Start a run" }));
  return screen.findByRole("dialog", { name: "Start a run" });
}

describe("Start a run (first approval, the quickstart guide, section 9)", () => {
  it("sends the plan, then names the run, links it and says who must decide", async () => {
    const user = userEvent.setup();
    const startPlan = vi.fn<ApiClient["runs"]["startPlan"]>(() => Promise.resolve({ runId: "run-0f0f", state: "accepted", engine: "light" }));
    // the runner has not begun the run at the first look, and has at the second
    const startStatus = vi
      .fn<ApiClient["runs"]["startStatus"]>()
      .mockResolvedValueOnce({ state: "requested", refusalCode: null })
      .mockResolvedValue({ state: "begun", refusalCode: null });
    renderRuns(withStart(startPlan, undefined, startStatus));
    const dialog = await openDialog(user);
    expect(within(dialog).getByLabelText("Plan (JSON)")).toHaveValue(FIRST_APPROVAL_PLAN);
    expect(dialog).toHaveTextContent("Once a second person can decide, you cannot decide a request of a run you started");
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    expect(startPlan).toHaveBeenCalledExactlyOnceWith(JSON.parse(FIRST_APPROVAL_PLAN));
    const started = await screen.findByTestId("run-started");
    expect(started).toHaveTextContent("run-0f0f");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // a requested run has no page yet: no link until the runner has begun it
    expect(started).toHaveTextContent("Waiting for a runner to begin it");
    expect(within(started).queryByRole("link", { name: "Open the run" })).not.toBeInTheDocument();
    expect(await within(started).findByRole("link", { name: "Open the run" }, { timeout: 4000 })).toHaveAttribute("href", "/runs/run-0f0f");
    expect(within(started).getByRole("link", { name: "Approvals" })).toHaveAttribute("href", "/approvals");
    expect(startStatus).toHaveBeenCalledWith("run-0f0f");
  });

  it("names the runner's refusal code when the accepted plan is refused afterwards", async () => {
    const user = userEvent.setup();
    const startPlan = vi.fn<ApiClient["runs"]["startPlan"]>(() => Promise.resolve({ runId: "run-0f0f", state: "accepted", engine: "light" }));
    const startStatus = vi.fn<ApiClient["runs"]["startStatus"]>(() => Promise.resolve({ state: "refused", refusalCode: "plan_gate_preset_missing" }));
    renderRuns(withStart(startPlan, undefined, startStatus));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    const refused = await screen.findByTestId("run-refused");
    expect(refused).toHaveAttribute("role", "alert");
    expect(refused).toHaveTextContent("run-0f0f");
    expect(refused).toHaveTextContent("plan_gate_preset_missing");
    expect(screen.queryByRole("link", { name: "Open the run" })).not.toBeInTheDocument();
  });

  it("shows the server's refusal with its code and keeps the dialog and the plan", async () => {
    const user = userEvent.setup();
    const startPlan = vi.fn<ApiClient["runs"]["startPlan"]>(() =>
      Promise.reject(new ProblemError("the organization has no budget to start a run", 409, "run_budget_missing")),
    );
    renderRuns(withStart(startPlan));
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    // the dialog's alert line is always there (empty until something is refused)
    const alert = within(dialog).getByRole("alert");
    await waitFor(() => {
      expect(alert).toHaveTextContent("the organization has no budget to start a run (run_budget_missing)");
    });
    expect(within(dialog).getByLabelText("Plan (JSON)")).toHaveValue(FIRST_APPROVAL_PLAN);
    expect(screen.queryByTestId("run-started")).not.toBeInTheDocument();
  });

  it("refuses text that is not a JSON object here, without calling the server", async () => {
    const user = userEvent.setup();
    const startPlan = vi.fn<ApiClient["runs"]["startPlan"]>();
    renderRuns(withStart(startPlan));
    const dialog = await openDialog(user);
    const plan = within(dialog).getByLabelText("Plan (JSON)");
    await user.clear(plan);
    await user.type(plan, "not json");
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    await waitFor(() => {
      expect(within(dialog).getByRole("alert")).toHaveTextContent("The plan is not valid JSON");
    });
    await user.clear(plan);
    await user.type(plan, "[[1]");
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    await waitFor(() => {
      expect(within(dialog).getByRole("alert")).toHaveTextContent("The plan must be a JSON object");
    });
    expect(startPlan).not.toHaveBeenCalled();
  });

  it("says so when the data source cannot start a run (sample data)", async () => {
    const user = userEvent.setup();
    renderRuns(createFixtureClient());
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start" }));
    await waitFor(() => {
      expect(within(dialog).getByRole("alert")).toHaveTextContent("This data source can't start a run");
    });
  });

  it("offers no button to a caller who may not act (default deny)", async () => {
    renderRuns(withStart(vi.fn<ApiClient["runs"]["startPlan"]>(), { orgSafety: "viewer" }));
    await screen.findByRole("table", { name: "Runs, newest first" });
    expect(screen.queryByRole("button", { name: "Start a run" })).not.toBeInTheDocument();
  });
});
