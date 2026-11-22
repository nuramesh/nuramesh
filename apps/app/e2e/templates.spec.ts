import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";
import { exportYamlSample } from "./templates-sample";

test("shows the available templates, and the filters (none is planned any more)", async ({ page }) => {
  await page.goto("/templates");
  await expect(page.getByRole("heading", { name: "Dev team", level: 3 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Support", level: 3 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Planned" })).toHaveCount(0);
  await page.getByRole("button", { name: "Available now" }).click();
  await expect(page.getByRole("heading", { name: "Dev team", level: 3 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Support", level: 3 })).toBeVisible();
  await expect(page.getByText(/Nothing saved yet/)).toBeVisible();
});

test("preview shows YAML; Use template opens it in the builder", async ({ page }) => {
  await page.goto("/templates");
  const dev = page.locator("article", { has: page.getByRole("heading", { name: "Dev team", level: 3 }) });
  await dev.getByRole("button", { name: "Preview YAML" }).click();
  const dialog = page.getByRole("dialog", { name: "Dev team (YAML)" });
  await expect(dialog.getByRole("region", { name: "YAML" })).toContainText("version: 1");
  await expectNoSeriousA11yViolations(page);
  await dialog.getByRole("button", { name: "Close" }).first().click();
  await dev.getByRole("button", { name: "Use template" }).click();
  await expect(page).toHaveURL(/\/builder$/);
  await expect(page.getByText("Loaded template Dev team. Undo to go back.")).toBeVisible();
  await expect(page.getByTestId("builder-summary")).toContainText("5 agents");
});

test("Import YAML: errors are explained, valid YAML opens in the builder", async ({ page }) => {
  await page.goto("/templates");
  await page.getByRole("button", { name: "Import YAML" }).click();
  const dialog = page.getByRole("dialog", { name: "Import YAML" });
  await expect(dialog.getByRole("button", { name: "Open in the builder" })).toBeDisabled();
  await dialog.getByRole("textbox", { name: "YAML" }).fill("prices:\n  prices: []\n");
  await dialog.getByRole("button", { name: "Open in the builder" }).click();
  await expect(dialog.getByRole("alert")).toContainText("prices: The builder doesn't edit");
  await dialog.getByRole("textbox", { name: "YAML" }).fill(exportYamlSample);
  await dialog.getByRole("button", { name: "Open in the builder" }).click();
  await expect(page).toHaveURL(/\/builder$/);
  await expect(page.getByText("Imported YAML. Undo to go back.")).toBeVisible();
  await expect(page.getByTestId("builder-summary")).toContainText("2 agents");
});

for (const scheme of ["light", "dark"] as const) {
  test(`passes axe with the import dialog open (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/templates");
    await expect(page.getByRole("heading", { name: "Dev team", level: 3 })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.getByRole("button", { name: "Import YAML" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: fits without sideways page scrolling, buttons 44 px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/templates");
    await expect(page.getByRole("heading", { name: "Dev team", level: 3 })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
    const box = await page.getByRole("button", { name: "Use template" }).first().boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  });
}

test("no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/templates");
  await expect(page.getByRole("heading", { name: "Dev team", level: 3 })).toBeVisible();
  expect(errors).toEqual([]);
});
