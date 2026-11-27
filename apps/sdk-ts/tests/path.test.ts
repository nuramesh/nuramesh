import { describe, expect, it } from "vitest";
import { fillPath } from "../src/path.js";

const OLD = (p: string, f: (n: string) => string): string =>
  p.replace(/\{([^}]+)\}/g, (_, n: string) => f(n));
const upper = (n: string): string => `<${n.toUpperCase()}>`;

describe("fillPath", () => {
  const cases = [
    "/runs/{id}",
    "/a/{x}/b/{y}/c",
    "/{x}/{x}",
    "/none",
    "",
    "/{}",
    "/{}/{a}",
    "/{{a}",
    "/{a{b}c}",
    "/{a",
    "/a}",
    "{a}{b}",
    "/{a}}/{",
    "}{a}",
  ];
  it.each(cases)("matches the old regex on %j", (p) => {
    expect(fillPath(p, upper)).toBe(OLD(p, upper));
  });

  it("is linear on hostile input and still correct", () => {
    // Expected values are computed by hand: the old regex needs seconds on these (quadratic).
    const hostile: [string, string][] = [
      ["{".repeat(100_000), "{".repeat(100_000)],
      ["{a".repeat(100_000), "{a".repeat(100_000)],
      ["}".repeat(100_000), "}".repeat(100_000)],
      [
        "{".repeat(50_000) + "x" + "}".repeat(50_000),
        `<${"{".repeat(49_999)}X>` + "}".repeat(49_999),
      ],
    ];
    for (const [p, want] of hostile) {
      const t0 = performance.now();
      const got = fillPath(p, upper);
      expect(performance.now() - t0).toBeLessThan(200);
      expect(got).toBe(want);
    }
  });
});
