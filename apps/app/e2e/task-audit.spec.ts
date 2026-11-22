import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

// Real-browser spec for R1.27 (spec/r127 section 11). The fixture client serves run r1, task t1. It cannot run on the
// build VM (ND135); CI runs it. The live-update cases (U7 to U11, U23, U24, U28) need a pushed stream and are covered
// by vitest with a fake socket in src/runs/task-audit.test.tsx.

const PAGE = "/runs/r1/tasks/t1";

test("the audit page shows the attempts and the rework of the task", async ({ page }) => {
  await page.goto(PAGE);
  await expect(page.getByRole("heading", { level: 1, name: "Draft summary" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Summary" })).toContainText("Chain verified");
  await expect(page.getByRole("region", { name: "Attempts" })).toContainText("Attempt 2: at least $0.00000000 in 1 call, 1 with cost unknown (charged the reservation)");
  await expect(page.getByRole("region", { name: "Rework" })).toContainText("sent back by a gate: gate failed: g-ver");
  await expect(page.getByRole("log").getByRole("listitem")).toHaveCount(21);
});

test("the run page's Audit trail link opens the task's page (the seeded run-128)", async ({ page }) => {
  await page.goto("/runs/run-128");
  await page.getByRole("link", { name: "Audit trail for Add retries to the payment webhook" }).click();
  await expect(page).toHaveURL(/\/runs\/run-128\/tasks\/t-retries$/);
  await expect(page.getByRole("heading", { level: 1, name: "Add retries to the payment webhook" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Approvals and decisions" })).toContainText("Engine incident");
  await expect(page.getByText("NOTE-TEXT-FIXTURE")).toHaveCount(0);
  await page.getByRole("link", { name: "Open run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-128$/);
});

test("a keyboard alone reaches the filters and an entry link", async ({ page }) => {
  await page.goto(PAGE);
  await page.getByRole("heading", { level: 1 }).waitFor();
  const gates = page.getByRole("button", { name: "Gates" });
  await gates.focus();
  await page.keyboard.press("Enter");
  await expect(gates).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("log").getByRole("listitem")).toHaveCount(4);
  await page.getByRole("log").getByRole("link").first().focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/ledger\//);
});

test("no serious accessibility violations, light and dark", async ({ page }) => {
  await page.goto(PAGE);
  await page.getByRole("heading", { level: 1 }).waitFor();
  await expectNoSeriousA11yViolations(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await expectNoSeriousA11yViolations(page);
});

test("at 360 px wide nothing scrolls sideways and targets are 44 px high", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(PAGE);
  await page.getByRole("heading", { level: 1 }).waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  for (const target of [page.getByRole("button", { name: "Gates" }), page.getByRole("link", { name: "Open run" })]) {
    const box = await target.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
});

test("an unknown task is the not-found page", async ({ page }) => {
  await page.goto("/runs/r1/tasks/nope");
  await expect(page.getByRole("heading", { name: /not found/i })).toBeVisible();
});
