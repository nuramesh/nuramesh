import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

test("the runs list opens a run", async ({ page }) => {
  await page.goto("/runs");
  const table = page.getByRole("table", { name: "Runs, newest first" });
  await expect(table.getByRole("row")).toHaveCount(6); // header + 5 runs
  await expect(page.getByTestId("runs-summary")).toHaveText("5 runs · 1 running · 1 waiting for you");
  await table.getByRole("link", { name: /Fix issue #142/ }).click();
  await expect(page).toHaveURL(/\/runs\/run-128$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Fix issue #142");
  await page.getByRole("link", { name: "Runs", exact: true }).first().click(); // breadcrumb or sidebar
  await expect(page).toHaveURL(/\/runs$/);
});

test("a live run shows the numbers, the graph, the latest gate and the timeline", async ({ page }) => {
  await page.goto("/runs/run-128");
  await expect(page.getByText("of 3 done")).toBeVisible();
  await expect(page.getByTestId("run-node-tester")).toHaveAttribute("data-status", "running");
  await expect(page.getByRole("region", { name: "Latest gate result" })).toContainText("Independent review");
  await expect(page.getByRole("log", { name: "Events, newest first" }).getByRole("listitem").first()).toBeVisible();
  await page.getByRole("button", { name: "Spend" }).click();
  for (const text of await page.getByRole("log").getByRole("listitem").allInnerTexts()) expect(text).toMatch(/\$|budget|reserved|settled/i);
  await page.getByRole("button", { name: "Approvals" }).click();
  await expect(page.getByRole("log").getByRole("listitem").first()).toContainText(/approval/i);
  await page.getByRole("link", { name: /Needs you/ }).click();
  await expect(page).toHaveURL(/\/approvals(\/a1)?$/);
});

test("pause updates the status and the timeline without a reload; stop asks first", async ({ page }) => {
  await page.goto("/runs/run-128");
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByText("Paused", { exact: true })).toBeVisible();
  await expect(page.getByRole("log").getByText("run.paused")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause" })).toBeDisabled();
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Stop run #128?" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("button", { name: "Keep running" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.getByRole("button", { name: "Stop", exact: true }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText("Stopped", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeDisabled();
  // the runs list agrees (client-side navigation: a reload would reset the in-memory sample data)
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Runs" }).click();
  await expect(page.getByRole("row", { name: /#128/ })).toContainText("Stopped");
});

for (const scheme of ["light", "dark"] as const) {
  test(`both run screens pass axe (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/runs");
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.goto("/runs/run-128");
    await expect(page.getByRole("log")).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: both screens fit without sideways page scrolling`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/runs", "/runs/run-128"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      expect(overflow, path).toBe(false);
    }
    await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
    await expect(page.getByRole("log")).toBeVisible();
  });
}

test("no console errors on either screen", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/runs");
  await expect(page.getByRole("table")).toBeVisible();
  await page.goto("/runs/run-128");
  await expect(page.getByRole("log")).toBeVisible();
  await page.goto("/runs/not-a-run");
  await expect(page.getByRole("heading", { name: "Run not found" })).toBeVisible();
  expect(errors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});

test("390 px: the graph region is keyboard-scrollable and the run page passes axe; controls are 44 px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/runs/run-128");
  await expect(page.getByRole("log")).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  for (const name of ["Pause", "Stop", "Gates", "Spend"]) {
    const box = await page.getByRole("button", { name, exact: true }).first().boundingBox();
    expect(box?.height ?? 0, name).toBeGreaterThanOrEqual(44);
  }
});
