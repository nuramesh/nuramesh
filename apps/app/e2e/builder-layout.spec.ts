import { expect, test } from "@playwright/test";

import { expectAgents, loadTemplate, nodes, openBuilder } from "./builder-helpers";

/** The layout of the org builder at the smallest desktop width, in both themes. */
for (const scheme of ["light", "dark"] as const) {
  test.describe(`builder layout, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme, viewport: { width: 1280, height: 800 } });

    test("fits at 1280x800: no horizontal scroll, Start fully visible", async ({ page }) => {
      await openBuilder(page);
      await expect(page.locator("html")).toHaveClass(scheme === "dark" ? /dark/ : /^(?!.*dark)/);
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);

      const widths = await page.evaluate(() => ({
        root: [document.documentElement.scrollWidth, document.documentElement.clientWidth],
        body: [document.body.scrollWidth, document.body.clientWidth],
        main: [document.querySelector("main")?.scrollWidth ?? 0, document.querySelector("main")?.clientWidth ?? 0],
      }));
      for (const [scrollWidth = 0, clientWidth = 0] of Object.values(widths)) expect(scrollWidth).toBeLessThanOrEqual(clientWidth);

      const start = page.getByRole("button", { name: "Start", exact: true });
      await expect(start).toBeVisible();
      const box = await start.boundingBox();
      if (box === null) throw new Error("Start has no box");
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(1280);
      expect(box.y + box.height).toBeLessThanOrEqual(800);

      // The palette, the canvas and Properties are all inside the window, and no role name is cut off.
      for (const locator of [
        page.getByRole("complementary", { name: "Add to the organization" }),
        page.getByRole("region", { name: "Organization canvas" }),
        page.getByRole("complementary", { name: "Details" }),
      ]) {
        const b = await locator.boundingBox();
        if (b === null) throw new Error("panel has no box");
        expect(b.x).toBeGreaterThanOrEqual(0);
        expect(b.x + b.width).toBeLessThanOrEqual(1280);
      }
      const clipped = await page.evaluate(() =>
        [...document.querySelectorAll<HTMLElement>("[data-role-id] > span")].filter((el) => el.scrollWidth > el.clientWidth).length,
      );
      expect(clipped).toBe(0);
    });

    test("agent cards carry the role colour, and full screen exits with Esc", async ({ page }) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
      await page.getByRole("button", { name: /^fit view$/i }).click();
      const bars = await nodes(page).evaluateAll((els) =>
        els.map((el) => el.querySelector("div")).map((card) => (card === null ? "" : getComputedStyle(card).borderLeftWidth)),
      );
      expect(bars.length).toBeGreaterThan(0);
      for (const width of bars) expect(width).toBe("3px");

      await page.getByRole("button", { name: /^Full screen/ }).click();
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeVisible();
      await expect(page.getByRole("button", { name: /^fit view$/i })).toHaveCount(1);
      await page.getByRole("button", { name: "Add agent" }).click();
      await expect(page.getByRole("complementary", { name: "Add to the organization" })).toBeVisible();
      await page.keyboard.press("Escape"); // the open drawer closes first
      await expect(page.getByRole("complementary", { name: "Add to the organization" })).toBeHidden();
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeHidden();
      await expect(page.getByRole("button", { name: /^Full screen/ })).toBeVisible();
    });
  });
}
