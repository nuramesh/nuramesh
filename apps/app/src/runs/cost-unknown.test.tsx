import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RawSpend } from "@/api/server/raw";
import { rawNeedsHuman } from "@/test/contract";
import { renderAt, runLike, serverLike } from "@/test/server-like";

const RUN = "run-20261023-193348-ef9e";

const client = (spend: Partial<RawSpend>) => {
  const run = runLike({ spend: { ...(rawNeedsHuman as { spend: RawSpend }).spend, ...spend } });
  return serverLike({ runs: { get: () => Promise.resolve(run) } });
};

const spendPanel = async (): Promise<HTMLElement> => {
  const section = (await screen.findByText("Where the money went")).closest("section");
  if (section === null) throw new Error("no spend panel");
  return section;
};

// spec default-prices 6.2: the run view keeps "no price" (blocked, set a price) apart from "cost unknown"
describe("the run view, a call of unknown cost", () => {
  it("says 'Cost unknown (charged the reservation)' and gives the total as 'at least', from the server's fields", async () => {
    const raw = { total: { amount: "0.04", currency: "USD" }, calls: 3, unknown_cost_calls: 1, cost_unknown_calls: 1, at_least: { amount: "0.04", currency: "USD" } };
    renderAt(`/runs/${RUN}`, client(raw));
    const panel = await spendPanel();
    const note = within(panel).getByRole("status");
    expect(note).toHaveTextContent("Cost unknown (charged the reservation): 1 call. Total: at least $0.04000000.");
    expect(panel).not.toHaveTextContent(/unknown price|no price|blocked/i);
  });

  it("says nothing about unknown cost when every cost is known", async () => {
    renderAt(`/runs/${RUN}`, client({ cost_unknown_calls: 0, at_least: null }));
    const panel = await spendPanel();
    expect(within(panel).queryByRole("status")).toBeNull();
  });
});
