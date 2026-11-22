import { type Browser, chromium, expect, type Page, test } from "@playwright/test";

import { expectAgents, loadTemplate, node, openBuilder } from "./builder-helpers";
import { auditBuilder } from "./layout-audit";

/**
 * Every 20 px of CSS width from 800 to 1920, in both themes, in a Chromium with classic (always
 * visible, 15 px) scrollbars. Browser zoom changes the layout width only: a 1260 px window at 125%
 * is a 1008 px layout, at 110% 1145 px, so the CSS widths 800 to 1920 cover 100% (1024+), 110%
 * (931+) and 125% (819+) for every window from 1024 to 1920 px wide.
 */
const WIDTHS = Array.from({ length: 57 }, (_, i) => 800 + i * 20);
const HEIGHT = 700;
/** The widths are checked in 4 consecutive ranges per theme, so the tests can run on both workers. */
const CHUNKS = 4;
const chunkSize = Math.ceil(WIDTHS.length / CHUNKS);

let classic: Browser;
test.beforeAll(async () => {
  // Playwright hides scrollbars in headless Chromium by default; that hides a whole class of bugs.
  classic = await chromium.launch({ ignoreDefaultArgs: ["--hide-scrollbars"], args: ["--disable-features=OverlayScrollbar"] });
});
test.afterAll(async () => {
  await classic.close();
});

/** Sanity check of the audit itself: a page that is too wide must be reported. */
test("the audit catches an overflow (so a pass means something)", async () => {
  const context = await classic.newContext({ viewport: { width: 1100, height: HEIGHT }, baseURL: test.info().project.use.baseURL });
  const page = await context.newPage();
  await openBuilder(page, { palette: "default" });
  await page.evaluate(() => {
    const wide = document.createElement("div");
    wide.style.cssText = "width:2000px;height:10px;";
    document.getElementById("main")?.append(wide);
    const button = document.querySelector<HTMLElement>('[aria-label="Organization"] button');
    if (button !== null) button.style.marginLeft = "1200px";
  });
  const result = await auditBuilder(page);
  expect(result.violations.length).toBeGreaterThan(0);
  expect(result.violations.join("\n")).toContain("main overflows");
  await context.close();
});

async function audit(page: Page, label: string): Promise<string[]> {
  const result = await auditBuilder(page);
  return result.violations.map((v) => `${label}: ${v}`);
}

for (const scheme of ["light", "dark"] as const) {
  for (let chunk = 0; chunk < CHUNKS; chunk += 1) {
    const widths = WIDTHS.slice(chunk * chunkSize, (chunk + 1) * chunkSize);
    const [first, last] = [widths[0], widths[widths.length - 1]];
    test(`classic scrollbars: every 20 px from ${String(first)} to ${String(last)}, ${scheme} theme`, async () => {
      test.setTimeout(240_000);
      const context = await classic.newContext({ viewport: { width: 1280, height: HEIGHT }, colorScheme: scheme, baseURL: test.info().project.use.baseURL });
      const page = await context.newPage();
      await openBuilder(page, { palette: "default" });
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
      // An agent is selected, so Properties shows the full agent form (model, budget, rules, YAML).
      await page.locator('.react-flow__node[data-id="chief"]').click();

      // The browser really has a classic scrollbar: a page that scrolls loses width to it.
      await page.evaluate(() => {
        const probe = document.createElement("div");
        probe.id = "scroll-probe";
        probe.style.cssText = "position:absolute;top:0;left:0;width:1px;height:5000px;";
        document.body.append(probe);
        document.documentElement.style.overflowY = "scroll";
      });
      const probe = await page.evaluate(() => window.innerWidth - document.documentElement.clientWidth);
      expect(probe, "classic scrollbar takes width").toBeGreaterThan(0);
      await page.evaluate(() => {
        document.getElementById("scroll-probe")?.remove();
        document.documentElement.style.overflowY = "";
      });

      const problems: string[] = [];
      for (const width of widths) {
        await page.setViewportSize({ width, height: HEIGHT });
        const at = `${String(width)}px`;
        await node(page, "chief").waitFor({ state: "attached" }).catch(() => undefined);
        problems.push(...(await audit(page, `${at} nothing selected`)));
        const full = await page.getByRole("button", { name: "Full screen", exact: true }).boundingBox();
        const canvasBox = await page.getByRole("region", { name: "Organization canvas" }).boundingBox();
        if (full === null || canvasBox === null || full.x + full.width > canvasBox.x + canvasBox.width) problems.push(`${at}: Full screen is not inside the canvas`);
        const start = page.getByRole("button", { name: "Start", exact: true });
        const box = await start.boundingBox();
        if (box === null || box.x + box.width > (await page.evaluate(() => document.documentElement.clientWidth))) problems.push(`${at}: Start is not fully visible`);

        // Select an agent through its Properties
        const docked = width >= 1280;
        if (!docked) await page.getByRole("button", { name: "Properties", exact: true }).click();
        await page.getByRole("complementary", { name: "Details" }).waitFor({ state: "visible" });
        problems.push(...(await audit(page, `${at} ${docked ? "docked" : "drawer"} Properties`)));
        for (const tab of ["Roles", "Problems"]) {
          await page.getByRole("tab", { name: new RegExp(`^${tab}`) }).click();
          problems.push(...(await audit(page, `${at} ${tab} tab`)));
        }
        await page.getByRole("tab", { name: "Properties" }).click();
        if (!docked) {
          await page.keyboard.press("Escape");
          await page.getByRole("button", { name: "Palette", exact: true }).click();
          problems.push(...(await audit(page, `${at} Palette drawer`)));
          await page.keyboard.press("Escape");
        }
      }
      await context.close();
      expect(problems).toEqual([]);
    });
  }
}
