import type { Page } from "@playwright/test";

/** What one layout audit found: every entry is a human-readable violation. */
export interface AuditResult {
  violations: string[];
  /** `innerWidth - clientWidth`: the width a classic scrollbar takes from the layout viewport. */
  scrollbar: number;
  innerWidth: number;
  clientWidth: number;
}

/**
 * Measures the builder in the page: no horizontal overflow on html, body or main; every control in
 * the header and toolbar inside the layout viewport (`clientWidth`, which excludes a classic
 * scrollbar); the canvas floating buttons inside the canvas box; the Properties panel inside the
 * viewport with every field inside the panel.
 */
export async function auditBuilder(page: Page): Promise<AuditResult> {
  return page.evaluate(() => {
    const violations: string[] = [];
    const doc = document.documentElement;
    const vw = doc.clientWidth;
    const name = (el: Element): string =>
      `${el.tagName.toLowerCase()}[${(el.getAttribute("aria-label") ?? el.textContent).trim().slice(0, 24)}]`;
    const visible = (el: Element): boolean => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && (el as HTMLElement).offsetParent !== null;
    };
    const main = document.querySelector("main");
    for (const [label, el] of [
      ["html", doc],
      ["body", document.body],
      ["main", main],
    ] as const) {
      if (el !== null && el.scrollWidth > el.clientWidth) violations.push(`${label} overflows by ${String(el.scrollWidth - el.clientWidth)}px`);
    }
    const inside = (scope: string, selector: string, right: number, left = 0): void => {
      for (const el of document.querySelectorAll<HTMLElement>(selector)) {
        if (!visible(el)) continue;
        const box = el.getBoundingClientRect();
        if (box.left < left - 0.5 || box.right > right + 0.5) {
          violations.push(`${scope}: ${name(el)} spans ${box.left.toFixed(1)}..${box.right.toFixed(1)} outside ${left}..${right.toFixed(1)}`);
        }
      }
    };
    const header = '[aria-label="Organization"]';
    inside("header", `${header} button, ${header} a, ${header} [data-testid]`, vw);
    inside("toolbar", '[aria-label="Canvas"] button', vw);
    const canvas = document.querySelector('[aria-label="Organization canvas"]');
    if (canvas !== null) {
      const c = canvas.getBoundingClientRect();
      inside("canvas buttons", '[aria-label="Organization canvas"] button', c.right, c.left);
      if (c.right > vw + 0.5) violations.push(`canvas right ${c.right.toFixed(1)} > viewport ${String(vw)}`);
    }
    const panel = document.querySelector<HTMLElement>('aside[aria-label="Details"]');
    if (panel !== null && visible(panel)) {
      const p = panel.getBoundingClientRect();
      if (p.right > vw + 0.5) violations.push(`Properties panel right ${p.right.toFixed(1)} > viewport ${String(vw)}`);
      for (const el of panel.querySelectorAll<HTMLElement>("input, textarea, select, button")) {
        if (!visible(el)) continue;
        const box = el.getBoundingClientRect();
        if (box.right > p.right + 0.5) violations.push(`panel field ${name(el)} right ${box.right.toFixed(1)} > panel ${p.right.toFixed(1)}`);
      }
      if (panel.scrollWidth > panel.clientWidth) violations.push(`Properties panel scrolls sideways by ${String(panel.scrollWidth - panel.clientWidth)}px`);
    }
    return { violations, scrollbar: window.innerWidth - vw, innerWidth: window.innerWidth, clientWidth: vw };
  });
}
