import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

test("shows numbers, budgets and spend by model", async ({ page }) => {
  await page.goto("/budgets");
  await expect(page.getByText("Org this month")).toBeVisible();
  await expect(page.getByRole("table", { name: /Budgets with limit/ }).getByText("blocked")).toBeVisible();
  await expect(page.getByRole("region", { name: "Spend by model" })).toBeVisible();
  await expect(page.getByText(/Unknown prices are blocked, never guessed/)).toBeVisible();
});

test("unblocking needs a reason and records a ledger entry", async ({ page }) => {
  await page.goto("/budgets");
  await page.getByRole("button", { name: "Unblock with a reason" }).click();
  const confirm = page.getByRole("button", { name: "Confirm unblock" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel(/Reason for unblocking/).fill("Approved by finance");
  await page.getByLabel(/New limit in USD/).fill("60.00");
  await confirm.click();
  await expect(page.getByText(/unblocked by You · reason recorded as ledger #\d+/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Unblock with a reason" })).toHaveCount(0);
  await expect(page.getByRole("table", { name: /Budgets with limit/ }).getByText("blocked")).toHaveCount(0);
});

for (const scheme of ["light", "dark"] as const) {
  test(`passes axe, with the breach form open (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/budgets");
    await expect(page.getByRole("table", { name: /Budgets with limit/ })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.getByRole("button", { name: "Unblock with a reason" }).click();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: fits without sideways page scrolling, buttons 44 px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/budgets");
    await expect(page.getByRole("table", { name: /Budgets with limit/ })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
    const box = await page.getByRole("button", { name: "Unblock with a reason" }).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  });
}

test("no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/budgets");
  await expect(page.getByRole("table", { name: /Budgets with limit/ })).toBeVisible();
  expect(errors).toEqual([]);
});
