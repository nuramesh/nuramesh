import { cleanup, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { approval, ledgerEntry } from "@/api/server/map";
import type { RawApproval, RawEvent } from "@/api/server/raw";
import { rawNeedsHuman } from "@/test/contract";
import { renderAt, runLike, serverLike } from "@/test/server-like";
import { createFixtureClient } from "@/api/fixture-client";

const RUN = "run-20261024-100000-abcd";
const WITHHELD = "withheld (a person's text is not shown)";
const HASH = `${"ab".repeat(6)}${"c".repeat(52)}`;

const entry = (seq: number, actor: string, payload: Record<string, unknown>, extra: Partial<RawEvent> = {}): RawEvent =>
  ({ stream: `run:${RUN}`, seq, type: seq === 7 ? "decision.recorded" : "task.transitioned", actor, at: "2026-10-24T10:00:00Z", run_id: RUN, task_id: "t5", payload, cost: null, cost_basis: null, hash: "h".repeat(8), prev_hash: "p".repeat(8), ...extra });

describe("U1 run page", () => {
  it("shows no note paragraph for a task whose note the server withheld (null), from the unchanged needs-human body", async () => {
    const body = rawNeedsHuman as unknown as { tasks: { id: string; note: string | null }[] };
    const tasks = body.tasks.map((t) => ({ ...t, note: null }));
    const client = serverLike({ runs: { get: () => Promise.resolve(runLike({ tasks } as never)) } });
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    for (const t of tasks) {
      const item = await screen.findByTestId(`task-${t.id}`);
      expect(item.querySelector("p.text-muted-foreground")).toBeNull();
    }
  });
});

describe("U2 ledger page", () => {
  it("shows withheld for a person's reason and note_present for an incident", async () => {
    const six = ledgerEntry(entry(6, "human:u1", { reason: null }, { redacted: ["reason"] }));
    const seven = ledgerEntry(entry(7, "system", { kind: "engine_incident", note_present: true }, { redacted: ["note"] }));
    const client = (shown: typeof six) => serverLike({ ledger: { list: () => Promise.resolve([shown]), get: () => Promise.resolve(shown) } });
    renderAt(`/ledger/${encodeURIComponent(six.n)}`, client(six));
    await screen.findByText("Chain", { selector: "h3" });
    expect(screen.getByText(WITHHELD)).toBeInTheDocument();
    cleanup();
    renderAt(`/ledger/${encodeURIComponent(seven.n)}`, client(seven));
    await screen.findByText("Chain", { selector: "h3" });
    expect(screen.getByText("note_present")).toBeInTheDocument();
    expect(screen.getByText("yes")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("NOTE-TEXT-FIXTURE");
  });
});

describe("U2 budget stop sentence", () => {
  it("prints no reason after 'Budget stop' for a person's event, even from a stale server", () => {
    const stale = ledgerEntry(entry(8, "human:u1", { reason: "STALE-WORDS" }, { type: "budget.exceeded" }));
    const nulled = ledgerEntry(entry(9, "human:u1", { reason: null }, { type: "budget.exceeded", redacted: ["reason"] }));
    const system = ledgerEntry(entry(10, "system", { reason: "limit passed" }, { type: "budget.exceeded" }));
    expect(JSON.stringify(stale)).not.toContain("STALE-WORDS");
    expect(JSON.stringify(nulled)).not.toMatch(/Budget stop: /);
    expect(JSON.stringify(system)).toContain("Budget stop: limit passed");
  });
});

const raw = (patch: Partial<RawApproval>): RawApproval => ({
  id: "a-old", run_id: RUN, task_id: null, agent_id: "dana", action: { kind: "run_command", target: "pytest", amount: null },
  asked_because: "rule", status: "approved", requested_at: "2026-10-24T10:00:00Z", decided_by: "human:u1", decided_at: "2026-10-24T10:05:00Z",
  decision_reason_hash: null, decision_reason: null, ledger: [{ ledger_seq: 9, type: "approval.decided", at: "x", actor: "human:u1" }], ...patch,
});

async function showApproval(r: RawApproval): Promise<void> {
  const a = approval(r, null);
  renderAt(`/approvals/${r.id}`, serverLike({ approvals: { list: () => Promise.resolve([a]), get: () => Promise.resolve(a) } }));
  await screen.findByText(/Approved by/);
}

describe("U3 approvals page", () => {
  it("says a person's reason is not shown and names the hash only when there is one", async () => {
    await showApproval(raw({}));
    expect(screen.getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(screen.queryByText(/kept in the content store/)).toBeNull();
    expect(document.body.textContent).not.toContain("Reason: “");
  });

  it("with a hash adds the first 12 characters", async () => {
    await showApproval(raw({ id: "a-h", decision_reason_hash: HASH }));
    expect(screen.getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(screen.getByText(`kept in the content store (hash ${HASH.slice(0, 12)})`)).toBeInTheDocument();
  });

  it("never shows a stale server's decision_reason", async () => {
    await showApproval(raw({ id: "a-stale", decision_reason: "STALE-WORDS" }));
    expect(screen.getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("STALE-WORDS");
  });
});

describe("U4 fixture client", () => {
  it("follows the same rule: a decision has a hash, a seeded old one has none, neither holds the words", async () => {
    const api = createFixtureClient();
    const pending = (await api.approvals.list()).find((a) => a.state === "pending");
    const decided = await api.approvals.decide(pending?.id ?? "", { decision: "approve", reason: "Looks right" });
    expect(decided.result.decision).toMatchObject({ by: "You" });
    expect(decided.result.decision?.reasonHash).toMatch(/^[0-9a-f]{64}$/);
    expect(JSON.stringify(decided.result.decision)).not.toContain("Looks right");
    const old = await api.approvals.get("a4");
    expect(old.decision?.reasonHash).toBeNull();
    expect(JSON.stringify(old.decision)).not.toContain("Route through support");
  });
});
