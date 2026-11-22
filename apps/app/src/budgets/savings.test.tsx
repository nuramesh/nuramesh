import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NotAvailableError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { type RawTokenSavings, formatSaved, formatSavingShare, tokenSavings } from "@/api/savings";
import { createServerClient } from "@/api/server-client";
import { renderAt, serverLike } from "@/test/server-like";

const raw: RawTokenSavings = {
  organization: "demo",
  currency: "USD",
  as_of: "2026-10-26T12:00:00Z",
  from: "2026-10-01T00:00:00Z",
  to: "2026-11-01T00:00:00Z",
  group_by: "model",
  rows: [
    {
      group: "anthropic/m1",
      calls: 2,
      unknown_calls: 0,
      known_total: "0.0042372",
      complete: true,
      input_tokens: 20,
      output_tokens: 2,
      cached_input_tokens: 1024,
      cache_write_tokens: 1024,
      calls_without_usage: 0,
      cache_hit_rate: "0.495164",
      cache_saving: "0.0027648",
      cache_write_premium: "0.000768",
      net_cache_saving: "0.0019968",
      unknown_saving_calls: 0,
      saving_complete: true,
      cache_saving_share: "0.320308",
    },
    {
      group: "lab/unpriced",
      calls: 3,
      unknown_calls: 1,
      known_total: "0",
      complete: false,
      input_tokens: 30,
      output_tokens: 3,
      cached_input_tokens: 0,
      cache_write_tokens: 0,
      calls_without_usage: 1,
      cache_hit_rate: "0.000000",
      cache_saving: "0",
      cache_write_premium: "0",
      net_cache_saving: "-0.000001",
      unknown_saving_calls: 2,
      saving_complete: false,
      cache_saving_share: null,
    },
  ],
};

describe("the token savings data", () => {
  it("maps the server's answer without rounding or inventing anything", () => {
    const report = tokenSavings(raw);
    expect(report).toMatchObject({ organization: "demo", groupBy: "model" });
    expect(report.rows[0]).toMatchObject({ inputTokens: 20, cachedInputTokens: 1024, cacheHitRate: "0.495164", savingComplete: true });
    expect(report.rows[0]?.netCacheSaving).toEqual({ amount: "0.0019968", currency: "USD" });
    expect(report.rows[1]?.cacheSavingShare).toBeNull(); // unknown stays unknown
  });

  it("formats rates and money from text, never through floats", () => {
    expect(formatSavingShare("0.495164")).toBe("49.5164%");
    expect(formatSavingShare("0.320308")).toBe("32.0308%");
    expect(formatSavingShare("0.000000")).toBe("0%");
    expect(formatSavingShare("1.000000")).toBe("100%");
    expect(formatSavingShare("-0.000001")).toBe("-0.0001%");
    expect(formatSavingShare(null)).toBe("not stated");
    expect(formatSaved({ amount: "0.0027648", currency: "USD" })).toBe("$0.0027648");
    expect(formatSaved({ amount: "-0.000768", currency: "USD" })).toBe("-$0.000768");
    expect(formatSaved({ amount: "0", currency: "USD" })).toBe("$0.00");
    expect(formatSaved({ amount: "1234.5", currency: "USD" })).toBe("$1,234.50");
  });
});

describe("the clients", () => {
  it("the server client reads /v1/savings/tokens with the grouping", async () => {
    const fetchFn = vi.fn((input: string) => Promise.resolve(new Response(JSON.stringify(raw), { status: input.length > 0 ? 200 : 500 })));
    const api = createServerClient({ fetch: fetchFn });
    expect((await api.savings.tokens()).rows).toHaveLength(2);
    expect(fetchFn.mock.calls[0]?.[0]).toBe("/v1/savings/tokens");
    await api.savings.tokens({ groupBy: "role" });
    expect(fetchFn.mock.calls[1]?.[0]).toBe("/v1/savings/tokens?group_by=role");
  });

  it("the fixture client has sample data with a cached, a plain and an unpriced model", async () => {
    const report = await createFixtureClient().savings.tokens({ groupBy: "model" });
    expect(report.rows.some((r) => r.cacheHitRate !== null && r.cacheHitRate !== "0.000000")).toBe(true);
    expect(report.rows.some((r) => r.unknownCalls > 0 && !r.savingComplete)).toBe(true);
  });
});

describe("the Savings tab of the Budgets screen", () => {
  const client = () => serverLike({ savings: { tokens: () => Promise.resolve(tokenSavings(raw)) } });

  it("is a tab next to Budgets, and the Budgets tab stays the default", async () => {
    renderAt("/budgets", createFixtureClient());
    expect(await screen.findByRole("tab", { name: "Budgets", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Savings", selected: false })).toBeInTheDocument();
  });

  it("shows tokens, hit rate with n, the saving, and words unknown as not priced and excluded", async () => {
    const user = userEvent.setup();
    renderAt("/budgets", client());
    await user.click(await screen.findByRole("tab", { name: "Savings" }));
    const table = await screen.findByRole("table", { name: "Tokens and cache" });
    const good = within(table).getByRole("row", { name: /^anthropic\/m1/ });
    expect(within(good).getByText("49.5164%")).toBeInTheDocument();
    expect(within(good).getAllByText("n=2")).toHaveLength(2);
    expect(within(good).getByText("$0.0019968")).toBeInTheDocument();
    expect(within(good).getByText("32.0308%")).toBeInTheDocument();
    const bad = within(table).getByRole("row", { name: /^lab\/unpriced/ });
    expect(within(bad).getByText("1 call not priced, excluded from the money figures")).toBeInTheDocument();
    expect(within(bad).getByText("2 calls excluded from the saving: the saving cannot be known")).toBeInTheDocument();
    expect(within(bad).getByText("-$0.000001")).toBeInTheDocument();
    expect(within(bad).getByText("not stated")).toBeInTheDocument();
    expect(screen.queryByText(/at least/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /apply|enable|switch/i })).toBeNull(); // no apply button in the open core
  });

  it("asks for the chosen grouping", async () => {
    const user = userEvent.setup();
    const seen: (string | undefined)[] = [];
    renderAt(
      "/budgets",
      serverLike({
        savings: {
          tokens: (o) => {
            seen.push(o?.groupBy);
            return Promise.resolve(tokenSavings(raw));
          },
        },
      }),
    );
    await user.click(await screen.findByRole("tab", { name: "Savings" }));
    await screen.findByRole("table", { name: "Tokens and cache" });
    await user.selectOptions(screen.getByLabelText("Group by"), "role");
    await screen.findByRole("table", { name: "Tokens and cache" });
    expect(seen).toEqual(expect.arrayContaining(["model", "role"]));
  });

  it("says plainly when the source cannot show it, and when nothing was recorded", async () => {
    const user = userEvent.setup();
    renderAt("/budgets", serverLike({ savings: { tokens: () => Promise.reject(new NotAvailableError("savings.tokens")) } }));
    await user.click(await screen.findByRole("tab", { name: "Savings" }));
    expect(await screen.findByText("This data source can't show token figures yet.")).toBeInTheDocument();
  });

  it("shows an empty period as a sentence, not a zero row", async () => {
    const user = userEvent.setup();
    renderAt("/budgets", serverLike({ savings: { tokens: () => Promise.resolve(tokenSavings({ ...raw, rows: [] })) } }));
    await user.click(await screen.findByRole("tab", { name: "Savings" }));
    expect(await screen.findByText("No model calls were recorded in this period.")).toBeInTheDocument();
  });
});
