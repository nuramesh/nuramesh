import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { budgetRow } from "@/api/server/map";
import type { RawBudget } from "@/api/server/raw";
import type { BudgetSummary } from "@/api/types";
import { renderAt, serverLike } from "@/test/server-like";

import { formatUtc, windowNote } from "./windows";

describe("window wording", () => {
  const resetsAt = "2026-10-25T00:00:00.000Z";
  it("formats window edges in UTC", () => {
    expect(formatUtc(resetsAt)).toBe("Sun 25 Oct, 00:00 UTC");
    expect(formatUtc("junk")).toBe("junk");
  });
  it("a reset window starts fresh by itself; a hold window waits for a person", () => {
    expect(windowNote({ window: "day", onBreach: "reset", blocked: false, resetsAt })).toBe("Resets Sun 25 Oct, 00:00 UTC.");
    expect(windowNote({ window: "day", onBreach: "reset", blocked: true, resetsAt })).toBe("Blocked until the window resets (Sun 25 Oct, 00:00 UTC); a person can lift it sooner.");
    expect(windowNote({ window: "day", onBreach: "hold", blocked: true, resetsAt })).toBe("Held: stays blocked, even after the window rolls over, until a person lifts it.");
    expect(windowNote({ window: "month", onBreach: "hold", blocked: false, resetsAt })).toMatch(/held until a person lifts it/);
    expect(windowNote({ window: "none", onBreach: "hold", blocked: true, resetsAt: null })).toBe("Stopped until a person lifts it.");
    expect(windowNote({ window: "none", onBreach: "hold", blocked: false, resetsAt: null })).toBe("No window: a lifetime cap.");
    expect(windowNote({ window: "month", onBreach: "reset", blocked: false, resetsAt: null })).toBe("Resets at the end of the window.");
  });
});

const raw = (over: Partial<RawBudget>): RawBudget => ({
  id: "org-daily",
  scope: "org",
  scope_id: "demo",
  limit: { amount: "30", currency: "USD" },
  spent: { amount: "1.262", currency: "USD" },
  reserved: { amount: "0", currency: "USD" },
  available: { amount: "28.738", currency: "USD" },
  blocked: false,
  window: "day",
  on_breach: "reset",
  window_start: "2026-10-24T00:00:00.000000Z",
  version: 1,
  ...over,
});

const summary: BudgetSummary = {
  orgMonth: { spent: { amount: "1.262", currency: "USD" }, limit: { amount: "30", currency: "USD" } },
  orgWindow: "day",
  costPerVerifiedTask: { amount: "0.000365", currency: "USD" },
  spentOnFailedAttempts: { amount: "0.001095", currency: "USD" },
  verifiedTasks: 3,
  runs: { total: 2, finished: 2, running: 0, blocked: 1 },
  unknownPriceRefusals: 0,
  unknownCostCalls: 0,
  breach: {
    budgetId: "dana-daily",
    scope: "Agent: dana",
    spent: { amount: "0.028", currency: "USD" },
    limit: { amount: "0.01", currency: "USD" },
    since: null,
    ledgerEntry: "",
    nextCall: null,
    window: "day",
    onBreach: "hold",
    resetsAt: null,
  },
};

describe("Budgets with daily windows (real data shapes)", () => {
  const rows = [
    raw({}),
    raw({ id: "dana-daily", scope: "agent", scope_id: "dana", limit: { amount: "0.01", currency: "USD" }, spent: { amount: "0.028", currency: "USD" }, blocked: true, on_breach: "hold" }),
    raw({ id: "rhea-daily", scope: "agent", scope_id: "rhea", limit: { amount: "0.005", currency: "USD" }, spent: { amount: "0.034", currency: "USD" }, blocked: true, on_breach: "reset" }),
    raw({ id: "dana-monthly", scope: "agent", scope_id: "dana", window: "none", window_start: null, on_breach: "hold", limit: { amount: "5", currency: "USD" }, spent: { amount: "0.03", currency: "USD" } }),
  ].map(budgetRow);

  it("shows the window, held versus resets wording per budget, and the daily org spend", async () => {
    const client = serverLike({
      budgets: { list: () => Promise.resolve(rows), summary: () => Promise.resolve(summary) },
      spend: { byModel: () => Promise.resolve([]) },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });
    renderAt("/budgets", client);
    expect(await screen.findByText("Org today")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Org spend today" })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: /Budgets with limit/ });
    expect(within(table).getAllByText("Daily (UTC)")).toHaveLength(3);
    expect(within(table).getByText("Lifetime")).toBeInTheDocument();
    expect(screen.getByTestId("window-note-org-daily")).toHaveTextContent("Resets Sun 25 Oct, 00:00 UTC.");
    expect(screen.getByTestId("window-note-dana-daily")).toHaveTextContent("Held: stays blocked, even after the window rolls over, until a person lifts it.");
    expect(screen.getByTestId("window-note-rhea-daily")).toHaveTextContent(/Blocked until the window resets \(Sun 25 Oct, 00:00 UTC\)/);
    expect(screen.getByTestId("window-note-dana-monthly")).toHaveTextContent("No window: a lifetime cap.");
    // the breach card carries the same wording and does not invent a call or a time it doesn't know
    const card = screen.getByRole("region", { name: /Agent: dana stopped at its \$0\.01 daily limit/ });
    expect(card).toHaveTextContent(/Held: stays blocked/);
    expect(card).toHaveTextContent(/ledger doesn't say which call/);
    expect(card).not.toHaveTextContent(/since/);
    expect(screen.getByText(/0 calls have an unknown cost/)).toBeInTheDocument();
  });

  it("with no organization budget it says so instead of showing zero", async () => {
    const client = serverLike({
      budgets: { list: () => Promise.resolve([]), summary: () => Promise.resolve({ ...summary, orgMonth: null, orgWindow: "none", breach: null }) },
      spend: { byModel: () => Promise.resolve([]) },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });
    renderAt("/budgets", client);
    expect(await screen.findByText(/No organization budget is defined on this server/)).toBeInTheDocument();
  });
});
