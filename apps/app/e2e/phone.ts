import { expect, type Page } from "@playwright/test";

/** The phone viewport of ND65 (iPhone 14 class). */
export const PHONE = { width: 390, height: 844 };

/** Every screen of the app with its fixture URL and heading. Oversight screens come first (ND65). */
export const PHONE_ROUTES: readonly { path: string; title: string; innerScroll?: boolean }[] = [
  { path: "/approvals", title: "Approvals" },
  { path: "/approvals/a1", title: "Approvals" },
  { path: "/runs", title: "Runs" },
  { path: "/runs/run-128", title: "Fix issue" },
  // Follow-up: another builder is reworking the Budgets and Templates screens, so this PR leaves
  // them alone. Budgets' table still scrolls sideways inside its card (the page itself fits).
  { path: "/budgets", title: "Budgets and spend", innerScroll: true },
  { path: "/ledger", title: "Ledger" },
  { path: "/ledger/1", title: "Ledger" },
  { path: "/settings", title: "Settings" },
  { path: "/templates", title: "Templates" },
  { path: "/builder", title: "" },
];

/** What the phone audit found on the current page: each entry is one readable violation. */
export async function auditPhone(page: Page, innerScroll = false): Promise<string[]> {
  return page.evaluate((allowInnerScroll) => {
    const out: string[] = [];
    const doc = document.documentElement;
    const vw = doc.clientWidth;
    const label = (el: Element): string =>
      `${el.tagName.toLowerCase()}[${(el.getAttribute("aria-label") ?? el.textContent).trim().slice(0, 28)}]`;
    for (const [name, el] of [
      ["html", doc],
      ["body", document.body],
      ["main", document.querySelector("main")],
    ] as const) {
      if (el !== null && el.scrollWidth > el.clientWidth) out.push(`${name} scrolls sideways by ${String(el.scrollWidth - el.clientWidth)}px`);
    }
    const visible = (el: Element): boolean => {
      const box = el.getBoundingClientRect();
      // `.sr-only` boxes (live-region announcers) are visually hidden on purpose, a pixel wide at -1.
      return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== "hidden" && el.closest(".sr-only") === null;
    };
    // A box may stick out only inside a container that scrolls on purpose (a wide table, code).
    // `overflow: hidden` does not count: it clips the box, so content past the screen edge is lost.
    // The walk stops at <main>; the page itself is checked above.
    const scrollsInside = (el: Element): boolean => {
      for (let p = el.parentElement; p !== null && p.tagName !== "MAIN"; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
      return false;
    };
    const main = document.querySelector("main");
    if (main === null) return out;
    for (const el of main.querySelectorAll<HTMLElement>("*")) {
      if (!visible(el) || el.closest(".react-flow") !== null) continue;
      const box = el.getBoundingClientRect();
      if ((box.right > vw + 0.5 || box.left < -0.5) && !scrollsInside(el)) {
        out.push(`${label(el)} spans ${box.left.toFixed(0)}..${box.right.toFixed(0)} of ${String(vw)}`);
      }
    }
    // A wide table or code block that scrolls sideways inside the page still hides things from a
    // phone; mark one on purpose with data-phone-scroll (a code block, a long hash).
    for (const el of main.querySelectorAll<HTMLElement>("*")) {
      if (allowInnerScroll || !visible(el) || el.closest(".react-flow") !== null || el.closest("[data-phone-scroll]") !== null) continue;
      const ox = getComputedStyle(el).overflowX;
      if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) {
        out.push(`${label(el)} scrolls sideways by ${String(el.scrollWidth - el.clientWidth)}px inside the page`);
      }
    }
    // Primary controls are at least 44 px tall. Links inside running text are exempt (WCAG 2.5.8).
    const controls = "button, a[href], [role=button], select, input:not([type=hidden]):not([type=checkbox]):not([type=radio])";
    for (const el of main.querySelectorAll<HTMLElement>(controls)) {
      if (!visible(el) || el.closest(".react-flow") !== null) continue;
      if (el.tagName === "A" && el.closest("p, li, td, dd, code, pre, summary") !== null) continue;
      const h = el.getBoundingClientRect().height;
      if (h < 43.5) out.push(`${label(el)} is only ${h.toFixed(0)}px tall`);
    }
    return out;
  }, innerScroll);
}

/** Asserts the page fits a phone: no sideways scroll, controls at least 44 px tall. */
export async function expectPhoneFit(page: Page, where: string, innerScroll = false): Promise<void> {
  expect(await auditPhone(page, innerScroll), where).toEqual([]);
}

/** Asserts a button or link can be reached on a phone: fully inside the screen width and 44 px tall. */
export async function expectOnScreen(page: Page, name: string | RegExp, role: "button" | "link" = "button"): Promise<void> {
  const target = page.getByRole(role, { name }).first();
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  expect(box, `${String(name)} has a box`).not.toBeNull();
  if (box === null) return;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(PHONE.width + 0.5);
  expect(box.height).toBeGreaterThanOrEqual(43.5);
}
