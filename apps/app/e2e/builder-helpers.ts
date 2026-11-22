import { expect, type Locator, type Page } from "@playwright/test";

export const nodes = (page: Page): Locator => page.locator(".react-flow__node");
export const node = (page: Page, id: string): Locator => page.locator(`.react-flow__node[data-id="${id}"]`);
/** The reporting lines (verification and approval lines have their own ids). */
export const edges = (page: Page): Locator => page.locator('.react-flow__edge[data-id^="edge:"]');
export const mod = process.platform === "darwin" ? "Meta" : "Control";

/**
 * Opens the builder with an empty canvas. The palette is set to its expanded choice first (a stored
 * choice, as a person would have made), so tests that use palette items work at any width; pass
 * `{ palette: "default" }` to see what a viewer with no stored choice gets (rail below 1600 px).
 */
export async function openBuilder(page: Page, options: { palette?: "expanded" | "default" } = {}): Promise<void> {
  if ((options.palette ?? "expanded") === "expanded") {
    await page.addInitScript(() => {
      if (window.localStorage.getItem("nuramesh.builder.palette-expanded") === null) {
        window.localStorage.setItem("nuramesh.builder.palette-expanded", "1");
      }
    });
  }
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Organization canvas" })).toBeVisible();
  await expect(page.locator(".react-flow__pane")).toBeVisible();
}

/** Starts from a built-in or saved template through the Templates menu. */
export async function loadTemplate(page: Page, name: string): Promise<void> {
  await page.getByRole("button", { name: "Templates" }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
  await expect(page.getByRole("menu")).toBeHidden();
}

/**
 * Waits until the document has `count` agents and automatic layout has finished. Counts the
 * document, not DOM nodes: the canvas renders only agents inside the viewport.
 */
export async function expectAgents(page: Page, count: number): Promise<void> {
  const summary = page.getByTestId("builder-summary");
  await expect(summary).toHaveText(new RegExp(`^${String(count)} agents? · (valid|\\d+ errors?)$`));
}

/** Fits every agent into view (so each one is rendered and clickable). */
export async function fitView(page: Page): Promise<void> {
  // The one Fit view button is in the canvas controls (React Flow labels it "Fit View").
  await page.getByRole("button", { name: /^fit view$/i }).click();
}

export async function center(locator: Locator): Promise<{ x: number; y: number }> {
  const box = await locator.boundingBox();
  if (box === null) throw new Error("element is not visible");
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

/** Drags with the mouse in small steps (React Flow needs intermediate moves). */
export async function drag(page: Page, from: { x: number; y: number }, to: { x: number; y: number }, steps = 12) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps });
  await page.mouse.up();
}

/**
 * The centre of a handle's visible half. A handle straddles the card edge and the card clips paint
 * (paint containment), so the exact centre can land a sub-pixel outside the clickable part at some
 * zoom levels; aim a quarter of the handle's height inward instead.
 */
async function handlePoint(locator: Locator, inward: 1 | -1): Promise<{ x: number; y: number }> {
  const box = await locator.boundingBox();
  if (box === null) throw new Error("element is not visible");
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 + (inward * box.height) / 4 };
}

/** Connects manager → report by dragging from the manager's bottom handle to the report's top handle. */
export async function connect(page: Page, managerId: string, reportId: string): Promise<void> {
  const from = await handlePoint(node(page, managerId).locator(".react-flow__handle-bottom"), -1);
  const to = await handlePoint(node(page, reportId).locator(".react-flow__handle-top"), 1);
  await drag(page, from, to);
}

/** Imports a JSON value (or raw text) through the file input. */
export async function importJson(page: Page, name: string, content: unknown): Promise<void> {
  const text = typeof content === "string" ? content : JSON.stringify(content);
  await page.getByTestId("import-file").setInputFiles({ name, mimeType: "application/json", buffer: Buffer.from(text) });
}

/**
 * An organization with `count` agents: a Chief of Staff, managers and workers (8 reports each).
 * With `grid`, every agent is pinned in a compact grid so the fitted view shows all of them at
 * once (the worst case for rendering; a 500-agent tree is too wide to show whole).
 */
export function generatedOrg(count: number, grid = false) {
  const roles = [
    { id: "chief-of-staff", name: "Chief of Staff", kind: "chief_of_staff" },
    { id: "lead", name: "Lead", kind: "worker" },
    { id: "developer", name: "Developer", kind: "worker" },
    { id: "reviewer", name: "Reviewer", kind: "verifier" },
  ];
  const agents: { id: string; name: string; role: string; reports_to: string | null }[] = [
    { id: "a0", name: "Chief of Staff", role: "chief-of-staff", reports_to: null },
  ];
  for (let i = 1; i < count; i += 1) {
    const manager = Math.floor((i - 1) / 8);
    agents.push({
      id: `a${String(i)}`,
      name: `Agent ${String(i)}`,
      role: i <= 8 ? "lead" : i % 5 === 0 ? "reviewer" : "developer",
      reports_to: `a${String(manager)}`,
    });
  }
  const positions: Record<string, { x: number; y: number }> = {};
  if (grid) {
    const columns = Math.ceil(Math.sqrt(count * 2));
    agents.forEach((agent, i) => {
      positions[agent.id] = { x: (i % columns) * 260, y: Math.floor(i / columns) * 116 };
    });
  }
  return { schema_version: 1, org: { id: "big", name: "Big org", roles, agents }, layout: { positions } };
}
