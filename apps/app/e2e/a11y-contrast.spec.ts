import { expect, type Page, test } from "@playwright/test";

import { expectNoContrastViolations, expectNoSeriousA11yViolations } from "./a11y";
import { expectAgents, loadTemplate, openBuilder } from "./builder-helpers";

/**
 * Every screen in both themes, with the `color-contrast` rule on (and the full serious/critical
 * check). Light was the owner's complaint; dark must keep passing.
 */
const SCREENS: [string, (page: Page) => Promise<void>][] = [
  [
    "builder (Dev team loaded)",
    async (page) => {
      await openBuilder(page);
      await loadTemplate(page, "Dev team");
      await expectAgents(page, 5);
    },
  ],
  ["templates", async (page) => { await page.goto("/templates"); await expect(page.getByRole("heading", { level: 1, name: "Templates" })).toBeVisible(); }],
  ["runs", async (page) => { await page.goto("/runs"); await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible(); }],
  ["run detail", async (page) => { await page.goto("/runs/run-128"); await expect(page.getByRole("heading", { level: 1, name: "Fix issue" })).toBeVisible(); }],
  [
    "approvals (pending: reason box and disabled button)",
    async (page) => {
      await page.goto("/approvals/a1");
      await expect(page.getByRole("textbox", { name: /Your reason/ })).toBeVisible();
      await expect(page.getByRole("button", { name: "Approve" })).toBeDisabled();
    },
  ],
  ["budgets", async (page) => { await page.goto("/budgets"); await expect(page.getByRole("heading", { level: 1, name: "Budgets and spend" })).toBeVisible(); }],
  ["ledger", async (page) => { await page.goto("/ledger"); await expect(page.getByRole("heading", { level: 1, name: "Ledger" })).toBeVisible(); }],
  ["settings", async (page) => { await page.goto("/settings"); await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible(); }],
  ...orgSafetyScreens(),
];

/**
 * E19 (spec/e19/README.md section 12): the status bar in each state, the roles area, the breakers
 * panel with an ok, a tripped, a disabled and a dry-run row, and each of the seven dialogs open.
 * The fixture state comes from `?orgSafety=` (fixture data only).
 */
function orgSafetyScreens(): [string, (page: Page) => Promise<void>][] {
  const bar = (page: Page) => page.getByRole("region", { name: "Organization status" });
  const at = async (page: Page, path: string): Promise<void> => {
    await page.goto(path);
    await expect(bar(page)).toBeVisible();
  };
  return [
    ["org safety: running bar", async (page) => { await at(page, "/templates"); await expect(bar(page).getByText("Organization running")).toBeVisible(); }],
    ["org safety: halted bar (human)", async (page) => { await at(page, "/templates?orgSafety=halted"); await expect(bar(page).getByText("Organization halted")).toBeVisible(); }],
    ["org safety: halted bar (failing checks)", async (page) => { await at(page, "/templates?orgSafety=poison"); await expect(bar(page).getByText(/Cause: KeyError/)).toBeVisible(); }],
    ["org safety: roles area with a paused role", async (page) => { await at(page, "/templates?orgSafety=rolePaused"); await expect(bar(page).getByRole("button", { name: "Resume role researcher" })).toBeVisible(); }],
    ["org safety: breakers panel (ok, tripped, dry run)", async (page) => { await page.goto("/budgets?orgSafety=tripped"); await expect(page.getByRole("button", { name: "Reset breaker denied-loop" })).toBeVisible(); }],
    ["org safety: breakers panel (disabled)", async (page) => { await page.goto("/budgets?orgSafety=disabled"); await expect(page.getByRole("button", { name: "Enable breaker watch-gates" })).toBeVisible(); }],
    ["org safety: breakers panel (checks failing)", async (page) => { await page.goto("/budgets?orgSafety=failing"); await expect(page.getByText(/Breaker checks are failing since/)).toBeVisible(); }],
    ["org safety: breakers panel (viewer)", async (page) => { await page.goto("/budgets?orgSafety=viewer"); await expect(page.getByText("Only a signed-in person can reset, disable or enable a breaker.")).toBeVisible(); }],
    ["org safety: halt dialog", async (page) => { await at(page, "/templates"); await bar(page).getByRole("button", { name: "Halt organization" }).click(); await expect(page.getByRole("dialog", { name: "Halt the whole organization?" })).toBeVisible(); }],
    ["org safety: resume dialog", async (page) => { await at(page, "/templates?orgSafety=tripped"); await bar(page).getByRole("button", { name: "Resume organization" }).click(); await expect(page.getByRole("dialog", { name: "Resume the organization?" })).toBeVisible(); await expect(page.getByText("Breaker denied-loop is still tripped.")).toBeVisible(); }],
    ["org safety: pause role dialog", async (page) => { await at(page, "/templates"); await bar(page).getByRole("button", { name: "Roles" }).click(); await bar(page).getByRole("button", { name: "Pause role writer" }).click(); await expect(page.getByRole("dialog", { name: "Pause role writer?" })).toBeVisible(); }],
    ["org safety: resume role dialog", async (page) => { await at(page, "/templates?orgSafety=rolePaused"); await bar(page).getByRole("button", { name: "Resume role researcher" }).click(); await expect(page.getByRole("dialog", { name: "Resume role researcher?" })).toBeVisible(); }],
    ["org safety: reset dialog", async (page) => { await page.goto("/budgets?orgSafety=tripped"); await page.getByRole("button", { name: "Reset breaker denied-loop" }).click(); await expect(page.getByRole("dialog", { name: "Reset breaker denied-loop?" })).toBeVisible(); }],
    ["org safety: disable dialog", async (page) => { await page.goto("/budgets"); await page.getByRole("button", { name: "Disable breaker spend-spike" }).click(); await expect(page.getByRole("dialog", { name: "Disable breaker spend-spike?" })).toBeVisible(); }],
    ["org safety: enable dialog", async (page) => { await page.goto("/budgets?orgSafety=disabled"); await page.getByRole("button", { name: "Enable breaker watch-gates" }).click(); await expect(page.getByRole("dialog", { name: "Enable breaker watch-gates?" })).toBeVisible(); }],
  ];
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`axe on every screen, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme });
    for (const [name, open] of SCREENS) {
      test(`${name}: no colour-contrast violations and no serious findings`, async ({ page }) => {
        await open(page);
        await expect(page.locator("html")).toHaveClass(scheme === "dark" ? /dark/ : /^(?!.*dark)/);
        const checked = await expectNoContrastViolations(page, `${name} (${scheme})`);
        expect(checked, "color-contrast actually checked elements").toBeGreaterThan(10);
        await expectNoSeriousA11yViolations(page);
      });
    }
  });
}

/** E19 at 360 px, both themes: the page never scrolls sideways; the breakers table scrolls inside its own labelled region. */
for (const scheme of ["light", "dark"] as const) {
  test.describe(`org safety at 360 px, ${scheme} theme`, () => {
    test.use({ colorScheme: scheme, viewport: { width: 360, height: 740 } });
    const noPageScroll = async (page: Page): Promise<void> => {
      const { scrollWidth, clientWidth } = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
      expect(scrollWidth, "horizontal page scroll").toBeLessThanOrEqual(clientWidth);
    };
    for (const state of ["running", "halted", "poison", "rolePaused"]) {
      test(`status bar (${state}) wraps and fits`, async ({ page }) => {
        await page.goto(`/templates?orgSafety=${state}`);
        await expect(page.getByRole("region", { name: "Organization status" })).toBeVisible();
        await noPageScroll(page);
        await expectNoContrastViolations(page, `bar ${state} (${scheme}, 360)`);
      });
    }
    test("breakers panel: the table scrolls inside its region, the page does not", async ({ page }) => {
      await page.goto("/budgets?orgSafety=tripped");
      const region = page.getByRole("region", { name: "Circuit breakers table" });
      await expect(region).toBeVisible();
      await noPageScroll(page);
      const { scrollWidth, clientWidth } = await region.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
      expect(scrollWidth, "the table region scrolls inside itself").toBeGreaterThan(clientWidth);
      await region.focus();
      await expect(region).toBeFocused();
    });
    test("the dialogs fit", async ({ page }) => {
      await page.goto("/budgets?orgSafety=tripped");
      await page.getByRole("button", { name: "Reset breaker denied-loop" }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      const box = await dialog.boundingBox();
      expect(box, "the dialog has a box").not.toBeNull();
      const { x, width } = box ?? { x: -1, width: 0 };
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x + width).toBeLessThanOrEqual(360);
      await noPageScroll(page);
    });
  });
}
