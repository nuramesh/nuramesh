import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

import { fixture } from "./test-utils";

/** Every button whose name is an action only a signed-in person may take. */
const ACTION = /^(halt|resume|pause|reset|disable|enable|unblock|approve\b|decline\b|reject\b|deny\b|stop\b|confirm)/i;

function renderAt(path: string, scenario: "viewer" | "running" = "viewer") {
  render(
    <Providers client={fixture(scenario)}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
    </Providers>,
  );
}

function actionButtons(): string[] {
  return screen
    .queryAllByRole("button")
    .map((b) => b.getAttribute("aria-label") ?? b.textContent)
    .filter((name) => ACTION.test(name));
}

/** A signed-in role that cannot act (`can_act` false) sees no action button on any page (ND106). */
describe("a viewer sees no action buttons anywhere", () => {
  const pages: [string, string, () => Promise<unknown>][] = [
    ["budgets (blocked budget and breakers)", "/budgets", async () => within(await screen.findByRole("table", { name: /Budgets with limit/ })).findByText("blocked")],
    ["approvals list", "/approvals", async () => screen.findByRole("heading", { level: 1 })],
    ["a pending approval", "/approvals/a1", async () => screen.findByRole("heading", { level: 1 })],
    ["a running run", "/runs/run-128", async () => screen.findByRole("heading", { level: 1 })],
    ["the runs list", "/runs", async () => screen.findByRole("table", { name: "Runs, newest first" })],
    ["the ledger", "/ledger", async () => screen.findByRole("heading", { level: 1, name: "Ledger" })],
    ["templates", "/templates", async () => screen.findByRole("heading", { level: 1 })],
    ["settings", "/settings", async () => screen.findByRole("heading", { level: 1, name: "Settings" })],
  ];
  for (const [name, path, loaded] of pages) {
    it(`${name}: none`, async () => {
      renderAt(path);
      await loaded();
      await screen.findByText("Only a signed-in person can halt the organization.");
      await screen.findAllByRole("heading");
      expect(actionButtons()).toEqual([]);
    });
  }

  it("the budget block panel shows a read-only hint instead of Unblock", async () => {
    renderAt("/budgets");
    await screen.findByText("Only a signed-in person can halt the organization.");
    expect(await screen.findByText(/Only a signed-in person can unblock a budget/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unblock with a reason" })).toBeNull();
  });

  it("a pending approval shows a read-only hint instead of Approve and Reject", async () => {
    renderAt("/approvals/a1");
    await screen.findByText("Only a signed-in person can halt the organization.");
    expect(await screen.findByText(/Only a signed-in person can decide an approval/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(actionButtons()).toEqual([]);
  });

  it("the viewer guard would catch a rendered Approve or Reject button", () => {
    const button = (label: string) => {
      const b = document.createElement("button");
      b.textContent = label;
      document.body.append(b);
      return b;
    };
    const added = [button("Approve"), button("Reject")];
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeNull();
    expect(actionButtons()).toEqual(["Approve", "Reject"]);
    added.forEach((b) => { b.remove(); });
  });

  it("a running run shows a read-only hint instead of Pause and Stop", async () => {
    renderAt("/runs/run-128");
    await screen.findByText("Only a signed-in person can halt the organization.");
    expect(await screen.findByText(/Only a signed-in person can stop a run/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Stop$|Stopping/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
  });

  it("control: a person (can_act) still gets the buttons", async () => {
    renderAt("/budgets", "running");
    expect(await screen.findByRole("button", { name: "Unblock with a reason" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Halt organization" })).toBeInTheDocument();
  });
});
