import { expect, type Page, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";
import { center, drag, expectAgents, fitView, loadTemplate, node, openBuilder } from "./builder-helpers";

test.beforeEach(async ({ page }) => {
  await openBuilder(page);
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await fitView(page);
});

const gateCount = (page: Page) => page.getByTestId("gate-diamond");

/** Adds a gate to the selected agent's role from the inspector: a kind, then its settings (a gate is never added without them). */
async function addGate(page: Page, kind: string, settings: string): Promise<void> {
  await page.getByRole("combobox", { name: "Add a gate" }).selectOption({ label: kind });
  await expect(page.getByRole("button", { name: "Add gate" })).toBeDisabled();
  await page.getByRole("textbox", { name: `Settings for the ${kind} (YAML)` }).fill(settings);
  await page.getByRole("button", { name: "Add gate" }).click();
  await expect(page.getByRole("button", { name: `Remove ${kind}` })).toBeVisible();
}

test("the header shows the file, the Git ref and that the organization is valid", async ({ page }) => {
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText("example-org");
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText("Dev team");
  await expect(page.getByTestId("config-chip")).toHaveText(/dev-team\.yaml · main/);
  await expect(page.getByTestId("validity-chip")).toHaveText("Valid · in sync with YAML");
  await expect(page.getByRole("button", { name: "Export YAML" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save as template" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start", exact: true })).toBeEnabled();
});

test("the palette offers agents, checks and people", async ({ page }) => {
  const palette = page.getByRole("complementary", { name: /Add to the organization/ });
  for (const heading of ["Agents", "Checks", "People"]) await expect(palette.getByRole("heading", { name: heading })).toBeVisible();
  for (const name of ["Rule check", "Test command", "Independent verifier", "Eval", "Human approver", "Custom role"]) {
    await expect(palette.getByRole("button", { name: new RegExp(name) })).toBeVisible();
  }
});

test("nodes show model chips (dashed for open weights), gates are diamonds with counts", async ({ page }) => {
  await expect(node(page, "dana").getByTestId("model-chip")).toHaveText("anthropic/claude-sonnet-5-5");
  await expect(node(page, "dana").getByTestId("model-chip")).not.toHaveAttribute("data-open-weight", "true");
  await node(page, "dana").click();
  await page.getByRole("combobox", { name: "Model" }).fill("meta/llama-3.1-70b");
  await expect(node(page, "dana").getByTestId("model-chip")).toHaveAttribute("data-open-weight", "true");
  // a template brings no gates; one added for the developer role shows on the one developer
  await expect(gateCount(page)).toHaveCount(0);
  await addGate(page, "Independent verifier", "verifier: reviewer");
  await expect(gateCount(page)).toHaveCount(1);
  await expect(gateCount(page).first()).toContainText("1");
});

test("drags a check from the palette onto an agent: the gate is added in the inspector, with its settings", async ({ page }) => {
  await expect(gateCount(page)).toHaveCount(0);
  await page.getByRole("button", { name: /^Add Rule check/ }).dragTo(node(page, "rhea"));
  // the drop writes no gate (it would have no settings); it opens the reviewer's inspector
  await expect(gateCount(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Gates" })).toBeVisible();
  await addGate(page, "Rule check", 'rules:\n  - criterion: cited\n    check: {type: contains, value: "Evidence:"}');
  await expect(gateCount(page)).toHaveCount(1);
  await expect(page.getByTestId("validity-chip")).toHaveText("Valid · in sync with YAML");
});

test("connects a human approver to an agent with an approval line, and a verifier to the work it checks", async ({ page }) => {
  await page.getByRole("button", { name: /^Add Human approver/ }).click();
  const person = page.locator(".react-flow__node-person");
  await fitView(page); // people start left of the organization, outside the view
  await expect(person).toHaveCount(1);
  await drag(page, await center(person.locator(".react-flow__handle-right")), await center(node(page, "dana").locator(".react-flow__handle-left")));
  // the one developer holds the role, so it gets an approval line
  await expect(page.locator(".react-flow__edge.ng-edge-approve")).toHaveCount(1);

  // verification lines: the reviewer checks three agents; a new one is added from the inspector
  await expect(page.locator(".react-flow__edge.ng-edge-verify")).toHaveCount(3);
  await node(page, "rhea").click();
  await page.getByRole("combobox", { name: "Verify another agent" }).selectOption({ label: "Chief of Staff" });
  await expect(page.locator(".react-flow__edge.ng-edge-verify")).toHaveCount(4);
  // ...but a verifier can't check what it answers to: the problem is reported
  await expect(page.getByTestId("validity-chip")).toHaveText(/1 problem/);
});

test("renames an agent: the name changes, the id (what ledger and budgets use) stays", async ({ page }) => {
  await node(page, "dana").click();
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Ada");
  await expect(node(page, "dana")).toContainText("Ada");
  await expect(page.getByRole("textbox", { name: "Id" })).toHaveValue("dana");
});

test("a verifier must be a different model family from the work it checks", async ({ page }) => {
  await node(page, "rhea").click();
  await expect(page.getByTestId("model-family")).toContainText("openai");
  await page.getByRole("combobox", { name: "Model" }).fill("anthropic/claude-opus-5-5");
  await expect(page.getByTestId("validity-chip")).toHaveText(/problem/);
  await page.getByRole("tab", { name: /^Problems/ }).click();
  await expect(page.getByText(/same family as/).first()).toBeVisible();
  await page.getByRole("tab", { name: "Properties" }).click();
  await page.getByRole("combobox", { name: "Model" }).fill("google/gemini-2.5-pro");
  await expect(page.getByTestId("validity-chip")).toHaveText("Valid · in sync with YAML");
});

test("applies a policy preset and changes permissions and gates", async ({ page }) => {
  await node(page, "dana").click();
  await page.getByRole("combobox", { name: "Apply policy preset" }).selectOption({ label: "Read-only" });
  await expect(page.getByRole("list", { name: "Applied presets" })).toContainText("Read-only");
  await expect(page.getByRole("list", { name: "Rules from presets" })).toBeVisible();
  await page.getByRole("button", { name: "Add rule", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Rule 3 decision" })).toBeVisible();
  // gates are ordered cheapest first: a verifier before a test command is a problem, and moving it back fixes it
  await addGate(page, "Independent verifier", "verifier: reviewer");
  await addGate(page, "Test command", "image: registry.example/tests@sha256:" + "a".repeat(64) + "\ncommand: [pytest, -q]");
  await page.getByRole("button", { name: "Move Independent verifier up" }).click();
  await expect(page.getByTestId("validity-chip")).toHaveText(/1 problem/);
  await page.getByRole("button", { name: "Move Independent verifier down" }).click();
  await expect(page.getByTestId("validity-chip")).toHaveText("Valid · in sync with YAML");
});

test("the YAML tab is two-way synced with the canvas", async ({ page }) => {
  await node(page, "dana").click();
  await page.getByRole("tab", { name: "YAML" }).click();
  const yaml = page.getByRole("textbox", { name: /^YAML for/ });
  await expect(yaml).toHaveValue(/name: Dana \(developer\)/);
  // YAML -> canvas
  const text = (await yaml.inputValue()).replace("name: Dana (developer)", "name: Grace");
  await yaml.fill(text);
  await expect(node(page, "dana")).toContainText("Grace");
  await expect(page.getByRole("status").filter({ hasText: "In sync with the canvas." })).toBeVisible();
  // an invalid edit is explained and changes nothing
  await yaml.fill("agent: {id: someone-else}");
  await expect(page.getByRole("alert")).toContainText("agent.id");
  await expect(node(page, "dana")).toContainText("Grace");
  // canvas -> YAML (undo goes through the document)
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(yaml).toHaveValue(/name: Dana \(developer\)/);
});

test("Export YAML downloads a nuramesh.yaml", async ({ page }) => {
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export YAML" }).click()]);
  expect(download.suggestedFilename()).toBe("dev-team.yaml");
  const path = await download.path();
  const { readFileSync } = await import("node:fs");
  const text = readFileSync(path, "utf8");
  expect(text).toMatch(/^version: 1\n/);
  expect(text).toContain("checks:");
  expect(text).toContain("policies:");
});

test("starts a run from the header", async ({ page }) => {
  await page.getByRole("button", { name: "Start", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/run-/);
});

for (const scheme of ["light", "dark"] as const) {
  test(`passes axe with an agent open on Settings and on YAML (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await node(page, "dana").click();
    await expectNoSeriousA11yViolations(page);
    await page.getByRole("tab", { name: "YAML" }).click();
    await expectNoSeriousA11yViolations(page);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`${String(width)} px: header and actions fit, nothing scrolls sideways`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    if (width < 768) {
      // Phone mode (ND65): a read-only view with a notice instead of the editing header.
      await expect(page.getByRole("note").filter({ hasText: "larger screen" })).toBeVisible();
    } else {
      await expect(page.getByTestId("validity-chip")).toBeVisible();
      await expect(page.getByRole("button", { name: "Export YAML" })).toBeVisible();
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
  });
}

test("no console errors while editing", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await node(page, "dana").click();
  await page.getByRole("tab", { name: "YAML" }).click();
  await page.getByRole("tab", { name: "Settings" }).click();
  await page.getByRole("button", { name: /^Add Human approver/ }).click();
  await page.getByRole("tab", { name: "Roles" }).click();
  await expect(page.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");
  expect(errors).toEqual([]);
});
