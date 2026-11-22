import { describe, expect, it } from "vitest";

import { soloLines } from "./solo-line";

describe("soloLines (SA-18 item 2)", () => {
  it.each([
    [5, 3, "0.6000", ["approved asks: 5 (solo 3, 60 %)", "no second person reviewed the solo ones"]],
    [3, 1, "0.3333", ["approved asks: 3 (solo 1, 33.33 %)", "no second person reviewed the solo ones"]],
    [8, 1, "0.1250", ["approved asks: 8 (solo 1, 12.5 %)", "no second person reviewed the solo ones"]],
    [30, 30, "1.0000", ["approved asks: 30 (solo 30, 100 %)", "no second person reviewed the solo ones"]],
    [4, 0, "0.0000", ["approved asks: 4 (solo 0, 0 %)"]],
    [0, 0, null, ["approved asks: 0 (solo 0)"]],
  ] as const)("%i approved, %i solo, share %s", (approved, solo, share, expected) => {
    expect(soloLines(approved, solo, share)).toEqual(expected);
  });
});
