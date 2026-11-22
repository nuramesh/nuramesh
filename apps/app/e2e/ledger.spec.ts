import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

test("shows the chain status and opens an entry", async ({ page }) => {
  await page.goto("/ledger");
  await expect(page.getByTestId("chain-status")).toContainText("Chain verified");
  await expect(page.getByTestId("chain-status")).toContainText("1,051 entries · no gaps · no edits");
  const table = page.getByRole("table", { name: "Ledger entries, newest first" });
  await expect(table.getByRole("row").nth(1)).toBeVisible();
  await table.getByRole("link", { name: "#1043" }).click();
  await expect(page).toHaveURL(/\/ledger\/1043$/);
  const detail = page.getByRole("complementary", { name: "Entry detail" });
  await expect(detail.getByRole("heading", { name: "Entry #1043" })).toBeVisible();
  await expect(detail).toContainText("Previous");
  await expect(detail).toContainText("Prompts and outputs are not stored in the ledger");
});

test("filters by kind and searches; the count follows", async ({ page }) => {
  await page.goto("/ledger");
  await expect(page.getByRole("table")).toBeVisible();
  const total = await page.getByTestId("ledger-count").innerText();
  await page.getByRole("button", { name: "Gates" }).click();
  await expect(page.getByTestId("ledger-count")).not.toHaveText(total);
  for (const kind of await page.getByRole("table").locator("tbody tr td:nth-child(3)").allInnerTexts()) expect(kind).toMatch(/gate/);
  await page.getByRole("button", { name: "All" }).click();
  await page.getByRole("searchbox", { name: "Search the ledger" }).fill("no-such-thing-zzz");
  await expect(page.getByText("No entries match.")).toBeVisible();
});

test("verifies the chain on request", async ({ page }) => {
  await page.goto("/ledger");
  await page.getByRole("button", { name: "Verify chain" }).click();
  await expect(page.getByRole("button", { name: "Verify chain" })).toBeEnabled();
  await expect(page.getByTestId("chain-status")).toContainText("Chain verified");
});

test("exports hashes and metadata, never content", async ({ page }) => {
  await page.goto("/ledger");
  await expect(page.getByRole("table")).toBeVisible();
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export" }).click()]);
  expect(download.suggestedFilename()).toBe("ledger.json");
  const { readFileSync } = await import("node:fs");
  const entries = JSON.parse(readFileSync(await download.path(), "utf8")) as Record<string, unknown>[];
  expect(entries.length).toBeGreaterThan(5);
  expect(Object.keys(entries[0] ?? {}).sort()).toEqual(["actor", "at", "cost", "entry", "hash", "kind", "previous_hash", "stream", "summary", "task_id"]);
});

for (const scheme of ["light", "dark"] as const) {
  test(`passes axe with an entry open (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/ledger/1043");
    await expect(page.getByRole("heading", { name: "Entry #1043" })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: fits without sideways page scrolling, targets are 44 px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/ledger/1043");
    await expect(page.getByRole("heading", { name: "Entry #1043" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
    const box = await page.getByRole("button", { name: "Verify chain" }).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  });
}

test("no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/ledger");
  await expect(page.getByRole("table")).toBeVisible();
  await page.goto("/ledger/9999999");
  await expect(page.getByText("There is no ledger entry 9999999.")).toBeVisible();
  expect(errors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});
