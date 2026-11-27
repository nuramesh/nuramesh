import { expect, type Page, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./a11y";
import {
  center,
  connect,
  drag,
  edges,
  expectAgents,
  fitView,
  importJson,
  loadTemplate,
  mod,
  node,
  openBuilder,
} from "./builder-helpers";

const roleList = (page: Page) => page.getByRole("list", { name: "Roles in this organization" });

test.beforeEach(async ({ page }) => {
  await openBuilder(page);
});

test("adds agents from the palette and validates live", async ({ page }) => {
  await expect(page.getByText(/Drag a role from the palette/)).toBeVisible();
  await page.getByRole("button", { name: /^Add Chief of Staff/ }).click();
  await expectAgents(page, 1);
  await expect(page.getByRole("tab", { name: "Problems" })).toHaveText("Problems");

  // With the Chief of Staff selected, a new agent reports to it.
  await page.getByRole("button", { name: /^Add Developer/ }).click();
  await expectAgents(page, 2);
  await expect(edges(page)).toHaveCount(1);
  await expect(page.getByRole("combobox", { name: "Reports to" })).toHaveValue("chief-of-staff");
  await expect(page.getByText("2 agents · valid")).toBeVisible();
});

test("drops a role onto the canvas where the user releases it", async ({ page }) => {
  const pane = page.locator(".react-flow__pane");
  await page.getByRole("button", { name: /^Add Chief of Staff/ }).dragTo(pane, { targetPosition: { x: 300, y: 300 } });
  await expectAgents(page, 1);
  const box = await node(page, "chief-of-staff").boundingBox();
  const paneBox = await pane.boundingBox();
  if (box === null || paneBox === null) throw new Error("not visible");
  // The node is centred on the drop point (the view may have been fitted afterwards, so allow slack).
  expect(Math.abs(box.x + box.width / 2 - (paneBox.x + 300))).toBeLessThan(box.width);
});

test("connects agents by dragging handles; a new manager replaces the old one", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await expect(edges(page)).toHaveCount(4);

  await connect(page, "developer", "tester");
  await expect(page.locator('.react-flow__edge[data-id="edge:tester"]')).toHaveCount(1);
  await node(page, "tester").click();
  await expect(page.getByRole("combobox", { name: "Reports to" })).toHaveValue("developer");
  await expect(edges(page)).toHaveCount(4); // still one line per agent

  // A line that would close a loop is refused.
  await connect(page, "tester", "developer");
  await node(page, "developer").click();
  await expect(page.getByRole("combobox", { name: "Reports to" })).toHaveValue("chief");
});

test("undo and redo every edit, with buttons and keyboard", async ({ page }) => {
  await loadTemplate(page, "Code review");
  await expectAgents(page, 4);

  await node(page, "code-reviewer").click();
  await page.getByLabel("Name", { exact: true }).fill("Alice");
  await expect(node(page, "code-reviewer")).toContainText("Alice");
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });

  await page.keyboard.press(`${mod}+z`);
  await expect(node(page, "code-reviewer")).toContainText("Code reviewer");
  await page.keyboard.press(`${mod}+Shift+z`);
  await expect(node(page, "code-reviewer")).toContainText("Alice");
  await page.keyboard.press("Control+y"); // nothing left to redo: no change
  await expect(node(page, "code-reviewer")).toContainText("Alice");

  await node(page, "security-reviewer").click();
  await page.keyboard.press("Delete");
  await expectAgents(page, 3);
  await page.getByRole("button", { name: "Undo" }).click();
  await expectAgents(page, 4);
  await page.getByRole("button", { name: "Redo" }).click();
  await expectAgents(page, 3);

  // Undo all the way back to before the template, then redo it.
  for (let i = 0; i < 3; i += 1) await page.getByRole("button", { name: "Undo" }).click();
  await expectAgents(page, 0);
  await expect(page.getByRole("button", { name: "Undo" })).toBeDisabled();
  await page.getByRole("button", { name: "Redo" }).click();
  await expectAgents(page, 4);
});

test("auto-layout keeps manual positions until asked, then re-arranges", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  const tester = node(page, "tester");
  const before = await center(tester);
  await drag(page, before, { x: before.x + 150, y: before.y + 120 });
  const moved = await center(tester);
  expect(moved.y - before.y).toBeGreaterThan(80);

  // Adding an agent re-runs layout for automatic nodes only; the moved one stays.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await page.getByRole("button", { name: /^Add Reviewer/ }).click();
  await expectAgents(page, 6);
  const stayed = await center(tester);
  expect(Math.abs(stayed.x - moved.x)).toBeLessThan(2);
  expect(Math.abs(stayed.y - moved.y)).toBeLessThan(2);

  await page.getByRole("button", { name: "Auto-layout" }).click();
  await expect
    .poll(async () => {
      const [t, chief] = await Promise.all([tester.boundingBox(), node(page, "chief").boundingBox()]);
      return t !== null && chief !== null && t.y > chief.y + chief.height && Math.abs(t.y - (chief.y + chief.height)) < 200;
    })
    .toBe(true);
  // Every report sits below its manager.
  const chief = await node(page, "chief").boundingBox();
  for (const id of ["developer", "devops", "tester", "reviewer"]) {
    const box = await node(page, id).boundingBox();
    expect(box?.y ?? 0).toBeGreaterThan((chief?.y ?? 0) + (chief?.height ?? 0));
  }
});

test("multi-select, select all, copy/paste, duplicate and cut", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);

  await node(page, "developer").click();
  await node(page, "tester").click({ modifiers: ["Shift"] });
  await expect(page.getByRole("heading", { name: "2 agents selected" })).toBeVisible();

  await page.keyboard.press(`${mod}+c`);
  await page.keyboard.press(`${mod}+v`);
  await expectAgents(page, 7);
  await fitView(page);
  await expect(node(page, "developer-2")).toContainText("Developer (copy)");

  await page.keyboard.press(`${mod}+d`);
  await expectAgents(page, 9);

  await page.keyboard.press(`${mod}+a`);
  await expect(page.getByRole("heading", { name: "9 agents selected" })).toBeVisible();

  await fitView(page);
  // Clicking a node inside a multi-selection keeps the group (React Flow), so clear it first.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await node(page, "reviewer").click();
  await expect(page.getByRole("heading", { name: "Reviewer", level: 3 })).toBeVisible();
  await page.keyboard.press(`${mod}+x`);
  await expectAgents(page, 8);
  await page.keyboard.press(`${mod}+v`);
  await expectAgents(page, 9);
});

test("box select with Shift-drag", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  const pane = await page.locator(".react-flow__pane").boundingBox();
  if (pane === null) throw new Error("pane not visible");
  await page.keyboard.down("Shift");
  await drag(page, { x: pane.x + 4, y: pane.y + 4 }, { x: pane.x + pane.width - 4, y: pane.y + pane.height - 4 });
  await page.keyboard.up("Shift");
  await expect(page.getByRole("heading", { name: "5 agents selected" })).toBeVisible();
});

test("problems panel lists issues and focuses the agent", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await node(page, "tester").click();
  await page.getByRole("combobox", { name: "Reports to" }).selectOption("");
  await expect(page.getByRole("tab", { name: /Problems/ })).toContainText("3");
  await page.getByRole("tab", { name: /Problems/ }).click();
  const problem = page.getByRole("button", { name: /The top agent 'tester' must have a Chief of Staff role/ });
  await expect(problem).toBeVisible();
  await expect(node(page, "tester").getByText("2")).toBeVisible(); // two errors on the node

  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await problem.click();
  await expect(node(page, "tester")).toBeFocused();
  await expect(node(page, "tester")).toHaveClass(/selected/);
});

test("edits roles: create, rename id (agents follow), and protect roles in use", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await page.getByRole("tab", { name: "Roles" }).click();
  await page.getByRole("button", { name: "New role" }).click();
  await page.getByLabel("Role name").fill("Designer");
  await expect(roleList(page).getByRole("button", { name: /Designer/ })).toBeVisible();
  await page.getByLabel("Role id").fill("Bad Id");
  await expect(page.getByText(/Use 1–63 lower-case letters/)).toBeVisible();
  await page.getByLabel("Role id").fill("designer");
  await page.getByRole("button", { name: "Delete role" }).click();
  await expect(roleList(page).getByRole("button", { name: /Designer/ })).toHaveCount(0);

  await roleList(page).getByRole("button", { name: /^Tester/ }).click();
  await expect(page.getByRole("button", { name: "Delete role" })).toBeDisabled();
  await page.getByLabel("Role id").fill("qa");
  await page.getByRole("tab", { name: "Properties" }).click();
  await node(page, "tester").click();
  await expect(page.getByRole("combobox", { name: "Role", exact: true })).toHaveValue("qa");
});

test("templates: save, reload and delete a user template", async ({ page }) => {
  await loadTemplate(page, "Code review");
  await expectAgents(page, 4);
  await page.getByRole("button", { name: "Save as template" }).click();
  await page.getByLabel("Template name").fill("My reviewers");
  await page.getByRole("button", { name: "Save template" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();

  await page.reload();
  await loadTemplate(page, "Blank organization");
  await expectAgents(page, 0);
  await loadTemplate(page, "My reviewers");
  await expectAgents(page, 4);

  await page.getByRole("button", { name: "Templates" }).click();
  await page.getByRole("menuitem", { name: "Manage saved templates…" }).click();
  await page.getByRole("button", { name: "Delete template My reviewers" }).click();
  await expect(page.getByText("No saved templates yet.")).toBeVisible();
});

test("keeps the draft across reloads", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await page.waitForTimeout(500); // autosave delay
  await page.reload();
  await expectAgents(page, 5);
});

test("exports JSON the Python model accepts and imports it back", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export JSON" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("dev-team.nuramesh.json");
  const stream = await file.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(chunk as Buffer);
  const exported = JSON.parse(Buffer.concat(chunks).toString("utf8")) as {
    schema_version: number;
    org: { agents: Record<string, unknown>[]; roles: Record<string, unknown>[] };
  };
  expect(exported.schema_version).toBe(1);
  expect(Object.keys(exported.org.agents[0] ?? {}).sort()).toEqual(["checks", "id", "model", "name", "reports_to", "role"]);
  expect(Object.keys(exported.org.roles[0] ?? {}).sort()).toEqual(
    ["default_model", "description", "id", "instructions", "kind", "name", "tools"],
  );

  await loadTemplate(page, "Blank organization");
  await expectAgents(page, 0);
  await importJson(page, "dev-team.nuramesh.json", exported);
  await expectAgents(page, 5);
  await expect(page.getByText("Imported dev-team.nuramesh.json. Undo to go back.")).toBeVisible();
});

test("rejects invalid imports with clear errors and changes nothing", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await importJson(page, "broken.json", "{ not json");
  await expect(page.getByRole("dialog", { name: "Couldn't import broken.json" })).toContainText("Not valid JSON");
  await page.getByRole("button", { name: "Close" }).first().click();

  await importJson(page, "bad.json", {
    schema_version: 2,
    org: { id: "Bad", name: "", roles: [], agents: [{ id: "a", name: "A", role: "x", extra: true }] },
    layout: { positions: { ghost: { x: 0, y: 0 } } },
  });
  const dialog = page.getByRole("dialog", { name: "Couldn't import bad.json" });
  await expect(dialog).toContainText("schema_version: Unsupported schema version 2");
  await expect(dialog).toContainText("org.id:");
  await expect(dialog).toContainText("org.agents[0].extra: Unknown field");
  await page.keyboard.press("Escape");
  await expectAgents(page, 5);
});

test("builder passes axe with a template loaded, in both themes and with a dialog open", async ({ page }) => {
  await loadTemplate(page, "Dev team");
  await expectAgents(page, 5);
  await node(page, "tester").click();
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("tab", { name: "Roles" }).click();
  await roleList(page).getByRole("button", { name: /^Developer/ }).click();
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("tab", { name: /Problems/ }).click();
  await expectNoSeriousA11yViolations(page);

  await page.getByRole("button", { name: /^Theme:/ }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  await expect(page.getByRole("menu")).toBeHidden();
  await expectNoSeriousA11yViolations(page);

  await page.getByRole("button", { name: "Save as template" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});

test("keyboard only: add, select, move and delete an agent", async ({ page }) => {
  await page.getByRole("button", { name: /^Add Chief of Staff/ }).focus();
  await page.keyboard.press("Enter");
  await expectAgents(page, 1);
  const chief = node(page, "chief-of-staff");
  await chief.focus();
  await expect(chief).toBeFocused();
  const before = await center(chief);
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => (await center(chief)).x).toBeGreaterThan(before.x);
  await page.keyboard.press("Delete");
  await expectAgents(page, 0);
  await page.keyboard.press(`${mod}+z`);
  await expectAgents(page, 1);
});
