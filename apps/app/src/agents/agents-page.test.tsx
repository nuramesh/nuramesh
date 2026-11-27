import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { renderSafety } from "@/org-safety/test-utils";

import { AgentsPage } from "./agents-page";
import { credential } from "./test-data";

const LIVE = credential({ id: "agc_" + "a".repeat(24), agent: "developer" });

/** Counts every call the page makes to the agent credential API. */
function spied(role: "owner" | "approver" | "viewer") {
  const client = createFixtureClient({ agentAccess: { role, credentials: [LIVE] } });
  const calls: string[] = [];
  const api = client.agentCredentials;
  for (const name of ["list", "agents"] as const) {
    const original = api[name].bind(api) as (...a: unknown[]) => Promise<unknown>;
    (api as unknown as Record<string, unknown>)[name] = (...a: unknown[]) => {
      calls.push(name);
      return original(...a);
    };
  }
  return { client, calls };
}

describe("Agents page (the agent identity notes section 10)", () => {
  it("lists the organization's agents from the agent list, each with an Access panel", async () => {
    const { client, calls } = spied("owner");
    renderSafety(<AgentsPage />, client);
    expect(await screen.findByRole("group", { name: "Access for developer" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: /Developer/ }).length).toBeGreaterThan(0);
    expect(calls).toContain("agents");
  });

  it.each(["approver", "viewer"] as const)("an %s sees one refusal sentence and nothing is called", async (role) => {
    const { client, calls } = spied(role);
    renderSafety(<AgentsPage />, client);
    expect(await screen.findByText("Only an owner can manage agent access.")).toBeInTheDocument();
    await waitFor(() => { expect(screen.queryByText("Reading agents…")).not.toBeInTheDocument(); });
    expect(calls).toEqual([]);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(within(document.body).queryByRole("button", { name: "Issue" })).not.toBeInTheDocument();
  });
});
