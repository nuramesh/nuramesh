import { createRequire } from "node:module";

import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import type { AuthApi, AuthState } from "@/auth/types";
import { renderAt, serverLike } from "@/test/server-like";

// axe-core is a dependency of @axe-core/playwright (already in package.json); resolve it from there so this
// slice adds no dependency (spec FR-16, C45).
const requireFrom = createRequire(createRequire(import.meta.url).resolve("@axe-core/playwright"));
/** The part of axe-core's API this test uses (its types are not a dependency of the app either). */
interface Axe {
  run(
    context: { include: string[][]; exclude: string[][] },
    options: { runOnly: { type: "tag"; values: string[] }; rules: Record<string, { enabled: boolean }> },
  ): Promise<{ violations: { id: string; nodes: { target: string[] }[] }[] }>;
}
const axe = requireFrom("axe-core") as Axe;

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

function client(reads: { runs: unknown; approvals: unknown; invites: unknown }): ApiClient {
  const base = serverLike({
    firstRun: {
      runs: () => Promise.resolve(reads.runs),
      approvals: () => Promise.resolve(reads.approvals),
      invites: () => Promise.resolve(reads.invites),
    },
  });
  return { ...base, source: "server", auth };
}

const EMPTY = { runs: { runs: [] }, approvals: { items: [], next_cursor: null }, invites: { invites: [], next_cursor: null } };
const ALL_DONE = {
  runs: { runs: [{ id: "r1" }] },
  approvals: { items: [{ status: "approved" }], next_cursor: null },
  invites: { invites: [{ id: "i1" }], next_cursor: null },
};

/**
 * jsdom has no layout or paint, so colour contrast is covered by the browser axe run (e2e/shell.spec.ts). The two
 * shell logo links (a.text-foreground) are left out: their name is the SVG <title> "NuraMesh", which jsdom does not
 * compute (a real browser does, and e2e/shell.spec.ts passes axe on every route). Nothing on /start has that class.
 * `region` is off because the page is rendered without the shell, so there is no landmark for axe to find; the
 * shell supplies the landmarks and e2e/shell.spec.ts checks them in a real browser.
 */
async function violations(): Promise<string[]> {
  const results = await axe.run({ include: [["body"]], exclude: [["a.text-foreground"]] }, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  document.documentElement.classList.remove("dark");
});

// C38: axe on /start in light and dark, in the three states the page can be in.
describe.each(["light", "dark"] as const)("/start accessibility (%s)", (theme) => {
  beforeEach(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  });

  it("has no axe violations for a new owner", async () => {
    renderAt("/start", client(EMPTY));
    await screen.findByRole("heading", { level: 1, name: "Get started" });
    await screen.findByRole("link", { name: "Open approvals" });
    expect(await violations()).toEqual([]);
  });

  it("has no axe violations when every step is done", async () => {
    renderAt("/start", client(ALL_DONE));
    await screen.findByRole("heading", { level: 2, name: "You are set up" });
    expect(await violations()).toEqual([]);
  });

  it("has no axe violations when a read failed", async () => {
    renderAt("/start", client({ ...EMPTY, runs: { nope: 1 } }));
    await screen.findByRole("button", { name: "Retry" });
    expect(await violations()).toEqual([]);
  });
});
