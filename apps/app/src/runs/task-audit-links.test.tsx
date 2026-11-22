import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { usd } from "@/api/money";
import type { Approval } from "@/api/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";
import { renderAt as renderServerLike, runLike, serverLike } from "@/test/server-like";

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

describe("links to the audit page", () => {
  it("U16 each task on the run page has an Audit trail link, at least 44 px high", async () => {
    const run = runLike({ status: "running", ended_at: null });
    const client = serverLike({ runs: { get: () => Promise.resolve(run) } });
    renderServerLike(`/runs/${run.id}`, client);
    await screen.findByRole("heading", { level: 1 });
    const task = run.taskList?.[0];
    expect(task).toBeDefined();
    const links = await screen.findAllByRole("link", { name: /Audit trail/ });
    const first = links[0];
    expect(first).toHaveAttribute("href", `/runs/${encodeURIComponent(run.id)}/tasks/${encodeURIComponent(task?.id ?? "")}`);
    expect(first).toHaveClass("min-h-11");
  });

  it("U17 the approvals inbox links the task id to its audit page", async () => {
    const a: Approval = {
      id: "a1",
      title: "Merge",
      requester: "writer",
      runId: "r1",
      runLabel: "#1",
      taskId: "t1",
      requestedAt: "2026-10-23T10:00:00Z",
      state: "pending",
      effect: "",
      rule: "merge: ask",
      runSpend: { spent: usd("1"), limit: usd("2") },
      evidence: [{ state: "ok", text: "Requested (ledger #3)" }],
      decision: null,
    };
    const base = createFixtureClient();
    const client: ApiClient = { ...base, approvals: { ...base.approvals, list: () => Promise.resolve([a]), get: () => Promise.resolve(a) } };
    renderAt("/approvals/a1", client);
    const link = await screen.findByRole("link", { name: "t1" });
    expect(link).toHaveAttribute("href", "/runs/r1/tasks/t1");
  });

  it("M9 the ledger opened at ?q= holds the search text", async () => {
    renderAt("/ledger?q=run%3Ar1");
    const box = await screen.findByRole("searchbox");
    expect(box).toHaveValue("run:r1");
  });

  it("G4 the timeline's Open in ledger goes to the run's entries", async () => {
    const user = userEvent.setup();
    renderAt("/runs/run-128");
    const link = await screen.findByRole("link", { name: "Open in ledger" });
    expect(link).toHaveAttribute("href", "/ledger?q=run%3Arun-128");
    await user.click(link);
    expect(await screen.findByRole("searchbox")).toHaveValue("run:run-128");
  });
});
