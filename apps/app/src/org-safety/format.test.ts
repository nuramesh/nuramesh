import { describe, expect, it } from "vitest";

import { seedOrgSafety } from "@/api/fixtures/org-safety";
import type { BreakerRow } from "@/api/types";

import { actionLabel, capped, haltedLine, localTime, observedText, pausedLine, relativeAgo, signalLabel, soloRemovalLine, windowLabel } from "./format";

const row = (over: Partial<BreakerRow>): BreakerRow => ({ ...seedOrgSafety().breakers[0]!, ...over });

describe("windowLabel", () => {
  it.each([
    [3600, "1 hour"],
    [7200, "2 hours"],
    [600, "10 minutes"],
    [60, "1 minute"],
    [45, "45 seconds"],
    [1, "1 second"],
    [5400, "90 minutes"],
  ])("%i seconds is %s", (seconds, text) => {
    expect(windowLabel(seconds)).toBe(text);
  });
});

describe("labels", () => {
  it("names the five signals and the role scope", () => {
    expect(signalLabel("spend", null)).toBe("Spend");
    expect(signalLabel("policy_denied", null)).toBe("Denied actions");
    expect(signalLabel("gate_failed", null)).toBe("Failed gates");
    expect(signalLabel("model_blocked", null)).toBe("Blocked model calls");
    expect(signalLabel("model_called", "writer")).toBe("Model calls for role writer");
    expect(signalLabel(null, null)).toBe("");
  });
  it("names the two actions", () => {
    expect(actionLabel("halt_org", null)).toBe("Halt organization");
    expect(actionLabel("pause_role", "writer")).toBe("Pause role writer");
    expect(actionLabel(null, null)).toBe("");
  });
});

describe("observedText", () => {
  it("writes counts as events and spend as USD", () => {
    expect(observedText(row({ signal: "policy_denied", observed: "4", limit: "20" }))).toBe("4 of 20 events");
    expect(observedText(row({ signal: "spend", observed: "3.5", limit: "25" }))).toBe("3.5 of 25 USD");
  });
  it("marks a recorded trip and counts calls with unknown cost, never guessing their price", () => {
    expect(observedText(row({ signal: "policy_denied", observed: "21", limit: "20", observedAs: "at_trip" }))).toBe("21 of 20 events at trip");
    expect(observedText(row({ signal: "spend", observed: "3.5", limit: "25", unknownCostCalls: 2 }))).toBe("3.5 of 25 USD + 2 calls with unknown cost");
    expect(observedText(row({ signal: "spend", observed: "3.5", limit: "25", unknownCostCalls: 1 }))).toBe("3.5 of 25 USD + 1 call with unknown cost");
  });
  it("is empty for a rule with nothing to compute", () => {
    expect(observedText(row({ signal: null, observed: null, limit: null }))).toBe("");
  });
});

describe("times", () => {
  it("says how long ago, in the largest whole unit", () => {
    const now = Date.parse("2026-11-01T12:00:00Z");
    expect(relativeAgo("2026-11-01T11:59:56Z", now)).toBe("4 seconds");
    expect(relativeAgo("2026-11-01T11:59:00Z", now)).toBe("1 minute");
    expect(relativeAgo("2026-11-01T10:00:00Z", now)).toBe("2 hours");
    expect(relativeAgo("2026-10-29T12:00:00Z", now)).toBe("3 days");
    expect(relativeAgo("2026-11-01T12:00:05Z", now)).toBe("0 seconds"); // a clock ahead is never negative
  });
  it("shows today's time without a date, another day with one", () => {
    const now = new Date(2026, 10, 1, 15, 0, 0);
    expect(localTime(new Date(2026, 10, 1, 9, 5).toISOString(), now)).toBe("09:05");
    expect(localTime(new Date(2026, 9, 21, 16, 5).toISOString(), now)).toBe("21 Oct 16:05");
    expect(localTime("not a date", now)).toBe("not a date");
  });
});

describe("halted and paused lines", () => {
  const now = new Date(2026, 10, 1, 15, 0, 0);
  const at = new Date(2026, 10, 1, 11, 58).toISOString();
  const base = seedOrgSafety("halted").halt;
  it("names who halted it, as given, never an email", () => {
    expect(haltedLine({ ...base, at, by: "human:ann", cause: "human" }, now)).toBe("human:ann halted it at 11:58.");
  });
  it("names the breaker", () => {
    expect(haltedLine({ ...base, at, cause: "breaker", rule: "denied-loop" }, now)).toBe("Breaker denied-loop halted it at 11:58.");
  });
  it("names the error class, the phase and the rule of a failing check", () => {
    const poison = { ...seedOrgSafety("poison").halt, at };
    expect(haltedLine(poison, now)).toBe("Breaker checks were failing, so it was halted at 11:58. Cause: KeyError while evaluating rule spend-spike, after 3 failed checks.");
    expect(haltedLine({ ...poison, phase: "read", failingRule: null }, now)).toBe("Breaker checks were failing, so it was halted at 11:58. Cause: KeyError while reading, after 3 failed checks.");
    expect(haltedLine({ ...poison, phase: "write" }, now)).toContain("while writing rule spend-spike");
    expect(haltedLine({ ...poison, phase: "timeout", errorClass: "TickTimeout" }, now)).toContain("Cause: TickTimeout rule spend-spike");
  });
  it("says who paused a role", () => {
    const role = seedOrgSafety("rolePaused").halt.roles[0]!;
    expect(pausedLine({ ...role, at }, now)).toBe("human:ann paused it at 11:58");
    expect(pausedLine({ ...role, at, cause: "breaker", rule: "denied-loop" }, now)).toBe("Breaker denied-loop paused it at 11:58");
  });
});

describe("soloRemovalLine (solo approval SA-18 item 4)", () => {
  it("is shown whenever the count is above 0, and not otherwise", () => {
    expect(soloRemovalLine(0)).toBeNull();
    expect(soloRemovalLine(1)).toBe("solo approvals within 7 days of an approver being removed: 1");
    expect(soloRemovalLine(12)).toBe("solo approvals within 7 days of an approver being removed: 12");
  });
});

describe("capped", () => {
  it("keeps three lines and counts the rest", () => {
    expect(capped(["a", "b", "c"])).toEqual(["a", "b", "c"]);
    expect(capped(["a", "b", "c", "d", "e"])).toEqual(["a", "b", "c", "and 2 more"]);
  });
});
