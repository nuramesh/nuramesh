import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { renderSafety } from "@/org-safety/test-utils";

import { credential } from "./test-data";
import { AgentsPage } from "./agents-page";

const LIVE = credential({ id: "agc_" + "a".repeat(24), agent: "dana" });
const GONE = credential({ id: "agc_" + "b".repeat(24), agent: "dana", state: "revoked", revokedAt: "2026-10-27T00:00:00Z" });
const OLD = credential({ id: "agc_" + "c".repeat(24), agent: "dana", state: "expired", expiresAt: "2026-01-01T00:00:00Z" });

function dump(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, i) => `${storage.key(i) ?? ""}=${storage.getItem(storage.key(i) ?? "") ?? ""}`).join("\n");
}

async function panel(options: Parameters<typeof createFixtureClient>[0] = {}) {
  const client = createFixtureClient({ agentAccess: { credentials: [LIVE, GONE, OLD] }, ...options });
  renderSafety(<AgentsPage />, client);
  return screen.findByRole("group", { name: "Access for dana" });
}

describe("Access panel (the agent identity notes section 10)", () => {
  it("lists id, scopes, state and dates, ended ones greyed", async () => {
    const p = await panel();
    expect(await within(p).findAllByRole("listitem")).toHaveLength(3);
    expect(within(p).getByText("active")).toBeInTheDocument();
    expect(within(p).getByText("revoked")).toBeInTheDocument();
    expect(within(p).getByText("expired")).toBeInTheDocument();
    expect(p.querySelector('[data-state="revoked"]')).toHaveClass("opacity-60");
  });

  it("shows the secret only in the dialog, once, and never in storage or the address", async () => {
    const user = userEvent.setup();
    const p = await panel();
    await user.click(await within(p).findByRole("button", { name: "Issue" }));
    await user.click(await screen.findByRole("button", { name: "Issue" }));
    const secret = (await screen.findByTestId("secret")).textContent;
    expect(secret).toMatch(/^nma_/);
    expect(screen.getByText(/Shown once/)).toBeInTheDocument();
    expect(dump(localStorage)).not.toContain(secret);
    expect(dump(sessionStorage)).not.toContain(secret);
    expect(globalThis.location.href).not.toContain(secret);
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(secret); // gone from the page once closed
  });

  it("names the agent and the last 6 characters when revoking, and Revoke all works", async () => {
    const user = userEvent.setup();
    const p = await panel();
    await user.click(await within(p).findByRole("button", { name: `Revoke ${LIVE.id}` }));
    expect(await screen.findByRole("heading", { name: `Revoke aaaaaa of dana?` })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(within(p).getByRole("button", { name: "Revoke all for this agent" }));
    await user.click(await screen.findByRole("button", { name: "Revoke all" }));
    expect(await within(p).findAllByText("revoked")).toHaveLength(2);
  });

  it("disables Issue and Rotate while halted, keeps Revoke, and says why", async () => {
    const p = await panel({ orgSafety: "halted" });
    expect(await within(p).findByText(/^Halted: agent credentials cannot run/)).toBeInTheDocument();
    expect(within(p).getByRole("button", { name: "Issue" })).toBeDisabled();
    expect(within(p).getByRole("button", { name: `Rotate ${LIVE.id}` })).toBeDisabled();
    expect(within(p).getByRole("button", { name: `Revoke ${LIVE.id}` })).toBeEnabled();
    expect(within(p).getByRole("button", { name: "Revoke all for this agent" })).toBeEnabled();
  });

  it("a non-owner sees the refusal sentence and no controls", async () => {
    const client = createFixtureClient({ agentAccess: { role: "viewer", credentials: [LIVE] } });
    renderSafety(<AgentsPage />, client);
    expect((await screen.findAllByText("Only an owner can manage agent access.")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Issue" })).not.toBeInTheDocument();
  });
});

describe("Access panel look (narrow widths, dates)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("never wraps the Rotate and Revoke labels and stacks them on a narrow card", async () => {
    const p = await panel();
    const rotate = await within(p).findByRole("button", { name: `Rotate ${LIVE.id}` });
    const revoke = within(p).getByRole("button", { name: `Revoke ${LIVE.id}` });
    expect(rotate).toHaveClass("whitespace-nowrap");
    expect(revoke).toHaveClass("whitespace-nowrap");
    expect(rotate.parentElement).toHaveClass("flex-col", "sm:flex-row");
  });

  it("shows each date as local time plus relative time, with the raw ISO kept in the element", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-26T12:03:00Z") });
    const p = await panel();
    const issued = (await within(p).findAllByText(/3 mins? ago|3 min\. ago/))[0];
    if (issued === undefined) throw new Error("no relative time shown");
    expect(issued.tagName).toBe("TIME");
    expect(issued).toHaveAttribute("datetime", LIVE.createdAt);
    expect(issued).toHaveAttribute("title", LIVE.createdAt);
    expect(issued.textContent).toMatch(/2026/);
    expect(p.textContent).not.toContain(LIVE.createdAt);
  });
});
