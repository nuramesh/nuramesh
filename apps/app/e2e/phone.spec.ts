import { expect, test } from "@playwright/test";

import { expectOnScreen, expectPhoneFit, PHONE, PHONE_ROUTES } from "./phone";

test.use({ viewport: PHONE, hasTouch: true });

for (const scheme of ["light", "dark"] as const) {
  test.describe(`phone width, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    for (const route of PHONE_ROUTES) {
      test(`${route.path} fits 390 px`, async ({ page }) => {
        await page.goto(route.path);
        await expect(page.getByRole("main")).toBeVisible();
        const h1 = page.getByRole("heading", { level: 1 }).first();
        if (route.title !== "") await expect(h1).toContainText(route.title);
        else await expect(h1).toBeVisible();
        await expectPhoneFit(page, route.path, route.innerScroll === true);
        // No leftover debug label (the dev-only metrics strip must never reach a production build).
        await expect(page.getByText(/inner \d+ · client \d+/)).toHaveCount(0);
      });
    }
  });
}

test.describe("oversight reachable on a phone", () => {
  test("the menu opens, shows the pending count and reaches every page", async ({ page }) => {
    await page.goto("/runs");
    await page.getByRole("button", { name: "Open menu" }).click();
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: /Approvals/ })).toContainText("2");
    for (const name of ["Runs", "Approvals", "Budgets and spend", "Ledger", "Org builder", "Templates"]) {
      const link = nav.getByRole("link", { name: new RegExp(name) });
      await expect(link).toBeVisible();
      expect((await link.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(43.5);
    }
  });

  test("the open menu shows the logo once and its content fills the drawer", async ({ page }) => {
    await page.goto("/approvals");
    // /approvals pins the first request in the URL once the list loads (a route change, which closes
    // the menu by design). Wait for that redirect to settle, or under load it lands after the click
    // and the drawer closes again (width 0, nav not found).
    await expect(page).toHaveURL(/\/approvals\/a1$/);
    await page.getByRole("button", { name: "Open menu" }).click();
    await expect(page.getByRole("button", { name: "Close menu" })).toHaveAttribute("aria-expanded", "true");
    const drawer = page.locator("#app-sidebar");
    await expect(drawer.getByRole("navigation", { name: "Main" })).toBeVisible();
    // The top bar (logo + close button) stays; the sidebar must not draw a second logo.
    const logos = page.getByRole("img", { name: /NuraMesh/ });
    // Poll: the layout settles after the menu opens (fonts, async org and budget data), so a single
    // early read can see a transient state under CI load.
    await expect
      .poll(() => logos.evaluateAll((els) => els.filter((el) => el.getBoundingClientRect().width > 0).length), {
        message: "visible logos while the menu is open",
      })
      .toBe(1);
    // Content spans the drawer (390 px) minus normal side padding (at most 24 px a side).
    await expect.poll(async () => (await drawer.boundingBox())?.width ?? 0).toBeGreaterThan(380);
    const blocks = [
      drawer.getByText("example-org").locator("xpath=ancestor::div[contains(@class,'rounded-[10px]')][1]"), // org card
      drawer.getByRole("link", { name: /Approvals/ }), // a menu row
      drawer.getByText("This month", { exact: false }).first().locator("xpath=ancestor::div[contains(@class,'rounded-[10px]')][1]"), // budget card
    ];
    for (const block of blocks) await expect(block).toBeVisible();
    const d = await drawer.boundingBox();
    for (const block of blocks) {
      const b = await block.boundingBox();
      expect(b, "drawer block exists").not.toBeNull();
      expect((b?.x ?? 99) - (d?.x ?? 0)).toBeLessThanOrEqual(24);
      expect((d?.x ?? 0) + (d?.width ?? 0) - ((b?.x ?? 0) + (b?.width ?? 0))).toBeLessThanOrEqual(24);
    }
  });

  test("a request can be read, reasoned about and decided without sideways scrolling", async ({ page }) => {
    await page.goto("/approvals/a1");
    await expectOnScreen(page, "Approve");
    await expectOnScreen(page, "Reject");
    await page.getByRole("textbox", { name: /Your reason/ }).fill("Reviewed on my phone");
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText(/Approved by You/)).toBeVisible();
  });

  test("the approvals list opens a request in one tap", async ({ page }) => {
    await page.goto("/approvals");
    await page.getByRole("region", { name: "Requests" }).getByRole("link", { name: /Chief of Staff wants to run a command/ }).click();
    await expect(page).toHaveURL(/\/approvals\/a2$/);
    await expectOnScreen(page, "Reject");
  });

  test("a tap on a ledger row reaches the detail pane with the columns the phone hides", async ({ page }) => {
    await page.goto("/ledger");
    const rows = page.getByRole("region", { name: "Entries" });
    await rows.getByRole("link", { name: /^#1050$/ }).click();
    await expect(page).toHaveURL(/\/ledger\/1050$/);
    const detail = page.getByRole("complementary", { name: "Entry detail" });
    await expect(detail.getByRole("heading", { name: /Entry #1050/ })).toBeVisible();
    // The table drops Time, Actor and Hash below 768 px; the detail pane carries all three.
    for (const label of ["When", "Actor", "This"]) await expect(detail.getByText(label, { exact: true })).toBeVisible();
    await expect(detail.getByRole("time")).toBeVisible();
  });

  test("a live run shows Stop and Pause on screen, and Stop asks first", async ({ page }) => {
    await page.goto("/runs/run-128");
    await expectOnScreen(page, "Stop");
    await expectOnScreen(page, "Pause");
    await page.getByRole("button", { name: "Stop", exact: true }).first().click();
    const dialog = page.getByRole("dialog", { name: "Stop run #128?" });
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 999)).toBeLessThanOrEqual(PHONE.width + 0.5);
    await page.getByRole("button", { name: "Keep running" }).click();
  });

  test("the runs list shows status and spend for each run without scrolling sideways", async ({ page }) => {
    await page.goto("/runs");
    const first = page.getByRole("link", { name: /Fix issue #142/ }).first();
    await first.scrollIntoViewIfNeeded();
    const card = first.locator("xpath=ancestor::*[self::tr or self::li or self::article][1]");
    await expect(card).toContainText("$");
    await expect(card).toContainText(/Running|Waiting|Done|Stopped|Failed/);
    const box = await card.boundingBox();
    expect((box?.x ?? -1) + (box?.width ?? 999)).toBeLessThanOrEqual(PHONE.width + 0.5);
  });

  test("the org builder offers a clear read-only phone mode", async ({ page }) => {
    await page.goto("/builder");
    await expect(page.getByRole("note").filter({ hasText: "larger screen" })).toBeVisible();
  });
});
