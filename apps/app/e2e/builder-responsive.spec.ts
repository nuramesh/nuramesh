import { expect, type Page, test } from "@playwright/test";

import { expectAgents, loadTemplate, openBuilder } from "./builder-helpers";

const WIDTHS = [1920, 1600, 1440, 1280, 1260, 1100, 900, 768];
const HEIGHT = 800;

const canvas = (page: Page) => page.getByRole("region", { name: "Organization canvas" });
const details = (page: Page) => page.getByRole("complementary", { name: "Details" });
const palette = (page: Page) => page.getByRole("complementary", { name: "Add to the organization" });

async function overflow(page: Page): Promise<Record<string, number>> {
  return page.evaluate(() => {
    const main = document.querySelector("main");
    return {
      html: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      body: document.body.scrollWidth - document.body.clientWidth,
      main: (main?.scrollWidth ?? 0) - (main?.clientWidth ?? 0),
    };
  });
}

async function expectNoOverflow(page: Page, label: string): Promise<void> {
  for (const [name, extra] of Object.entries(await overflow(page))) expect(extra, `${label}: ${name}`).toBeLessThanOrEqual(0);
}

/** Every visible button in the header and toolbar lies inside the window. */
async function expectControlsInside(page: Page, width: number): Promise<void> {
  const outside = await page.evaluate((w) => {
    const bad: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>('[aria-label="Organization"] button, [aria-label="Canvas"] button')) {
      if (el.offsetParent === null) continue;
      const box = el.getBoundingClientRect();
      if (box.left < 0 || box.right > w || box.width === 0) bad.push(`${el.textContent}|${el.getAttribute("aria-label") ?? ""}`);
    }
    return bad;
  }, width);
  expect(outside).toEqual([]);
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`builder responsive layout, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    for (const width of WIDTHS) {
      test(`${String(width)} px: nothing is cut off, panels follow the width`, async ({ page }) => {
        await page.setViewportSize({ width, height: HEIGHT });
        await openBuilder(page, { palette: "default" });
        await expect(page.locator("html")).toHaveClass(scheme === "dark" ? /dark/ : /^(?!.*dark)/);
        await loadTemplate(page, "Dev team");
        await expectAgents(page, 5);
        await expectNoOverflow(page, "docked");
        await expectControlsInside(page, width);

        const start = page.getByRole("button", { name: "Start", exact: true });
        await expect(start).toBeVisible();
        const box = await start.boundingBox();
        if (box === null) throw new Error("Start has no box");
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width);
        expect(box.y + box.height).toBeLessThanOrEqual(HEIGHT);

        const canvasBox = await canvas(page).boundingBox();
        if (canvasBox === null) throw new Error("canvas has no box");
        expect(canvasBox.x + canvasBox.width).toBeLessThanOrEqual(width);
        if (width === 1280) expect(canvasBox.width).toBeGreaterThan(400);

        if (width >= 1280) {
          // Properties is docked on the right; the palette is a rail at 1280 to 1599 and open from 1600
          await expect(details(page)).toBeVisible();
          const details_ = await details(page).boundingBox();
          expect(details_?.x ?? 0).toBeGreaterThanOrEqual(canvasBox.x + canvasBox.width - 1);
          expect(details_?.width ?? 0).toBeGreaterThan(280);
          expect(await palette(page).boundingBox().then((b) => b?.width ?? 0)).toBeLessThan(width < 1600 ? 60 : 300);
          await expect(page.getByRole("button", { name: "Palette", exact: true })).toHaveCount(0);
        } else {
          // both are overlay drawers, closed until opened, and they never push the canvas
          await expect(details(page)).toBeHidden();
          await expect(palette(page)).toBeHidden();
          for (const [name, panel] of [
            ["Properties", details(page)],
            ["Palette", palette(page)],
          ] as const) {
            await page.getByRole("button", { name, exact: true }).click();
            await expect(panel).toBeVisible();
            const open = await canvas(page).boundingBox();
            expect(open?.width).toBe(canvasBox.width);
            await expectNoOverflow(page, `${name} drawer`);
            const panelBox = await panel.boundingBox();
            expect((panelBox?.x ?? 0) + (panelBox?.width ?? 0)).toBeLessThanOrEqual(width);
            await page.keyboard.press("Escape");
            await expect(panel).toBeHidden();
            await expect(page.getByRole("button", { name, exact: true })).toBeFocused();
          }
        }
      });
    }

    test("1280 px: the palette is an icon rail with hover labels, and expands on click", async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: HEIGHT });
      await openBuilder(page, { palette: "default" });
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
      const rail = await palette(page).boundingBox();
      expect(rail?.width ?? 0).toBeLessThan(60);
      const checks = page.getByRole("button", { name: "Checks: expand the palette" });
      await checks.hover();
      await expect(page.getByText("Checks: expand the palette", { exact: true })).toBeVisible();
      await expect(page.getByText("Checks: expand the palette", { exact: true })).toHaveCSS("opacity", "1");
      await checks.click();
      await expect(palette(page).getByRole("button", { name: /Chief of Staff/ })).toBeVisible();
      expect(await palette(page).boundingBox().then((b) => b?.width ?? 0)).toBeGreaterThan(200);
      await expectNoOverflow(page, "expanded rail");
      await page.getByRole("button", { name: "Collapse the palette to a rail" }).click();
      expect(await palette(page).boundingBox().then((b) => b?.width ?? 0)).toBeLessThan(60);
    });

    for (const [width, stored, expanded] of [
      [1440, "1", true],
      [1920, "0", false],
      [1920, null, true],
      [1440, null, false],
    ] as const) {
      test(`${String(width)} px, stored palette choice ${String(stored)}: ${expanded ? "expanded" : "icon rail"}`, async ({ page }) => {
        await page.addInitScript((value) => {
          if (value !== null) window.localStorage.setItem("nuramesh.builder.palette-expanded", value);
        }, stored);
        await page.setViewportSize({ width, height: HEIGHT });
        await openBuilder(page, { palette: "default" });
        const box = await palette(page).boundingBox();
        if (expanded) expect(box?.width ?? 0).toBeGreaterThan(200);
        else expect(box?.width ?? 0).toBeLessThan(60);
      });
    }

    test("1100 px: a click outside closes a drawer, and so does its close button", async ({ page }) => {
      await page.setViewportSize({ width: 1100, height: HEIGHT });
      await openBuilder(page, { palette: "default" });
      await loadTemplate(page, "Dev team");
      await page.getByRole("button", { name: "Properties", exact: true }).click();
      await expect(details(page)).toBeVisible();
      await page.getByTestId("drawer-backdrop").click({ position: { x: 20, y: 200 } });
      await expect(details(page)).toBeHidden();
      await page.getByRole("button", { name: "Palette", exact: true }).click();
      await expect(palette(page)).toBeVisible();
      await page.getByRole("button", { name: "Close palette" }).click();
      await expect(palette(page)).toBeHidden();
    });

    test("full screen reuses the drawers: Esc closes the drawer first, then full screen", async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      await openBuilder(page, { palette: "default" });
      await loadTemplate(page, "Dev team");
      await page.getByRole("button", { name: /^Full screen/ }).click();
      await page.getByRole("button", { name: "Properties", exact: true }).click();
      await expect(details(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(details(page)).toBeHidden();
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeHidden();
    });
  });
}
