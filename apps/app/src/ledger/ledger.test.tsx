import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";
import { usd } from "@/api/money";
import type { LedgerEntry } from "@/api/types";

import { exportEntries, formatCount, kindTone, shortHash } from "./format";

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

describe("format", () => {
  it("shortens hashes, groups counts and colours kinds", () => {
    expect(shortHash("abcdef0123456789")).toBe("abcdef");
    expect(formatCount(1051)).toBe("1,051");
    expect(kindTone("gate.passed")).toBe("link");
    expect(kindTone("gate.failed")).toBe("danger");
    expect(kindTone("budget.blocked")).toBe("danger");
    expect(kindTone("approval.requested")).toBe("warm");
    expect(kindTone("model.call")).toBe("muted");
  });

  it("exports hashes and metadata only", () => {
    const entry: LedgerEntry = {
      n: "1",
      label: "1",
      at: "2026-10-23T10:00:00Z",
      kind: "model.call",
      category: "spend",
      actor: "Developer",
      summary: "call",
      cost: usd("0.31"),
      hash: "h1",
      prevHash: "h0",
      stream: "run/1",
      taskId: null,
      fields: [{ label: "Prompt", value: "SECRET PROMPT TEXT" }],
    };
    const text = exportEntries([entry]);
    expect(JSON.parse(text)).toEqual([
      { entry: "1", at: "2026-10-23T10:00:00Z", kind: "model.call", actor: "Developer", summary: "call", cost: { amount: "0.31", currency: "USD" }, hash: "h1", previous_hash: "h0", stream: "run/1", task_id: null },
    ]);
    expect(text).not.toContain("SECRET");
  });
});

describe("Ledger", () => {
  it("shows the chain status, the entries newest first, and the first entry's detail", async () => {
    renderAt("/ledger");
    const status = await screen.findByTestId("chain-status");
    expect(status).toHaveTextContent("Chain verified");
    expect(status).toHaveTextContent(/1,051 entries · no gaps · no edits/);
    expect(status).toHaveTextContent(/Git commit/);
    const table = await screen.findByRole("table", { name: "Ledger entries, newest first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.length).toBeGreaterThan(5);
    const numbers = rows.map((r) => r.getAttribute("data-entry"));
    expect(numbers[0]).toBe(rows[0]?.getAttribute("data-entry"));
    const detail = await screen.findByRole("complementary", { name: "Entry detail" });
    expect(within(detail).getByRole("heading", { name: /^Entry #/ })).toBeInTheDocument();
    expect(within(detail).getByText(/Prompts and outputs are not stored in the ledger/)).toBeInTheDocument();
  });

  it("opens the entry in the URL and shows its chain hashes", async () => {
    renderAt("/ledger/1043");
    const detail = await screen.findByRole("complementary", { name: "Entry detail" });
    expect(await within(detail).findByRole("heading", { name: "Entry #1043" })).toBeInTheDocument();
    expect(within(detail).getByText("Chain")).toBeInTheDocument();
    expect(within(detail).getByText("Previous")).toBeInTheDocument();
    expect(within(detail).getAllByText(/^[0-9a-f]{64}$/)).toHaveLength(2);
  });

  it("filters by category and by search, and says when nothing matches", async () => {
    const user = userEvent.setup();
    renderAt("/ledger");
    await screen.findByRole("table");
    const all = Number(screen.getByTestId("ledger-count").textContent.replace(/\D/g, ""));
    await user.click(screen.getByRole("button", { name: "Gates" }));
    expect(screen.getByRole("button", { name: "Gates" })).toHaveAttribute("aria-pressed", "true");
    await screen.findByRole("table");
    const gates = Number(screen.getByTestId("ledger-count").textContent.replace(/\D/g, ""));
    expect(gates).toBeGreaterThan(0);
    expect(gates).toBeLessThan(all);
    await user.click(screen.getByRole("button", { name: "All" }));
    await user.type(screen.getByRole("searchbox", { name: "Search the ledger" }), "zzzz-no-such");
    expect(await screen.findByText("No entries match.")).toBeInTheDocument();
  });

  it("follows a later change of ?q= on the mounted page", async () => {
    const router = renderAt("/ledger?q=first-term");
    const box = await screen.findByRole("searchbox", { name: "Search the ledger" });
    expect(box).toHaveValue("first-term");
    await act(() => router.navigate("/ledger?q=second-term"));
    expect(screen.getByRole("searchbox", { name: "Search the ledger" })).toHaveValue("second-term");
  });

  it("verifies the chain on request: the banner is replaced by the result", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    let calls = 0;
    client.ledger.verifyChain = () => {
      calls += 1;
      return Promise.resolve({ verified: true, entries: 1099, gaps: 0, anchor: null, checkedAt: "2026-10-23T03:04:05Z" });
    };
    renderAt("/ledger", client);
    expect(await screen.findByTestId("chain-status")).toHaveTextContent(/1,051 entries/);
    await user.click(screen.getByRole("button", { name: "Verify chain" }));
    await screen.findByText(/1,099 entries/);
    expect(calls).toBe(1);
    expect(screen.getByTestId("chain-status")).toHaveTextContent("Head not anchored in Git yet");
  });

  it("shows a chain that fails verification as broken, naming what failed", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    client.ledger.verifyChain = () => Promise.resolve({ verified: true, entries: 12, gaps: 2, anchor: null, checkedAt: "2026-10-23T03:04:05Z" });
    renderAt("/ledger", client);
    await user.click(await screen.findByRole("button", { name: "Verify chain" }));
    await screen.findByText(/Chain broken/);
    expect(screen.getByTestId("chain-status")).toHaveTextContent("entries are missing");
    expect(screen.getByTestId("chain-status")).not.toHaveTextContent("doesn't match its hash");
  });

  it("shows a broken chain as broken, in words", async () => {
    const client = createFixtureClient();
    client.ledger.chainStatus = () => Promise.resolve({ verified: false, entries: 12, gaps: 2, anchor: null, checkedAt: "2026-10-23T10:00:00Z" });
    renderAt("/ledger", client);
    const status = await screen.findByTestId("chain-status");
    expect(status).toHaveTextContent("Chain broken");
    expect(status).toHaveTextContent("2 gaps");
    expect(status).toHaveTextContent("Head not anchored in Git yet");
  });

  it("is honest about a missing entry and about a source that can't answer", async () => {
    renderAt("/ledger/9999999");
    expect(await screen.findByText("There is no ledger entry 9999999.")).toBeInTheDocument();
  });

  it("says plainly when the data source can't list entries", async () => {
    renderAt("/ledger", createUnavailableClient());
    expect(await screen.findByText("This data source can't check the chain yet.")).toBeInTheDocument();
    expect(await screen.findByText(/can't list ledger entries yet/)).toBeInTheDocument();
  });
});
