import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import type { AuthApi, AuthState } from "@/auth/types";
import { renderAt, serverLike } from "@/test/server-like";

/**
 * C37 (first-run spec FR-15): the page works at 390 px (ND65). jsdom has no layout, so this checks
 * what it can: a phone-sized window, no fixed or no-wrap sizing that would force a sideways scroll, and a touch
 * target of at least 44 px (`min-h-11`) on every link and button. The real-pixel check of the shared shell is
 * e2e/phone.spec.ts (it runs on the sample source, where /start is not shown).
 */
const state: AuthState = {
  kind: "signed-in",
  user: { id: "user-one", orgId: "org-one", role: "owner", email: "owner@example.test", displayName: "Owner" },
};
const auth: AuthApi = {
  state: () => state,
  subscribe: () => () => undefined,
  load: () => Promise.resolve(),
  login: () => Promise.resolve({ kind: "signed-in" as const }),
  completeMfa: () => Promise.resolve(),
  logout: () => Promise.resolve(),
  setupStatus: () => Promise.resolve(false),
  redeemSetup: () => Promise.resolve(),
};
const client = (): ApiClient => ({
  ...serverLike({
    firstRun: {
      runs: () => Promise.resolve({ runs: [] }),
      approvals: () => Promise.resolve({ items: [], next_cursor: null }),
      invites: () => Promise.resolve({ invites: [], next_cursor: null }),
    },
  }),
  source: "server",
  auth,
});

const widthBefore = window.innerWidth;
beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("max-width: 767px"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    onchange: null,
    dispatchEvent: () => false,
  }));
});
afterEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: widthBefore });
  vi.unstubAllGlobals();
});

describe("C37: /start at 390 px", () => {
  it("has no sizing that forces a sideways scroll", async () => {
    renderAt("/start", client());
    const page = await screen.findByRole("region", { name: "Get started" });
    const forced = /(^|\s)(w-\[|min-w-|whitespace-nowrap|overflow-x-)/;
    const bad = [page, ...Array.from(page.querySelectorAll("*"))]
      // The shared Button sets whitespace-nowrap; its labels here are short, so only the page's own markup is checked.
      .filter((el) => el.tagName !== "BUTTON" && forced.test(el.getAttribute("class") ?? ""))
      .map((el) => el.tagName.toLowerCase());
    expect(bad).toEqual([]);
    expect(page.className).toContain("max-w-2xl");
  });

  it("gives every link and button a touch target of 44 px", async () => {
    renderAt("/start", client());
    await screen.findByRole("link", { name: "Open approvals" });
    const page = screen.getByRole("region", { name: "Get started" });
    const small = Array.from(page.querySelectorAll("a, button"))
      .filter((el) => !(el.getAttribute("class") ?? "").includes("min-h-11"))
      .map((el) => el.textContent);
    expect(small).toEqual([]);
  });
});
