import { expect, type Page, test } from "@playwright/test";

import { expectNoContrastViolations, expectNoSeriousA11yViolations } from "./a11y";

/**
 * The organization status bar never covers page content (E19, ND106). At desktop, tablet and 360 px
 * phone width, in every state, at several scroll offsets and after focus moves down the page, no
 * element of the page content may sit under the bar's rectangle, and none may be cut by its
 * bottom edge. The bar is part of the page flow: it scrolls away with the content instead of
 * pinning over it.
 */
const WIDTHS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "tablet", width: 820, height: 1000 },
  { name: "phone 360", width: 360, height: 740 },
];
const STATES: [string, string][] = [
  ["running", "/budgets"],
  ["halted (breaker tripped)", "/budgets?orgSafety=tripped"],
  ["halted (failing checks)", "/budgets?orgSafety=poison"],
  ["role paused (roles area open)", "/budgets?orgSafety=rolePaused"],
  ["viewer", "/budgets?orgSafety=viewer"],
];

/** Elements of `#main` whose box overlaps the bar's box (anything that is not the bar or holds it). */
async function covered(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const bar = document.querySelector('[aria-label="Organization status"]');
    const main = document.getElementById("main");
    if (bar === null || main === null) return ["bar or main missing"];
    const b = bar.getBoundingClientRect();
    const found: string[] = [];
    for (const el of main.querySelectorAll("*")) {
      if (bar.contains(el) || el.contains(bar)) continue;
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.bottom <= 0 || r.top >= innerHeight) continue; // not on screen at all
      if (r.right <= b.left || r.left >= b.right) continue;
      if (r.top < b.bottom - 1 && r.bottom > b.top + 1) found.push(`${el.tagName.toLowerCase()} ${el.textContent.trim().slice(0, 30)}`);
    }
    return found.slice(0, 5);
  });
}

async function scrollMain(page: Page, y: number): Promise<void> {
  await page.evaluate((top) => {
    const main = document.getElementById("main");
    if (main !== null) main.scrollTop = top;
    window.scrollTo(0, top);
  }, y);
  await page.waitForTimeout(50);
}

for (const size of WIDTHS) {
  test.describe(`bar never covers content, ${size.name}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    for (const [state, path] of STATES) {
      test(`${state}: nothing under the bar at any scroll offset`, async ({ page }) => {
        await page.goto(path);
        await expect(page.getByRole("region", { name: "Organization status" })).toBeVisible();
        for (const y of [0, 60, 150, 300, 700]) {
          await scrollMain(page, y);
          expect(await covered(page), `scrolled to ${String(y)} px`).toEqual([]);
        }
      });
    }

    test("halted: focus moving down the page leaves the focused control clear of the bar", async ({ page }) => {
      await page.goto("/budgets?orgSafety=tripped");
      await expect(page.getByRole("region", { name: "Organization status" })).toBeVisible();
      await page.getByRole("link", { name: "View runs" }).focus();
      expect(await covered(page), "after focusing View runs").toEqual([]);
      await page.getByRole("button", { name: "Reset breaker denied-loop" }).focus();
      expect(await covered(page), "after focusing the last breaker button").toEqual([]);
    });

    test("halted, halt dialog closed and open: still nothing under the bar, contrast and axe green", async ({ page }) => {
      await page.goto("/budgets?orgSafety=tripped");
      await expect(page.getByRole("region", { name: "Organization status" })).toBeVisible();
      expect(await covered(page)).toEqual([]);
      await expectNoContrastViolations(page, `halted bar (${size.name})`);
      await expectNoSeriousA11yViolations(page);
      await page.getByRole("button", { name: "Resume organization" }).click();
      await expect(page.getByRole("dialog", { name: "Resume the organization?" })).toBeVisible();
      expect(await covered(page)).toEqual([]);
    });
  });
}
