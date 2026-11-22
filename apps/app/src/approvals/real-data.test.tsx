import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { approval } from "@/api/server/map";
import type { RawApproval } from "@/api/server/raw";
import { renderAt, serverLike } from "@/test/server-like";

const raw: RawApproval = {
  id: "ap-1",
  run_id: "run-20261024-100000-abcd",
  task_id: "run-20261024-100000-abcd-t1",
  agent_id: "dana",
  action: { kind: "run_command", target: "pytest -q", amount: null },
  asked_because: "rule 3 says ask",
  status: "pending",
  requested_at: "2026-10-24T10:00:00Z",
  decided_by: null,
  decided_at: null,
  decision_reason_hash: null,
  decision_reason: null,
  ledger: [{ ledger_seq: 4, type: "approval.requested", at: "2026-10-24T10:00:00Z", actor: "system" }],
};

describe("Approvals on real data", () => {
  it("shows a real request, and a refused decision (no content store) says why and changes nothing", async () => {
    const user = userEvent.setup();
    const decide = vi.fn(() => Promise.reject(new ProblemError("x", 503, "content_store_unavailable")));
    const client = serverLike({
      approvals: { list: () => Promise.resolve([approval(raw, null)]), get: () => Promise.resolve(approval(raw, null)), decide },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });
    renderAt("/approvals", client);
    expect(await screen.findByRole("heading", { level: 2, name: "dana wants to run a command: pytest -q." })).toBeInTheDocument();
    expect(screen.getByText("not available")).toBeInTheDocument(); // the run's spend isn't known here: said, not invented
    await user.type(screen.getByLabelText(/Your reason/), "reviewed the command");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(decide).toHaveBeenCalledWith("ap-1", { decision: "approve", reason: "reviewed the command" });
    const alert = await screen.findByText(/Couldn't save the decision: The server couldn't store the reason/);
    expect(alert).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("a decided request is read-only and links to the ledger entry that recorded it", async () => {
    const decided = approval(
      { ...raw, status: "approved", decided_by: "human:you", decided_at: "2026-10-24T10:05:00Z", decision_reason_hash: "c".repeat(64), ledger: [...(raw.ledger ?? []), { ledger_seq: 9, type: "approval.decided", at: "x", actor: "human:you" }] },
      { spent: { amount: "0.5", currency: "USD" }, limit: null },
    );
    const client = serverLike({
      approvals: { list: () => Promise.resolve([decided]), get: () => Promise.resolve(decided) },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });
    renderAt("/approvals/ap-1", client);
    const link = await screen.findByRole("link", { name: /recorded as ledger #9/ });
    expect(link).toHaveAttribute("href", "/ledger/run%3Arun-20261024-100000-abcd%409");
    expect(screen.getByText(/\$0\.50 \(no run budget recorded\)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByText(/kept in the content store \(hash c{12}\)/)).toBeInTheDocument();
  });
});
