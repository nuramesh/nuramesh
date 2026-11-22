import { expect, type Page, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

test.describe("app shell", () => {
  test("renders landmarks and passes axe in light and dark", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/builder$/); // / redirects to the builder
    await expect(page.getByRole("main")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    await expectNoSeriousA11yViolations(page);

    await page.getByRole("button", { name: /^Theme:/ }).click();
    await page.getByRole("menuitemradio", { name: "Dark" }).click();
    await expect(page.locator("html")).toHaveClass(/dark/);
    // The menu hides the rest of the page from assistive tech while open; wait until it closes.
    await expect(page.getByRole("menu")).toBeHidden();
    await expect(page.locator("#root")).not.toHaveAttribute("aria-hidden", "true");
    await expectNoSeriousA11yViolations(page);
  });

  test("skip link moves focus to the main content", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to content" });
    await expect(skip).toBeFocused();
    await expect(skip).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/#main$/);
  });

  test("unknown URLs show a 404 page that passes axe", async ({ page }) => {
    await page.goto("/does-not-exist");
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.getByRole("link", { name: "Go to the builder" }).click();
    await expect(page).toHaveURL(/\/builder$/);
  });
});

const ROUTES = [
  ["/templates", "Templates"],
  ["/runs", "Runs"],
  ["/runs/run-128", "Fix issue"],
  ["/approvals", "Approvals"],
  ["/approvals/a2", "Approvals"],
  ["/budgets", "Budgets and spend"],
  ["/ledger", "Ledger"],
  ["/settings", "Settings"],
] as const;

test.describe("sidebar and routes", () => {
  test("every route renders inside the shell and passes axe in both themes", async ({ page }) => {
    test.setTimeout(90_000); // 9 routes x 2 themes, each with an axe run
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const [path, title] of ROUTES) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
        await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
        await expectNoSeriousA11yViolations(page);
      }
    }
  });

  test("the sidebar links navigate and mark the current page", async ({ page }) => {
    await page.goto("/builder");
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("link", { name: "Approvals" }).click();
    await expect(page).toHaveURL(/\/approvals(\/a1)?$/);
    await expect(nav.getByRole("link", { name: "Approvals" })).toHaveAttribute("aria-current", "page");
    await page.getByRole("link", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  });

  test("the sidebar shows the live count, the approvals badge and the month's spend from the API client", async ({ page }) => {
    await page.goto("/runs");
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByText("1 live")).toBeVisible();
    await expect(nav.getByRole("link", { name: /Approvals/ })).toContainText(/2\s*waiting/);
    await expect(page.getByText("$42.10")).toBeVisible();
    await expect(page.getByText(/Sample data/)).toBeVisible();
  });

  test("sidebar targets are at least 44 px", async ({ page }) => {
    await page.goto("/builder");
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible(); // rendered
    const targets = page.locator("#app-sidebar").locator("a, button");
    expect(await targets.count()).toBeGreaterThan(6);
    for (const target of await targets.all()) {
      const box = await target.boundingBox();
      expect(box?.height ?? 0, await target.innerText()).toBeGreaterThanOrEqual(44);
      expect(box?.width ?? 0, await target.innerText()).toBeGreaterThanOrEqual(44);
    }
  });

  test.describe("builder layouts", () => {
    const canvasBox = async (page: Page) =>
      page.getByRole("region", { name: "Organization canvas" }).boundingBox();

    test("1024 px: the palette and Properties are drawers over the canvas", async ({ page }) => {
      await page.setViewportSize({ width: 1024, height: 800 });
      await page.goto("/builder");
      const canvas = page.getByRole("region", { name: "Organization canvas" });
      await expect(canvas).toBeVisible();
      await expect(page.getByRole("complementary", { name: "Details" })).toBeHidden();
      const before = (await canvasBox(page))?.width ?? 0;
      expect(before).toBeGreaterThan(400);
      await page.getByRole("button", { name: "Palette", exact: true }).click();
      await expect(page.getByRole("button", { name: /Chief of Staff/ }).first()).toBeVisible();
      await expect(canvas).toBeVisible();
      expect((await canvasBox(page))?.width).toBe(before); // an overlay never pushes the canvas
      await expectNoSeriousA11yViolations(page);
      await page.getByRole("button", { name: "Properties", exact: true }).click(); // opening one closes the other
      await expect(page.getByRole("complementary", { name: "Details" })).toBeVisible();
      await expect(page.getByRole("complementary", { name: "Add to the organization" })).toBeHidden();
      await expectNoSeriousA11yViolations(page);
    });

    test("the layout follows the width at the edge: docked at 1280, drawers at 1279", async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 800 });
      await page.goto("/builder");
      await expect(page.getByRole("complementary", { name: "Details" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Properties", exact: true })).toHaveCount(0);
      await page.setViewportSize({ width: 1279, height: 800 });
      await expect(page.getByRole("complementary", { name: "Details" })).toBeHidden();
      await expect(page.getByRole("button", { name: "Properties", exact: true })).toBeVisible();
      await page.setViewportSize({ width: 768, height: 800 });
      await expect(page.getByRole("button", { name: "Palette", exact: true })).toBeVisible();
    });

    test("axe passes at 768 and in dark at 1024", async ({ page }) => {
      await page.setViewportSize({ width: 768, height: 900 });
      await page.goto("/builder");
      await expectNoSeriousA11yViolations(page);
      await page.emulateMedia({ colorScheme: "dark" });
      await page.setViewportSize({ width: 1024, height: 800 });
      await expect(page.locator("html")).toHaveClass(/dark/); // the theme has switched before axe looks
      await expectNoSeriousA11yViolations(page);
    });

    test("768 px: drawers, opened from the toolbar", async ({ page }) => {
      await page.setViewportSize({ width: 768, height: 900 });
      await page.goto("/builder");
      await expect(page.getByRole("complementary", { name: "Details" })).toBeHidden();
      await page.getByRole("button", { name: "Properties", exact: true }).click();
      await expect(page.getByRole("complementary", { name: "Details" })).toBeVisible();
      await expect(page.getByRole("region", { name: "Organization canvas" })).toBeVisible();
    });

    test("1440 px: palette (icon rail), canvas and Properties side by side, no drawer buttons", async ({ page }) => {
      await page.goto("/builder");
      await expect(page.getByRole("button", { name: "Palette", exact: true })).toHaveCount(0);
      await expect(page.getByRole("region", { name: "Organization canvas" })).toBeVisible();
      await expect(page.getByRole("complementary", { name: "Details" })).toBeVisible();
      // below 1600 px the palette starts as the icon rail (a stored choice wins: see builder-responsive)
      await expect(page.getByRole("button", { name: "Expand the palette", exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: /Chief of Staff/ }).first()).toBeHidden();
    });
  });

  test.describe("on a phone", () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test("the menu opens from the top bar and follows a link", async ({ page }) => {
      await page.goto("/builder");
      await expect(page.getByRole("banner")).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
      await page.getByRole("button", { name: "Open menu" }).click();
      await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
      await expectNoSeriousA11yViolations(page);
      await page.getByRole("link", { name: "Ledger" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Ledger" })).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
    });

    test("the builder is a read-only canvas with a notice to edit on a larger screen", async ({ page }) => {
      await page.goto("/builder");
      const canvas = page.getByRole("region", { name: "Organization canvas" });
      await expect(canvas).toBeVisible();
      await expect(canvas.boundingBox().then((b) => b?.width ?? 0)).resolves.toBeGreaterThan(300);
      await expect(page.getByRole("note").filter({ hasText: "Open this page on a larger screen to edit" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Palette", exact: true })).toBeHidden();
      await expect(page.getByRole("button", { name: "Properties", exact: true })).toBeHidden();
      await expect(page.getByRole("button", { name: "Export YAML" })).toBeHidden();
      await expectNoSeriousA11yViolations(page);
    });

    test("Escape closes the menu and returns focus to its button, but a theme menu takes it first", async ({ page }) => {
      await page.goto("/builder");
      const open = page.getByRole("button", { name: "Open menu" });
      await open.click();
      await page.getByRole("button", { name: /^Theme:/ }).click();
      await expect(page.getByRole("menu")).toBeVisible();
      await page.keyboard.press("Escape"); // closes only the theme menu
      await expect(page.getByRole("menu")).toBeHidden();
      await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
      await page.getByRole("link", { name: "Org builder" }).focus();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
      await expect(page.getByRole("button", { name: "Open menu" })).toBeFocused();
    });

    test("following a menu link by keyboard moves focus to the page", async ({ page }) => {
      await page.goto("/builder");
      await page.getByRole("button", { name: "Open menu" }).click();
      await page.getByRole("link", { name: "Ledger" }).focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("heading", { level: 1, name: "Ledger" })).toBeVisible();
      await expect(page.locator("#main")).toBeFocused();
    });

    test("no page scrolls sideways", async ({ page }) => {
      for (const [path] of [["/builder"], ...ROUTES]) {
        await page.goto(path);
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow, path).toBeLessThanOrEqual(0);
      }
    });
  });
});

test.describe("theme", () => {
  test("follows the system setting by default", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/");
    await expect(page.locator("html")).toHaveClass(/dark/);
    await page.emulateMedia({ colorScheme: "light" });
    await expect(page.locator("html")).not.toHaveClass(/dark/);
  });

  test("applies the saved theme before the app's JavaScript runs (no flash)", async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem("nuramesh-theme", "dark");
    });
    // Block the app bundle: only the tiny head script may set the theme.
    await page.route(/\/assets\/.*\.js$/, (route) => route.abort());
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expect(page.locator("#root")).toBeEmpty();
  });

  test("remembers the choice across reloads", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/");
    await page.getByRole("button", { name: /^Theme:/ }).click();
    await page.getByRole("menuitemradio", { name: "Light" }).click();
    await page.reload();
    await expect(page.locator("html")).not.toHaveClass(/dark/);
    await expect(page.getByRole("button", { name: "Theme: Light" })).toBeVisible();
  });
});
