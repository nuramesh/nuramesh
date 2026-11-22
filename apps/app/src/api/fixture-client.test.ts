import { describe, expect, it } from "vitest";

import { runClientContract } from "./contract";
import { createFixtureClient } from "./fixture-client";
import { chooseSource, createApiClient } from "./index";
import { addMoney, compareMoney, formatMoney, fromMicros, toMicros, usd, percentUsed } from "./money";


runClientContract("fixtures", () => createFixtureClient());

describe("choosing the data source", () => {
  it("uses fixtures in development and the server in a production build", () => {
    expect(chooseSource(undefined, true)).toBe("fixtures");
    expect(chooseSource(undefined, false)).toBe("server");
    expect(chooseSource("", false)).toBe("server");
  });
  it("accepts an explicit choice and refuses anything else", () => {
    expect(chooseSource("fixtures", false)).toBe("fixtures");
    expect(chooseSource("server", true)).toBe("server");
    expect(() => chooseSource("fixture", true)).toThrow(/must be/);
  });
  it("builds the client for the source", async () => {
    expect((await createApiClient("fixtures")).source).toBe("fixtures");
    expect((await createApiClient("server")).source).toBe("server");
  });
});

describe("money", () => {
  it("adds exactly where floats would not", () => {
    expect(addMoney(usd("0.1"), usd("0.2")).amount).toBe("0.30");
    expect(addMoney(usd("0.000001"), usd("0.000002")).amount).toBe("0.000003");
    expect(fromMicros(toMicros(usd("1234.5"))).amount).toBe("1234.50");
  });
  it("compares, formats and takes percentages", () => {
    expect(compareMoney(usd("1.10"), usd("1.1"))).toBe(0);
    expect(compareMoney(usd("2"), usd("10"))).toBe(-1);
    expect(formatMoney(usd("42.1"))).toBe("$42.10");
    expect(formatMoney(usd("1234567.891"))).toBe("$1,234,567.891");
    expect(formatMoney(usd("0.0003"))).toBe("$0.0003");
    expect(percentUsed(usd("50"), usd("50"))).toBe(100);
    expect(percentUsed(usd("60"), usd("50"))).toBe(100);
    expect(percentUsed(usd("1"), usd("0"))).toBe(0);
  });
  it("refuses malformed amounts", () => {
    for (const bad of ["", "1.", ".5", "1e3", "1,5", "$2", "1.1234567", "--1"]) {
      expect(() => usd(bad), bad).toThrow(RangeError);
    }
  });
});
