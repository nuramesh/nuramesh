import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** Runs axe (WCAG 2.2 A/AA rules) and fails on any serious or critical violation. */
export async function expectNoSeriousA11yViolations(page: Page): Promise<void> {
  // Let enter/exit animations settle: mid-fade text would fail colour contrast spuriously.
  await page.evaluate(async () => {
    // allSettled: an animation cancelled mid-way (closing menu) rejects `finished`.
    await Promise.allSettled(document.getAnimations().map((animation) => animation.finished));
  });
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blocking = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${String(v.impact)}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(blocking, "serious/critical accessibility violations").toEqual([]);
}

/**
 * Runs only axe's `color-contrast` rule (text 4.5:1, large text 3:1) and fails on any violation of
 * any impact. Returns how many elements it checked, so a spec can prove the rule really ran.
 */
export async function expectNoContrastViolations(page: Page, label: string): Promise<number> {
  await page.evaluate(async () => {
    await Promise.allSettled(document.getAnimations().map((animation) => animation.finished));
  });
  const results = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
  const bad = results.violations.flatMap((v) => v.nodes.map((n) => `${n.target.join(" ")}: ${n.any[0]?.message ?? v.help}`));
  expect(bad, `${label}: color-contrast violations`).toEqual([]);
  return results.passes.reduce((sum, p) => sum + p.nodes.length, 0);
}
