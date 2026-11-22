import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { type ApiClient, NotAvailableError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { ProposalItem, ProposalList } from "@/api/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

const item = (patch: Partial<ProposalItem> = {}): ProposalItem => ({
  subject: { kind: "role", id: "developer" },
  ruleKey: 2,
  role: "developer",
  kind: "write_file",
  status: "proposable",
  reasons: [],
  approvedAsks: 5,
  solo: { approvedAsksSolo: 2, soloShare: "0.4000" },
  evidenceHash: "obviously-fake-evidence-hash-not-a-secret",
  ...patch,
});

const list = (items: ProposalItem[]): ProposalList => ({ items, ledgerPosition: 7, asOf: "2026-11-12T10:00:00Z" });

describe("Autonomy page", () => {
  it("is in the sidebar and shows each proposal with its solo line and evidence", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item()])));
    renderAt("/autonomy", api);
    expect(await screen.findByRole("heading", { level: 1, name: "Autonomy" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Autonomy/ })).toHaveAttribute("href", "/autonomy");
    expect(await screen.findByText("approved asks: 5 (solo 2, 40 %)")).toBeInTheDocument();
    expect(screen.getByText("no second person reviewed the solo ones")).toBeInTheDocument();
    expect(screen.getByText("role developer, write file")).toBeInTheDocument();
    expect(screen.getByText("can be granted")).toBeInTheDocument();
  });

  it("a word the server never sends (`eligible`) is not a grantable status", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item({ status: "eligible" })])));
    renderAt("/autonomy", api);
    expect(await screen.findByText("eligible")).toBeInTheDocument(); // shown as sent, never acted on
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("a blocked cell lists the server's reasons in words and offers no Grant", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item({ status: "blocked", reasons: ["denied_in_window", "some_new_code"] })])));
    renderAt("/autonomy", api);
    expect(await screen.findByText("blocked")).toBeInTheDocument();
    expect(screen.getByText("A person denied an ask in the window.")).toBeInTheDocument();
    expect(screen.getByText("some_new_code")).toBeInTheDocument(); // an unknown code is shown as sent
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("a cell that counts nothing prints no approved-asks line (as the CLI)", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item({ kind: "run_command", status: "never_grantable", reasons: ["never_grantable"], approvedAsks: null, solo: { approvedAsksSolo: 0, soloShare: null } })])));
    renderAt("/autonomy", api);
    expect(await screen.findByText("never grantable")).toBeInTheDocument();
    expect(screen.getByText("role developer, run command")).toBeInTheDocument();
    expect(screen.queryByText(/approved asks/)).toBeNull();
  });

  it("with no approved ask in a counted cell the line has no share: approved asks: 0 (solo 0)", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item({ status: "not_enough_evidence", reasons: ["approved_below_min"], approvedAsks: 0, solo: { approvedAsksSolo: 0, soloShare: null } })])));
    renderAt("/autonomy", api);
    expect(await screen.findByText("approved asks: 0 (solo 0)")).toBeInTheDocument();
  });

  it("an approver reads the proposals but is not offered Grant (only an owner can grant)", async () => {
    const api = createFixtureClient({ agentAccess: { role: "approver" } });
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item()])));
    renderAt("/autonomy", api);
    expect(await screen.findByText("can be granted")).toBeInTheDocument();
    expect(screen.getByText("Only an owner can grant autonomy.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("reads the roles from the organization status, never from the organization editor's data", async () => {
    const api = createFixtureClient();
    const orgsGet = vi.fn(() => Promise.reject(new NotAvailableError("orgs.get"))); // what the server client answers
    api.orgs.get = orgsGet;
    const proposals = vi.fn((role: string) => Promise.resolve(list([item({ subject: { kind: "role", id: role }, role })])));
    api.autonomy.proposals = proposals;
    const status = await api.org.status();
    renderAt("/autonomy", api);
    expect(await screen.findByText(/^approved asks: 5/)).toBeInTheDocument();
    expect(proposals.mock.calls[0]?.[0]).toBe(status.roles.find((r) => r.inConfig)?.role);
    expect(orgsGet).not.toHaveBeenCalled();
  });

  it("opens the grant dialog and grants what was read", async () => {
    const user = userEvent.setup();
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([item()])));
    const grant = vi.fn(() => Promise.resolve());
    api.autonomy.grant = grant;
    renderAt("/autonomy", api);
    await user.click(await screen.findByRole("button", { name: /Grant autonomy/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Your reason/ }), "reviewed");
    await user.click(within(dialog).getByRole("button", { name: "Grant autonomy" }));
    await waitFor(() => {
      expect(grant).toHaveBeenCalledTimes(1);
    });
  });

  it("says plainly when there is no proposal", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.resolve(list([])));
    renderAt("/autonomy", api);
    expect(await screen.findByText(/No agent has earned autonomy yet/)).toBeInTheDocument();
  });

  it("shows a loading state, then an error with a retry", async () => {
    const api = createFixtureClient();
    api.autonomy.proposals = vi.fn(() => Promise.reject(new NotAvailableError("proposals")));
    renderAt("/autonomy", api);
    expect(await screen.findByRole("alert")).toHaveTextContent(/can't list autonomy proposals/i);
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("works on sample data: a fixture proposal with a solo line", async () => {
    renderAt("/autonomy");
    expect(await screen.findByText(/^approved asks: \d+ \(solo \d+/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Grant autonomy/ })).toBeInTheDocument();
  });
});
