import { describe, expect, it } from "vitest";

import type { CatalogEntry, ModelPrice } from "@/api/models";

import { atLeastWords, COST_UNKNOWN_WORDS, NO_PRICE_WORDS, noPriceCount, priceWords } from "./words";

const base: ModelPrice = {
  inputPerMillion: "2",
  outputPerMillion: "10",
  cachedInputPerMillion: null,
  effectiveFrom: "2026-11-17T00:00:00Z",
  source: "shipped list; Anthropic pricing page, retrieved 2026-11-17",
  provisional: false,
};

const entry = (ref: string, price: ModelPrice | null): CatalogEntry => ({ ref, price }) as CatalogEntry;

describe("priceWords (spec default-prices 6.2, case DP-19)", () => {
  it("says a missing price is blocked, in the fixed words, never 'cost unknown'", () => {
    expect(priceWords(null)).toBe("No price: calls to this model are blocked");
    expect(NO_PRICE_WORDS).not.toMatch(/cost unknown/i);
  });

  it("reads a shipped price with its list date, and the age only when stale", () => {
    const shipped: ModelPrice = { ...base, origin: "shipped", retrievedOn: "2026-11-17", ageDays: 10, stale: false };
    expect(priceWords(shipped)).toBe("$2 in · $10 out per 1M tokens · shipped list, 17 Nov 2026");
    expect(priceWords({ ...shipped, ageDays: 91, stale: true })).toBe(
      "$2 in · $10 out per 1M tokens · shipped list, 17 Nov 2026 · list is 91 days old",
    );
  });

  it("reads an operator price and an override apart", () => {
    expect(priceWords({ ...base, origin: "operator" })).toBe("$2 in · $10 out per 1M tokens · set by your organization");
    expect(priceWords({ ...base, origin: "override", provisional: true })).toBe("$2 in · $10 out per 1M tokens (provisional)");
  });
});

describe("noPriceCount", () => {
  it("is null with every entry priced, and counts singular and plural", () => {
    expect(noPriceCount([entry("a/b", base)])).toBeNull();
    expect(noPriceCount([entry("a/b", null), entry("a/c", base)])).toBe("1 model has no price and is blocked");
    expect(noPriceCount([entry("a/b", null), entry("a/c", null)])).toBe("2 models have no price and are blocked");
  });
});

describe("cost unknown words (spec default-prices 6.2)", () => {
  it("are fixed, and never the no-price words", () => {
    expect(COST_UNKNOWN_WORDS).toBe("Cost unknown (charged the reservation)");
    expect(COST_UNKNOWN_WORDS).not.toMatch(/no price|blocked/i);
    expect(NO_PRICE_WORDS).not.toMatch(/cost unknown/i);
  });

  it("a total with an unknown cost says 'at least' with the server's exact amount", () => {
    expect(atLeastWords({ amount: "0.0670", currency: "USD" })).toBe("at least $0.06700000");
  });
});
