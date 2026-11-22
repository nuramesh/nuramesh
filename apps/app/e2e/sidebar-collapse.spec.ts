import { expect, type Page, test } from "@playwright/test";

import { expectAgents, fitView, loadTemplate, mod, node, openBuilder } from "./builder-helpers";

const nav = (page: Page) => page.getByRole("navigation", { name: "Main" });
const aside = (page: Page) => page.locator("#app-sidebar");
const toggle = (page: Page) => page.locator("#app-sidebar").getByRole("button", { name: /^(Collapse|Expand) sidebar$/ });

async function width(page: Page): Promise<number> {
  return Math.round((await aside(page).boundingBox())?.width ?? 0);
}

async function zoom(page: Page): Promise<number> {
  const transform = await page.locator(".react-flow__viewport").evaluate((el) => (el as HTMLElement).style.transform);
  return Number(/scale\(([\d.]+)\)/.exec(transform)?.[1] ?? "0");
}

async function expectNoOverflow(page: Page, label: string): Promise<void> {
  const extra = await page.evaluate(() => {
    const main = document.querySelector("main");
    const side = document.querySelector("#app-sidebar");
    return {
      html: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      main: (main?.scrollWidth ?? 0) - (main?.clientWidth ?? 0),
      side: (side?.scrollWidth ?? 0) - (side?.clientWidth ?? 0),
    };
  });
  for (const [name, value] of Object.entries(extra)) expect(value, `${label}: ${name}`).toBeLessThanOrEqual(0);
}

test.describe("collapsible sidebar", () => {
  test("the toggle button collapses to icons only and back, with the right name and state", async ({ page }) => {
    await page.goto("/runs");
    await expect(toggle(page)).toHaveAccessibleName("Collapse sidebar");
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
    expect(await width(page)).toBe(248);
    await toggle(page).click();
    await expect(toggle(page)).toHaveAccessibleName("Expand sidebar");
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
    expect(await width(page)).toBe(56);
    // Every item keeps its accessible name; the active route keeps its mark.
    for (const name of ["Org builder", "Templates", "Runs", "Approvals", "Budgets", "Ledger"]) {
      await expect(nav(page).getByRole("link", { name: new RegExp(name) })).toBeVisible();
    }
    await expect(nav(page).getByRole("link", { name: /Runs/ })).toHaveAttribute("aria-current", "page");
    await expect(nav(page).getByText("BUILD", { exact: true })).toHaveCount(0);
    await expect(aside(page).getByRole("link", { name: "Settings" })).toBeVisible();
    await toggle(page).click();
    expect(await width(page)).toBe(248);
    await expect(nav(page).getByText("BUILD", { exact: true })).toBeVisible();
  });

  test("Ctrl/Cmd+B toggles it; the tooltip names the shortcut", async ({ page }) => {
    await page.goto("/runs");
    await expect(toggle(page)).toBeVisible();
    await page.keyboard.press(`${mod}+b`);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
    await toggle(page).focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByRole("tooltip")).toContainText("Expand sidebar");
    await expect(page.getByRole("tooltip")).toContainText(/⌘B|Ctrl\+B/);
    await page.keyboard.press(`${mod}+b`);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
  });

  test("the shortcut is ignored while typing in a field or the YAML editor", async ({ page }) => {
    await openBuilder(page);
    await loadTemplate(page, "Dev team");
    await expectAgents(page, 5);
    await fitView(page);
    const before = await width(page);
    // The YAML editor is a textarea: typing there must never toggle the sidebar.
    await node(page, "dana").click();
    await page.getByRole("tab", { name: "YAML" }).click();
    const field = page.getByRole("textbox", { name: /^YAML for/ });
    await field.focus();
    await page.keyboard.press(`${mod}+b`);
    await page.waitForTimeout(300);
    expect(await width(page)).toBe(before);
    // Outside the field the same key does toggle it.
    await page.locator("main").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press(`${mod}+b`);
    await expect.poll(() => width(page)).not.toBe(before);
  });

  test("icons-only items show a tooltip on keyboard focus", async ({ page }) => {
    await page.goto("/runs");
    await toggle(page).click();
    await page.mouse.move(700, 400);
    const link = nav(page).getByRole("link", { name: /Ledger/ });
    await link.focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("tooltip")).toHaveText("Ledger");
    await aside(page).getByRole("link", { name: "Settings" }).focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("tooltip")).toHaveText("Settings");
  });

  test("the choice is remembered across a reload and wins at every width", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto("/runs");
    expect(await width(page)).toBe(248);
    await toggle(page).click();
    await page.reload();
    expect(await width(page)).toBe(56);
    await page.setViewportSize({ width: 1500, height: 800 });
    expect(await width(page)).toBe(56);
    await toggle(page).click();
    await page.setViewportSize({ width: 1280, height: 800 });
    expect(await width(page)).toBe(248);
    await page.reload();
    expect(await width(page)).toBe(248);
  });

  test("with no stored choice: expanded at 1440, icons only at 1280", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto("/runs");
    expect(await width(page)).toBe(248);
    await page.setViewportSize({ width: 1280, height: 800 });
    await expect.poll(() => width(page)).toBe(56);
    await page.setViewportSize({ width: 1440, height: 800 });
    await expect.poll(() => width(page)).toBe(248);
  });

  test("works when storage is blocked", async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, "localStorage", {
        get() {
          throw new Error("blocked");
        },
      });
    });
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/runs");
    expect(await width(page)).toBe(56);
    await toggle(page).click();
    expect(await width(page)).toBe(248);
  });

  test("on a narrow screen the menu stays the full sidebar", async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 800 });
    await page.goto("/runs");
    await page.getByRole("button", { name: "Open menu" }).click();
    await expect(nav(page).getByText("BUILD", { exact: true })).toBeVisible();
    await expect(toggle(page)).toBeHidden();
  });

  test("the canvas gains the freed width and fits again", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await openBuilder(page);
    await loadTemplate(page, "Dev team");
    await expectAgents(page, 5);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
    const region = page.getByRole("region", { name: "Organization canvas" });
    const collapsedWidth = (await region.boundingBox())?.width ?? 0;
    await toggle(page).click();
    await expect.poll(async () => ((await region.boundingBox())?.width ?? 0) < collapsedWidth).toBe(true);
    await page.waitForTimeout(300);
    const expandedZoom = await zoom(page);
    await toggle(page).click();
    await page.waitForTimeout(300);
    const collapsedZoom = await zoom(page);
    expect(collapsedZoom).toBeGreaterThanOrEqual(expandedZoom);
    expect(collapsedZoom).toBeGreaterThan(0);
  });

  for (const scheme of ["light", "dark"] as const) {
    for (const viewport of [1280, 1440]) {
      test(`no overflow, collapsed or expanded, at ${String(viewport)} (${scheme})`, async ({ page }) => {
        await page.emulateMedia({ colorScheme: scheme });
        await page.setViewportSize({ width: viewport, height: 800 });
        await openBuilder(page);
        await loadTemplate(page, "Dev team");
        for (const want of ["false", "true", "false"]) {
          if ((await toggle(page).getAttribute("aria-expanded")) !== want) await toggle(page).click();
          await expect(toggle(page)).toHaveAttribute("aria-expanded", want);
          await expectNoOverflow(page, `${String(viewport)} ${scheme} expanded=${want}`);
          // The avatar, theme and settings icons all lie inside the sidebar.
          const box = await aside(page).boundingBox();
          for (const name of [/^Theme:/, "Settings"]) {
            const b = await aside(page).getByRole(name === "Settings" ? "link" : "button", { name }).boundingBox();
            expect(b?.x ?? -1).toBeGreaterThanOrEqual(box?.x ?? 0);
            expect((b?.x ?? 0) + (b?.width ?? 0)).toBeLessThanOrEqual((box?.x ?? 0) + (box?.width ?? 0));
          }
        }
      });
    }
  }
});
