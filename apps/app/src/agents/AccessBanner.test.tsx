import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import type { AgentCredential } from "@/api/types";
import { renderSafety } from "@/org-safety/test-utils";

import { AgentsPage } from "./agents-page";
import { credential } from "./test-data";

const ERASED = [
  credential({ id: "agc_" + "1".repeat(24), agent: "developer", scopes: ["read", "run"], issuerErased: true }),
  credential({ id: "agc_" + "2".repeat(24), agent: "tester", issuerErased: true }),
];

function page(role: "owner" | "approver" | "viewer", credentials: AgentCredential[] = ERASED) {
  const client = createFixtureClient({ agentAccess: { role, credentials } });
  const spy = { calls: 0 };
  const list = client.agentCredentials.list.bind(client.agentCredentials);
  client.agentCredentials.list = (filter) => {
    spy.calls += 1;
    return list(filter);
  };
  renderSafety(<AgentsPage />, client);
  return { client, spy };
}

const TEXT = "2 agent credentials were issued by a person who has been erased. Re-own them (rotate) or revoke them.";

describe("credentials without an issuer (P16, P17)", () => {
  it("shows the owner the banner with the live count, and the rows are marked", async () => {
    page("owner");
    expect((await screen.findAllByText(TEXT)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("issuer erased")).length).toBeGreaterThanOrEqual(2);
  });

  it("shows an approver and a viewer no banner and the refusal text only", async () => {
    for (const role of ["approver", "viewer"] as const) {
      const { unmount } = (() => {
        page(role);
        return { unmount: () => undefined };
      })();
      await waitFor(() => { expect(screen.getAllByText("Only an owner can manage agent access.").length).toBeGreaterThan(0); });
      expect(screen.queryByText(TEXT)).not.toBeInTheDocument();
      unmount();
      document.body.innerHTML = "";
    }
  });

  it("is derived from the data: rotating one and revoking the other removes it", async () => {
    const user = userEvent.setup();
    page("owner");
    await screen.findAllByText(TEXT);
    const dev = await screen.findByRole("group", { name: "Access for developer" });
    await user.click(within(dev).getByRole("button", { name: /^Rotate agc_/ }));
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    await user.click(await screen.findByRole("button", { name: "Done" }));
    expect((await screen.findAllByText(/^1 agent credential was issued by a person who has been erased/)).length).toBeGreaterThan(0);
    const tester = screen.getByRole("group", { name: "Access for tester" });
    await user.click(within(tester).getByRole("button", { name: /^Revoke agc_/ }));
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => { expect(screen.queryByText(/issued by a person who has been erased/)).not.toBeInTheDocument(); });
  });
});
