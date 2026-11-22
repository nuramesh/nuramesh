import { describe, expect, it } from "vitest";

import {
  type AppCss,
  contrastResults,
  cssBlock,
  logoSizes,
  parseChecks,
  parseTokens,
  syncProblems,
} from "../../../../brand/design-tokens";
import { LOGO_MIN_WIDTH, TILE_COLOR, TILE_COLOR_DARK, TILE_MARK_COLOR, TILE_MARK_COLOR_DARK, logoWidth } from "../../../../brand/logo-metrics";

// The stylesheet and the design doc as text (`?raw`): the test reads exactly what ships.
const sheets = import.meta.glob<string>("../index.css", { query: "?raw", import: "default", eager: true });
const docs = import.meta.glob<string>("../../../../docs/design/DESIGN-TOKENS.md", {
  query: "?raw",
  import: "default",
  eager: true,
});
const logoUsers = import.meta.glob<string>(["../components/sidebar.tsx", "../components/app-shell.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const css = Object.values(sheets)[0] ?? "";
const doc = Object.values(docs)[0] ?? "";

const appCss: AppCss = { light: cssBlock(css, ":root {"), dark: cssBlock(css, ".dark {") };
const tokens = parseTokens(doc);
const checks = parseChecks(doc);

describe("design tokens (docs/design/DESIGN-TOKENS.md is the source of truth)", () => {
  it("the doc has its tables", () => {
    expect(tokens.length).toBe(75);
    expect(checks.length).toBe(49);
  });

  it("a malformed contrast row throws instead of being skipped", () => {
    const bad = "| Check | a | b | Min | Themes | Apps | Note |\n|---|---|---|---|---|---|---|\n| x | text | bg | abc | light | app | |\n";
    expect(() => parseChecks(bad)).toThrow(/malformed/);
    expect(() => parseChecks(bad.replace("abc", "4.5").replace("light", "sepia"))).toThrow(/bad themes/);
  });

  it("index.css agrees with the doc, in light and dark", () => {
    expect(syncProblems("app", tokens, appCss)).toEqual([]);
  });

  it("every contrast check passes in both themes (text 4.5:1, borders and lines 3:1)", () => {
    const results = contrastResults("app", tokens, checks, appCss);
    expect(results.length).toBeGreaterThanOrEqual(40);
    const failing = results
      .filter((r) => r.ratio < r.min)
      .map((r) => `${r.theme}: ${r.check} is ${r.ratio.toFixed(2)}:1, needs ${String(r.min)}:1`);
    expect(failing).toEqual([]);
  });
});

describe("logo", () => {
  it("the theme variables are the Ink pair of brand/logo-metrics.ts: ink box and white line on light, paper box and ink line on dark", () => {
    expect([appCss.light.get("--logo-tile"), appCss.light.get("--logo-mark")]).toEqual(["#141833", "#ffffff"]);
    expect([appCss.dark.get("--logo-tile"), appCss.dark.get("--logo-mark")]).toEqual(["#f3f4f8", "#141833"]);
    expect([TILE_COLOR, TILE_MARK_COLOR, TILE_COLOR_DARK, TILE_MARK_COLOR_DARK].map((c) => c.toLowerCase())).toEqual(["#141833", "#ffffff", "#f3f4f8", "#141833"]);
    expect(css).not.toContain("logo-outline");
  });

  it("every logo in the app is at least the kit's 100px minimum wide", () => {
    const files = Object.entries(logoUsers);
    expect(files.length).toBe(2);
    for (const [file, source] of files) {
      const sizes = logoSizes(source);
      expect(sizes.length, file).toBeGreaterThan(0);
      for (const size of sizes) expect(logoWidth(size), `${file} size ${String(size)}`).toBeGreaterThanOrEqual(LOGO_MIN_WIDTH);
    }
  });
});
