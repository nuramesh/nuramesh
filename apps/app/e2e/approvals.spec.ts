import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";

test("the inbox lists waiting requests first and opens one", async ({ page }) => {
  await page.goto("/approvals");
  const list = page.getByRole("region", { name: "Requests" });
  await expect(list.getByRole("link")).toHaveCount(4);
  await expect(page.getByTestId("pending-count")).toHaveText("2 waiting");
  await expect(page.getByRole("heading", { name: "Developer wants to merge main." })).toBeVisible();
  await list.getByRole("link", { name: /Chief of Staff wants to run a command/ }).click();
  await expect(page).toHaveURL(/\/approvals\/a2$/);
  await expect(page.getByRole("heading", { name: "Chief of Staff wants to run a command: deploy staging." })).toBeVisible();
  await expect(page.getByText("Applies to")).toBeVisible();
});

test("a reason is required, then approving records it and makes the request read-only", async ({ page }) => {
  await page.goto("/approvals/a1");
  const approve = page.getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  await page.getByRole("textbox", { name: /Your reason/ }).fill("   ");
  await expect(approve).toBeDisabled();
  await page.getByRole("textbox", { name: /Your reason/ }).fill("Reviewed the diff, tests and the verifier verdict");
  await approve.click();
  await expect(page.getByText(/Approved by You/)).toBeVisible();
  await expect(page.getByText("A person's reason (not shown here)")).toBeVisible();
  await expect(page.getByText(/Reviewed the diff, tests and the verifier verdict/)).toHaveCount(0);
  await expect(page.getByRole("link", { name: /recorded as ledger #\d+/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(page.getByTestId("pending-count")).toHaveText("1 waiting");
  // the sidebar badge follows without a reload
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /Approvals/ })).toContainText("1");
  await page.getByRole("link", { name: /Developer wants to merge main/ }).click();
  await expect(page.getByText("Decided requests are read-only.")).toBeVisible();
});

test("denying also needs a reason", async ({ page }) => {
  await page.goto("/approvals/a2");
  await expect(page.getByRole("button", { name: "Reject" })).toBeDisabled();
  await page.getByRole("textbox", { name: /Your reason/ }).fill("Wait for the release sign-off");
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByText(/Rejected by You/)).toBeVisible();
});

for (const scheme of ["light", "dark"] as const) {
  test(`the inbox passes axe, with a pending and a decided request (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/approvals/a1");
    await expect(page.getByRole("textbox", { name: /Your reason/ })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.getByRole("link", { name: /wants to spend/ }).click();
    await expect(page.getByText(/Approved by You/)).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: the inbox and the request fit without sideways page scrolling`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/approvals/a1");
    await expect(page.getByRole("heading", { name: "Developer wants to merge main." })).toBeVisible();
    await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
  });
}

test("approval buttons are at least 44 px tall", async ({ page }) => {
  await page.goto("/approvals/a1");
  for (const name of ["Approve", "Reject"]) {
    const box = await page.getByRole("button", { name }).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
});

test("no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/approvals");
  await expect(page.getByRole("heading", { name: "Developer wants to merge main." })).toBeVisible();
  await page.goto("/approvals/nope");
  await expect(page.getByText("There is no approval nope.")).toBeVisible();
  expect(errors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});
