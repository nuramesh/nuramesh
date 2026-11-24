import { readFileSync } from "node:fs";

import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { createFixtureAuth } from "@/auth/fixture-auth";
import type { AuthUser } from "@/auth/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

import { landingFor, ROLE_RULES } from "@/auth/roles";
import { NAV_GROUPS, navGroupsFor } from "./nav";

// The five role values of the role notes (R2.13); the app's union has three until that lands, the rows are data.
const FIVE = ["owner", "admin", "builder", "approver", "viewer"] as const;
const labels = (role: string | null): string[] => navGroupsFor(role).flatMap((g) => g.items.map((i) => i.label));
const ALL = NAV_GROUPS.flatMap((g) => g.items.map((i) => i.label));

function renderAs(role: string | null, path = "/") {
  const base = createFixtureClient();
  const client = role === null ? base : { ...base, auth: createFixtureAuth({ account: { email: "p@example.test", password: "obviously-fake-test-pw", role: role as AuthUser["role"] }, signedIn: true }) };
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

describe("AE-1 landing by role", () => {
  const expected: [string | null, string][] = [
    ["approver", "/approvals/a1"],
    ["viewer", "/runs"],
    ["owner", "/builder"],
    ["admin", "/builder"],
    ["builder", "/builder"],
    ["banana", "/runs"],
    [null, "/builder"],
  ];
  for (const [role, path] of expected) {
    it(`${String(role)} lands on ${path}`, async () => {
      const router = renderAs(role);
      await screen.findByRole("navigation", { name: "Main" });
      await vi.waitFor(() => {
        expect(router.state.location.pathname).toBe(path);
      });
    });
  }
  it("a next path wins for every role", async () => {
    for (const role of FIVE) {
      const router = renderAs(role, "/approvals/a2");
      await screen.findByRole("navigation", { name: "Main" });
      expect(router.state.location.pathname).toBe("/approvals/a2");
      document.body.innerHTML = "";
    }
  });
});

describe("AE-1/AE-2 tables", () => {
  it("every role of the spec has a landing and a menu row; an unknown role is a viewer", () => {
    for (const r of FIVE) expect(ROLE_RULES[r]).toBeDefined();
    expect(Object.keys(ROLE_RULES).sort()).toEqual([...FIVE].sort());
    expect(landingFor("banana")).toBe("/runs");
    expect(labels("banana")).toEqual(labels("viewer"));
  });
  it("the approver menu is exactly Approvals, Runs, Ledger (BUILD and REPORTS not drawn)", () => {
    expect(labels("approver").sort()).toEqual(["Approvals", "Ledger", "Runs"]);
    expect(navGroupsFor("approver").map((g) => g.label)).toEqual(["OPERATE", "RECORD"]);
  });
  it("the viewer menu has no Builder and no Templates; the others see everything", () => {
    expect(labels("viewer")).toEqual(ALL.filter((l) => l !== "Org builder" && l !== "Templates"));
    for (const role of ["owner", "admin", "builder", null]) expect(labels(role)).toEqual(ALL);
  });
});

describe("AE-2 the sidebar", () => {
  it("shows the approver menu and a logo to the landing page", async () => {
    renderAs("approver", "/approvals/a1");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(nav.textContent).toContain("Approvals");
    expect(nav.textContent).not.toContain("Org builder");
    const logos = screen.getAllByRole("link").filter((l) => l.getAttribute("href") === "/approvals");
    expect(logos.length).toBeGreaterThan(0);
  });
});

describe("AE-3 the menu is not access control", () => {
  it("an approver can still open /builder by URL", async () => {
    renderAs("approver", "/builder");
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
  });
  it("write enabling never reads a role name or the menu", () => {
    const src = readFileSync("src/org-safety/use-org-safety.ts", "utf8");
    expect(src).not.toMatch(/app\/nav|auth\/roles|navGroupsFor|landingFor|\.role\b/);
  });
});
