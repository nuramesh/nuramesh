import { expect, type Page, test } from "@playwright/test";

import { expectAgents, loadTemplate, openBuilder } from "./builder-helpers";

const root = (page: Page) => page.getByTestId("builder-root");
const viewport = (page: Page) => page.locator(".react-flow__viewport").evaluate((el) => (el as HTMLElement).style.transform);
const sidebar = (page: Page) => page.locator("#app-sidebar");

/** True when the user can see the element: it has a box and nothing else is drawn over its centre. */
async function shown(page: Page, selector: string): Promise<boolean> {
  return page.evaluate((sel) => {
    const el = document.querySelector<HTMLElement>(sel);
    if (el === null) return false;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) return false;
    const top = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return top !== null && el.contains(top);
  }, selector);
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`builder full screen, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme, viewport: { width: 1440, height: 900 } });

    test("the canvas covers the window, hides every panel, keeps the view, and Esc leaves", async ({ page }) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
      // the person adjusts the view: zoom in once, then it must survive entering and leaving
      await page.getByRole("button", { name: "Zoom in" }).click();
      await page.waitForTimeout(400);
      const before = await viewport(page);

      await page.getByRole("button", { name: /^Full screen/ }).click();
      await expect(page.getByRole("button", { name: "Exit full screen" })).toBeVisible();
      // the browser's Fullscreen API was used, or the fixed overlay is the fallback
      const mode = await page.evaluate(() => (document.fullscreenElement !== null ? "api" : "overlay"));
      const box = await root(page).boundingBox();
      expect(box).toEqual({ x: 0, y: 0, width: 1440, height: 900 });
      console.log(`full-screen mode in this browser: ${mode}`);
      expect(await shown(page, "#app-sidebar")).toBe(false);
      expect(await shown(page, "#root header")).toBe(false);
      await expect(page.getByRole("complementary", { name: "Details" })).toBeHidden();
      await expect(page.getByRole("complementary", { name: "Add to the organization" })).toBeHidden();
      await expect(page.getByRole("group", { name: "Organization" })).toBeHidden();
      await expect(page.getByRole("toolbar", { name: "Canvas", exact: true })).toBeHidden();
      expect(await viewport(page)).toBe(before);

      // the floating toolbar has every action
      for (const name of ["Add agent", "Properties", "Zoom in", "Zoom out", "Fit view", "Auto-layout"]) {
        await expect(page.getByRole("toolbar", { name: "Full-screen canvas" }).getByRole("button", { name })).toBeVisible();
      }
      // the way out is the canvas corner button, not part of the toolbar
      await expect(page.getByRole("toolbar", { name: "Full-screen canvas" }).getByRole("button", { name: "Exit full screen" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Exit full screen" })).toHaveCount(1);
      await page.getByRole("button", { name: "Zoom out" }).click();
      await page.waitForTimeout(400);
      const zoomed = await viewport(page);
      expect(zoomed).not.toBe(before);

      // Properties with nothing selected shows the organization's properties and selects nothing
      await page.getByRole("button", { name: "Properties", exact: true }).click();
      await expect(page.getByRole("complementary", { name: "Details" })).toBeVisible();
      await expect(page.getByTestId("builder-summary")).toBeHidden(); // status bar hidden too
      await page.keyboard.press("Escape"); // the drawer closes first
      await expect(page.getByRole("complementary", { name: "Details" })).toBeHidden();
      await expect(root(page)).toHaveAttribute("data-fullscreen", "true");

      await page.keyboard.press("Escape");
      await expect(root(page)).toHaveAttribute("data-fullscreen", "false");
      await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);
      await expect(sidebar(page)).toBeVisible();
      expect(await shown(page, "#app-sidebar")).toBe(true);
      // zoom and position are exactly what they were when leaving
      expect(await viewport(page)).toBe(zoomed);
    });

    test("a view the person never moved is refitted to the bigger canvas; all agents stay inside the window", async ({ page }) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
      await page.getByRole("button", { name: /^Full screen/ }).click();
      await expect(root(page)).toHaveAttribute("data-fullscreen", "true");
      await page.waitForTimeout(700);
      const boxes = await page.locator(".react-flow__node-agent").evaluateAll((els) =>
        els.map((el) => {
          const b = el.getBoundingClientRect();
          return { left: b.left, right: b.right, top: b.top, bottom: b.bottom };
        }),
      );
      expect(boxes).toHaveLength(5);
      for (const b of boxes) {
        expect(b.left).toBeGreaterThanOrEqual(0);
        expect(b.right).toBeLessThanOrEqual(1440);
        expect(b.bottom).toBeLessThanOrEqual(900);
      }
      // and the organization fills the width instead of sitting small in a corner
      expect(Math.max(...boxes.map((b) => b.right)) - Math.min(...boxes.map((b) => b.left))).toBeGreaterThan(900);
    });

    test("leaving full screen with the browser (not our button) syncs the state", async ({ page }) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await page.getByRole("button", { name: /^Full screen/ }).click();
      await expect(root(page)).toHaveAttribute("data-fullscreen", "true");
      const native = await page.evaluate(() => document.fullscreenElement !== null);
      test.skip(!native, "this browser build did not enter real full screen; the overlay fallback is covered above");
      await page.evaluate(() => document.exitFullscreen());
      await expect(root(page)).toHaveAttribute("data-fullscreen", "false");
      await expect(page.getByRole("button", { name: /^Full screen/ })).toBeVisible();
    });

    test("Add agent opens the palette as a drawer over the canvas", async ({ page }) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await page.getByRole("button", { name: /^Full screen/ }).click();
      await page.getByRole("button", { name: "Add agent" }).click();
      await expect(page.getByRole("complementary", { name: "Add to the organization" })).toBeVisible();
    });
  });
}

test.describe("the Full screen button lives in the canvas top-right corner", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("it is in the corner with a tooltip, not in the toolbar, and becomes Exit full screen", async ({ page }) => {
    await openBuilder(page);
    await loadTemplate(page, "Dev team");
    await expectAgents(page, 5);
    await expect(page.getByRole("toolbar", { name: "Canvas", exact: true }).getByRole("button", { name: /full screen/i })).toHaveCount(0);

    const canvas = page.getByRole("region", { name: "Organization canvas" });
    const button = page.getByRole("button", { name: "Full screen", exact: true });
    const [c, b] = [await canvas.boundingBox(), await button.boundingBox()];
    if (c === null || b === null) throw new Error("no box");
    expect(b.x + b.width).toBeLessThanOrEqual(c.x + c.width);
    expect(c.x + c.width - (b.x + b.width)).toBeLessThan(24);
    expect(b.y - c.y).toBeLessThan(24);
    expect(b.y).toBeGreaterThanOrEqual(c.y);
    expect(await button.innerHTML()).toContain("maximize-2");

    await button.hover();
    await expect(page.locator(".bg-popover").filter({ hasText: "Full screen (F)" })).toBeVisible();

    await button.click();
    const exit = page.getByRole("button", { name: "Exit full screen" });
    await expect(exit).toBeVisible();
    expect(await exit.innerHTML()).toContain("minimize-2");
    const e = await exit.boundingBox();
    expect(e?.x).toBeCloseTo(1440 - 12 - (e?.width ?? 0), 0);
    expect(e?.y).toBeCloseTo(12, 0);
    await exit.click();
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");
    await expect(button).toBeFocused();
  });

  test("F toggles full screen while the canvas has focus, and only then", async ({ page }) => {
    await openBuilder(page);
    await loadTemplate(page, "Dev team");
    await expectAgents(page, 5);

    // focus is elsewhere (the Templates button): F does nothing
    await page.getByRole("button", { name: "Templates" }).focus();
    await page.keyboard.press("f");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");

    // typing an f in a text field never toggles it
    await page.locator(".react-flow__node").first().click();
    const name = page.getByLabel("Name", { exact: true });
    await name.focus();
    await page.keyboard.type("f");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");

    // a modifier key held: no toggle
    await page.locator(".react-flow__pane").click({ position: { x: 40, y: 200 } });
    await page.keyboard.press("Control+f");
    await page.keyboard.press("Shift+F");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");

    // canvas focused: F enters, F leaves again; Esc also leaves
    await page.keyboard.press("f");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "true");
    await page.keyboard.press("f");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");
    await page.locator(".react-flow__pane").click({ position: { x: 40, y: 200 } });
    await page.keyboard.press("f");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "true");
    await page.keyboard.press("Escape");
    await expect(root(page)).toHaveAttribute("data-fullscreen", "false");
  });
});

test("the canvas corner controls have names and visible tooltips; fit view is not the full-screen icon", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openBuilder(page);
  for (const name of ["Zoom in", "Zoom out", "Fit view"]) {
    const button = page.getByRole("button", { name });
    await button.hover();
    await expect(page.locator(".bg-popover").filter({ hasText: name })).toBeVisible();
  }
  const fit = await page.getByRole("button", { name: "Fit view" }).innerHTML();
  const full = await page.getByRole("button", { name: /^Full screen/ }).innerHTML();
  expect(fit).toContain("scan-search");
  expect(full).toContain("maximize-2");
});

test("agent model chips are never truncated", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openBuilder(page);
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  const chips = page.getByTestId("model-chip");
  await expect(chips.first()).toBeVisible();
  const clipped = await chips.evaluateAll((els) => els.filter((el) => el.scrollWidth > el.clientWidth).map((el) => el.textContent));
  expect(clipped).toEqual([]);
  const styles = await chips.evaluateAll((els) => els.map((el) => getComputedStyle(el).textOverflow));
  for (const overflow of styles) expect(overflow).not.toBe("ellipsis");
  // no two agent cards overlap after automatic layout
  const boxes = await page.locator(".react-flow__node-agent").evaluateAll((els) =>
    els.map((el) => {
      const b = el.getBoundingClientRect();
      return [b.left, b.top, b.right, b.bottom];
    }),
  );
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const [a, b] = [boxes[i] ?? [], boxes[j] ?? []];
      const overlap = (a[0] ?? 0) < (b[2] ?? 0) && (b[0] ?? 0) < (a[2] ?? 0) && (a[1] ?? 0) < (b[3] ?? 0) && (b[1] ?? 0) < (a[3] ?? 0);
      expect(overlap, `agents ${String(i)} and ${String(j)} overlap`).toBe(false);
    }
});
