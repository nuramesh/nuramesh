import { describe, expect, it } from "vitest";
import type { LedgerEntry } from "@/api/types";
import type { AuditSpend } from "@/api/types";
import { costWords, missingParts } from "./task-audit-words";

const call = (fields: { label: string; value: string }[]): LedgerEntry => ({ kind: "model.called", cost: null, fields }) as unknown as LedgerEntry;

describe("costWords", () => {
  it("says cost unknown only when the entry's cost basis is unknown", () => {
    expect(costWords(call([{ label: "outcome", value: "ok" }, { label: "cost basis", value: "unknown" }]))).toBe("Cost unknown (charged the reservation)");
  });
  it("an ok call with no cost and no unknown basis was charged nothing: it stays 'price unknown'", () => {
    expect(costWords(call([{ label: "outcome", value: "ok" }]))).toBe("price unknown");
  });
  it("a failed call has no cost", () => {
    expect(costWords(call([{ label: "outcome", value: "error" }]))).toBe("no cost (call failed)");
  });
});

describe("missingParts", () => {
  const spend = (o: Partial<AuditSpend>): AuditSpend =>
    ({ calls: 0, priceUnknownCalls: 0, otherCurrencyCalls: 0, failedCallsNoCost: 0, unknownCostCalls: 0, costUnknownCalls: 0, ...o }) as AuditSpend;
  it("names calls with an unknown price apart from calls of unknown cost", () => {
    expect(missingParts(spend({ priceUnknownCalls: 3, costUnknownCalls: 1 }))).toEqual([
      "1 with cost unknown (charged the reservation)",
      "2 with unknown price",
    ]);
  });
  it("gives no unknown-price part when every price-unknown call is a cost-unknown one", () => {
    expect(missingParts(spend({ priceUnknownCalls: 1, costUnknownCalls: 1 }))).toHaveLength(1);
  });
  it("names failed calls with no cost", () => {
    expect(missingParts(spend({ failedCallsNoCost: 2 }))).toEqual(["2 failed, no cost"]);
  });
});
