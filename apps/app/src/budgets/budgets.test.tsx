import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

function renderAt(client: ApiClient = createFixtureClient()) {
  render(
    <Providers client={client}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/budgets"] })} />
    </Providers>,
  );
}

describe("Budgets and spend", () => {
  it("shows the four numbers, the budgets table with status in words, and spend by model", async () => {
    renderAt();
    expect(await screen.findByRole("heading", { name: "Budgets and spend", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("Org this month")).toBeInTheDocument();
    expect(screen.getByText("Cost per verified task")).toBeInTheDocument();
    expect(screen.getByText("Spent on failed attempts")).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: /Budgets with limit/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.length).toBeGreaterThan(3);
    expect(rows.map((r) => r.getAttribute("data-status"))).toEqual(expect.arrayContaining(["ok", "blocked"]));
    expect(within(table).getByText("blocked")).toBeInTheDocument();
    const models = await screen.findByRole("region", { name: "Spend by model" });
    expect(within(models).getAllByRole("progressbar").length).toBeGreaterThan(2);
    expect(within(models).getByText("$0 (self-hosted)")).toBeInTheDocument();
    expect(screen.getByText(/Unknown prices are blocked, never guessed: \d+ calls? refused this month/)).toBeInTheDocument();
  });

  it("a call whose cost is unknown is said to have an unknown cost, never 'no cost' (it may still be charged, #1459)", async () => {
    const fixtures = createFixtureClient();
    const summary = async () => ({ ...(await fixtures.budgets.summary()), unknownCostCalls: 2 });
    renderAt({ ...fixtures, budgets: { ...fixtures.budgets, summary } });
    expect(await screen.findByText(/2 calls have an unknown cost/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/have no cost/);
  });

  it("requires a reason to unblock, and a new limit above the current one", async () => {
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByRole("button", { name: "Unblock with a reason" }));
    const confirm = screen.getByRole("button", { name: "Confirm unblock" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/Reason for unblocking/), "   ");
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/Reason for unblocking/), "Migration needs more");
    expect(confirm).toBeEnabled();
    await user.type(screen.getByLabelText(/New limit in USD/), "50.00");
    expect(confirm).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/Must be more than \$50\.00/);
    await user.clear(screen.getByLabelText(/New limit in USD/));
    await user.type(screen.getByLabelText(/New limit in USD/), "lots");
    expect(screen.getByRole("alert")).toHaveTextContent(/amount such as/);
  });

  it("unblocks with a reason: the breach card goes, the ledger entry is named, the table agrees", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    const calls: { id: string; reason: string; newLimit?: string }[] = [];
    const original = client.budgets.unblock.bind(client.budgets);
    client.budgets.unblock = (id, input) => {
      calls.push({ id, ...input });
      return original(id, input);
    };
    renderAt(client);
    await user.click(await screen.findByRole("button", { name: "Unblock with a reason" }));
    await user.type(screen.getByLabelText(/Reason for unblocking/), "Approved by finance");
    await user.type(screen.getByLabelText(/New limit in USD/), "60.00");
    await user.click(screen.getByRole("button", { name: "Confirm unblock" }));
    expect(calls).toEqual([{ id: "run-127", reason: "Approved by finance", newLimit: "60.00" }]);
    expect(await screen.findByText(/unblocked by You · reason recorded as ledger #\d+/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unblock with a reason" })).not.toBeInTheDocument();
    const table = screen.getByRole("table", { name: /Budgets with limit/ });
    expect(within(table).queryByText("blocked")).not.toBeInTheDocument();
    expect(await within(table).findByText("$60.00")).toBeInTheDocument(); // the raised limit reached the API
  });

  it("forgets a cancelled form and rejects odd amounts", async () => {
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByRole("button", { name: "Unblock with a reason" }));
    await user.type(screen.getByLabelText(/Reason for unblocking/), "temp");
    for (const bad of ["1e3", "-5", "50.0000001"]) {
      await user.clear(screen.getByLabelText(/New limit in USD/));
      await user.type(screen.getByLabelText(/New limit in USD/), bad);
      expect(screen.getByRole("button", { name: "Confirm unblock" })).toBeDisabled();
    }
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Unblock with a reason" }));
    expect(screen.getByLabelText(/Reason for unblocking/)).toHaveValue("");
    expect(screen.getByLabelText(/New limit in USD/)).toHaveValue("");
  });

  it("is honest when the source can't answer", async () => {
    renderAt(createUnavailableClient());
    expect(await screen.findByRole("alert")).toHaveTextContent("can't show budgets yet");
  });

  it("has the circuit breakers panel under the budgets, and it keeps working when the budgets cannot load", async () => {
    renderAt();
    const panel = await screen.findByRole("region", { name: "Circuit breakers" });
    const budgets = await screen.findByRole("table", { name: /Budgets with limit/ });
    expect(budgets.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(await within(panel).findByRole("row", { name: /spend-spike/ })).toBeInTheDocument();
    const broken = { ...createFixtureClient(), budgets: createUnavailableClient().budgets };
    renderAt(broken);
    expect(await screen.findAllByRole("row", { name: /denied-loop/ })).not.toHaveLength(0);
  });
});
