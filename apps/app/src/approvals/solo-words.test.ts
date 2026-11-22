import { describe, expect, it } from "vitest";

import { ProblemError } from "@/api/client";
import { usd } from "@/api/money";

import { budgetLeft, isHighRisk, soloRefusal } from "./solo-words";

describe("solo words", () => {
  it("the high-risk kinds are the server's six (spec section 1)", () => {
    for (const kind of ["spend", "external_message", "merge", "run_command", "network", "use_tool"]) expect(isHighRisk(kind)).toBe(true);
    for (const kind of ["write_file", "call_model", "delegate", "", "toString"]) expect(isHighRisk(kind)).toBe(false);
  });

  it("a refusal's words, by code; anything else is not a solo refusal", () => {
    expect(soloRefusal(new ProblemError("x", 403, "acknowledge_required"))).toMatch(/Reload the page/);
    expect(soloRefusal(new ProblemError("x", 429, "solo_rate_limited"))).toMatch(/Wait a little and try again\.$/);
    expect(soloRefusal(new ProblemError("x", 429, "solo_rate_limited", 90))).toMatch(/Try again in 1 minute 30 seconds\.$/);
    expect(soloRefusal(new ProblemError("x", 403, "forbidden"))).toBeNull();
    expect(soloRefusal(new Error("x"))).toBeNull();
  });

  it("budget left is exact, never a guess", () => {
    expect(budgetLeft({ spent: usd("1.84"), limit: usd("50.00") })).toBe("$48.16");
    expect(budgetLeft({ spent: usd("0.10"), limit: usd("0.30") })).toBe("$0.20");
    expect(budgetLeft({ spent: usd("51"), limit: usd("50") })).toBe("-$1.00");
    expect(budgetLeft({ spent: usd("1"), limit: null })).toBe("no run budget recorded");
    expect(budgetLeft(null)).toBe("not available");
    expect(budgetLeft({ spent: usd("1"), limit: { amount: "2", currency: "EUR" } })).toBe("not available");
  });
});
