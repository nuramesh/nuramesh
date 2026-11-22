import { describe, expect, it } from "vitest";

import { contrast, cssBlock, opaque } from "../../../../brand/design-tokens";

// The shipped stylesheet as text: the test reads exactly what ships (same pattern as design-tokens.test.ts).
const sheets = import.meta.glob<string>("../index.css", { query: "?raw", import: "default", eager: true });
const css = Object.values(sheets)[0] ?? "";
const themes = { light: cssBlock(css, ":root {"), dark: cssBlock(css, ".dark {") } as const;
type Theme = keyof typeof themes;
const THEMES: Theme[] = ["light", "dark"];

/** The token's colour as `#rrggbb`; a translucent or transparent value is laid over `ground`. */
function colour(theme: Theme, name: string, ground = "#ffffff"): string {
  const value = themes[theme].get(`--t-${name}`);
  if (value === undefined) throw new Error(`${theme}: --t-${name} is missing`);
  return opaque(value, ground);
}
const ratio = (theme: Theme, fg: string, bg: string): number => {
  const ground = colour(theme, bg === "table-head-bg" ? "panel" : bg);
  return contrast(colour(theme, fg, ground), colour(theme, bg, colour(theme, "panel")));
};
const fail = (rows: [Theme, string, string, number, number][]): string[] =>
  rows.filter(([, , , r, min]) => r < min).map(([t, fg, bg, r, min]) => `${t}: ${fg} on ${bg} is ${r.toFixed(2)}:1, needs ${String(min)}:1`);

const SURFACES = ["bg", "panel", "panel2", "panel3"];

describe("contrast (light and dark, from the shipped CSS)", () => {
  it("control borders are at least 3:1 against every surface a control sits on", () => {
    const rows: [Theme, string, string, number, number][] = [];
    for (const theme of THEMES) {
      for (const border of ["border-strong", "pill-border"]) {
        for (const surface of SURFACES) rows.push([theme, border, surface, ratio(theme, border, surface), 3]);
      }
    }
    expect(fail(rows)).toEqual([]);
    expect(rows.length).toBe(16);
  });

  it("text is at least 4.5:1 on the surfaces it sits on", () => {
    const rows: [Theme, string, string, number, number][] = [];
    for (const theme of THEMES) {
      for (const text of ["text", "muted"]) {
        for (const surface of [...SURFACES, "canvas", "table-head-bg"]) rows.push([theme, text, surface, ratio(theme, text, surface), 4.5]);
      }
      for (const surface of ["bg", "panel", "panel2", "canvas", "table-head-bg"]) rows.push([theme, "small", surface, ratio(theme, "small", surface), 4.5]);
      rows.push([theme, "pill-text", "panel", ratio(theme, "pill-text", "panel"), 4.5]);
      rows.push([theme, "pill-on-text", "pill-on-bg", ratio(theme, "pill-on-text", "pill-on-bg"), 4.5]);
    }
    expect(fail(rows)).toEqual([]);
  });

  it("every status chip pair is at least 4.5:1, on the panel and on the page", () => {
    const rows: [Theme, string, string, number, number][] = [];
    for (const theme of THEMES) {
      for (const tone of ["success", "warning", "danger", "info", "neutral"]) {
        for (const ground of ["panel", "bg"]) {
          const bg = colour(theme, `chip-${tone}-bg`, colour(theme, ground));
          const fg = colour(theme, `chip-${tone}-text`, bg);
          rows.push([theme, `chip-${tone}-text`, `chip-${tone}-bg on ${ground}`, contrast(fg, bg), 4.5]);
        }
      }
    }
    expect(fail(rows)).toEqual([]);
    expect(rows.length).toBe(20);
  });

  it("the light chips are exactly the owner's palette", () => {
    const want: Record<string, [string, string, string]> = {
      success: ["#dcfce7", "#166534", "#86efac"],
      warning: ["#fef3c7", "#92400e", "#fcd34d"],
      danger: ["#fee2e2", "#991b1b", "#fca5a5"],
      info: ["#e0e7ff", "#3730a3", "#a5b4fc"],
      neutral: ["#f1f5f9", "#334155", "#cbd5e1"],
    };
    for (const [tone, [bg, text, border]] of Object.entries(want)) {
      expect([colour("light", `chip-${tone}-bg`), colour("light", `chip-${tone}-text`), colour("light", `chip-${tone}-border`)]).toEqual([bg, text, border]);
    }
  });

  it("the light surfaces and the primary button", () => {
    expect([colour("light", "bg"), colour("light", "panel"), colour("light", "canvas"), colour("light", "card-border"), colour("light", "border")]).toEqual([
      "#eef1f7",
      "#ffffff",
      "#e9edf5",
      "#cbd3e1",
      "#d5dbe7",
    ]);
    expect(contrast(colour("light", "btn-ink"), colour("light", "btn"))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(colour("dark", "btn-ink"), colour("dark", "btn"))).toBeGreaterThanOrEqual(4.5);
  });

  it("opaque() lays a translucent colour over its ground", () => {
    expect(opaque("rgb(255 0 0 / 50%)", "#000000")).toBe("#800000");
    expect(opaque("transparent", "#123456")).toBe("#123456");
    expect(() => opaque("hotpink", "#ffffff")).toThrow(/cannot read/);
  });
});
