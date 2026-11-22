import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { GrantInput, ProposalItem } from "@/api/types";
import { renderSafety } from "@/org-safety/test-utils";

import { GrantDialog } from "./grant-dialog";

const item = (patch: Partial<ProposalItem> = {}): ProposalItem => ({
  subject: { kind: "role", id: "writer" },
  ruleKey: 3,
  role: "writer",
  kind: "write_file",
  status: "proposable",
  reasons: [],
  approvedAsks: 5,
  solo: { approvedAsksSolo: 3, soloShare: "0.6000" },
  evidenceHash: "obviously-fake-evidence-hash-not-a-secret",
  ...patch,
});

const context = { asOf: "2026-11-12T10:00:00Z", ledgerPosition: 41 };

function open(proposal: ProposalItem, grant = vi.fn<(input: GrantInput) => Promise<void>>(() => Promise.resolve())) {
  const api = createFixtureClient();
  api.autonomy.grant = grant;
  const onOpenChange = vi.fn();
  renderSafety(<GrantDialog open onOpenChange={onOpenChange} item={proposal} {...context} />, api);
  return { grant, onOpenChange };
}

describe("GrantDialog", () => {
  it("prints the solo line and the no-second-person sentence before the person grants", async () => {
    open(item());
    expect(await screen.findByText("approved asks: 5 (solo 3, 60 %)")).toBeInTheDocument();
    expect(screen.getByText("no second person reviewed the solo ones")).toBeInTheDocument();
  });

  it("prints only the line when no approved ask was solo", async () => {
    open(item({ solo: { approvedAsksSolo: 0, soloShare: "0.0000" } }));
    expect(await screen.findByText("approved asks: 5 (solo 0, 0 %)")).toBeInTheDocument();
    expect(screen.queryByText(/no second person/)).toBeNull();
  });

  it("with no approved ask there is no share to print", async () => {
    open(item({ approvedAsks: 0, solo: { approvedAsksSolo: 0, soloShare: null } }));
    expect(await screen.findByText("approved asks: 0 (solo 0)")).toBeInTheDocument();
  });

  it("needs a reason, then sends what the person read", async () => {
    const user = userEvent.setup();
    const { grant } = open(item());
    const button = await screen.findByRole("button", { name: "Grant autonomy" });
    expect(button).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: /Your reason/ }), "reviewed the evidence");
    await user.click(button);
    await waitFor(() => {
      expect(grant).toHaveBeenCalledTimes(1);
    });
    expect(grant.mock.calls.at(0)?.at(0)).toEqual({
      subject: { kind: "role", id: "writer" },
      ruleKey: 3,
      asOf: context.asOf,
      ledgerPosition: 41,
      evidenceHash: "obviously-fake-evidence-hash-not-a-secret",
      reason: "reviewed the evidence",
    });
  });

  it("shows a refusal inside the dialog and keeps it open", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = open(item(), vi.fn(() => Promise.reject(new ProblemError("the evidence changed", 409, "evidence_changed"))));
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok");
    await user.click(screen.getByRole("button", { name: "Grant autonomy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not grant: the evidence changed/);
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
