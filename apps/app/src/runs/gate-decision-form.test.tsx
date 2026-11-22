import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { type ApiClient, InvalidRequestError, ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { RunTask } from "@/api/types";
import { usd } from "@/api/money";
import { renderSafety } from "@/org-safety/test-utils";

import { GateDecisionForm } from "./gate-decision-form";

const task: RunTask = {
  id: "t1",
  title: "Ship the report",
  assignee: "writer",
  status: "verifying",
  attempt: 2,
  maxAttempts: 3,
  note: "",
  gates: [],
  spend: usd("0"),
  calls: 0,
  submission: 4,
  criteria: [
    { id: "c1", text: "Numbers match the source" },
    { id: "c2", text: "No private data" },
  ],
  humanGates: [{ id: "review", checks: ["c1", "c2"] }],
};
const review = { id: "review", checks: ["c1", "c2"] };

function nth<T>(items: T[], i: number): T {
  const found = items[i];
  if (found === undefined) throw new Error(`no item ${String(i)}`);
  return found;
}

async function clientWith(soloNow: boolean | undefined, decide = vi.fn<ApiClient["gates"]["decide"]>(() => Promise.resolve({ outcome: "passed", alreadyRecorded: false }))) {
  const api = createFixtureClient();
  const status = await api.org.status();
  api.org.status = () => Promise.resolve({ ...status, ...(soloNow === undefined ? {} : { soloNow }) });
  api.gates.decide = decide;
  return { api, decide };
}

/** Answers each criterion in order: `[verdict, reason]`. Nothing is chosen for the person. */
async function answer(user: ReturnType<typeof userEvent.setup>, answers: ["approve" | "reject", string][]) {
  const boxes = await screen.findAllByRole("textbox");
  for (const [i, [verdict, text]] of answers.entries()) {
    await user.click(nth(screen.getAllByRole("radio", { name: verdict === "approve" ? "Approve" : "Reject" }), i));
    await user.type(nth(boxes, i), text);
  }
}

const PASS: ["approve", string][] = [["approve", "matches"], ["approve", "clean"]];

describe("GateDecisionForm", () => {
  it("asks one finding per criterion the gate checks and keeps Submit off until each has a reason", async () => {
    const user = userEvent.setup();
    const { api } = await clientWith(false);
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    expect(await screen.findByText("Numbers match the source")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Submit decision" });
    expect(submit).toBeDisabled();
    await answer(user, [["approve", "checked against the sheet"]]);
    expect(submit).toBeDisabled();
    await user.click(nth(screen.getAllByRole("radio", { name: "Approve" }), 1));
    expect(submit).toBeDisabled();
    await user.type(nth(screen.getAllByRole("textbox"), 1), "   ");
    expect(submit).toBeDisabled();
    await user.type(nth(screen.getAllByRole("textbox"), 1), "clean");
    expect(submit).toBeEnabled();
  });

  it("sends the round and findings, and no acknowledgement or digest, when the person is not alone", async () => {
    const user = userEvent.setup();
    const { api, decide } = await clientWith(false);
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    await waitFor(() => {
      expect(decide).toHaveBeenCalledTimes(1);
    });
    const [run, taskId, gate, input] = nth(decide.mock.calls, 0);
    expect([run, taskId, gate]).toEqual(["r1", "t1", "review"]);
    expect(input).toEqual({ attempt: 2, submission: 4, findings: { c1: { approved: true, reason: "matches" }, c2: { approved: true, reason: "clean" } } });
    expect("acknowledgeSolo" in input).toBe(false);
    expect(await screen.findByText(/Gate passed/)).toBeInTheDocument();
  });

  it("a sole decider who passes the gate must tick the acknowledgement, and it is sent as true", async () => {
    const user = userEvent.setup();
    const { api, decide } = await clientWith(true);
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    const ack = await screen.findByRole("checkbox", { name: /Nobody else will review this/ });
    const submit = screen.getByRole("button", { name: "Submit decision" });
    expect(submit).toBeDisabled();
    await user.click(ack);
    expect(submit).toBeEnabled();
    await user.click(submit);
    await waitFor(() => {
      expect(decide).toHaveBeenCalledTimes(1);
    });
    const input = nth(decide.mock.calls, 0)[3];
    expect(input.acknowledgeSolo).toBe(true);
    expect(JSON.stringify(input)).not.toMatch(/digest/i);
  });

  it("a sole decider who rejects a finding needs no acknowledgement (a failing decision is free)", async () => {
    const user = userEvent.setup();
    const { api, decide } = await clientWith(true, vi.fn(() => Promise.resolve({ outcome: "failed", alreadyRecorded: false })));
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, [["approve", "matches"], ["reject", "contains an address"]]);
    expect(screen.queryByRole("checkbox", { name: /Nobody else will review this/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    await waitFor(() => {
      expect(decide).toHaveBeenCalledTimes(1);
    });
    const input = nth(decide.mock.calls, 0)[3];
    expect(input.findings.c2).toEqual({ approved: false, reason: "contains an address" });
    expect("acknowledgeSolo" in input).toBe(false);
    expect(await screen.findByText(/Gate failed/)).toBeInTheDocument();
  });

  it("acknowledge_required from a server that said we were not alone: says why and shows the box", async () => {
    const user = userEvent.setup();
    const { api } = await clientWith(false, vi.fn(() => Promise.reject(new ProblemError("you are the only person who can decide this", 403, "acknowledge_required"))));
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/only person who can decide.*tick/i);
    expect(screen.getByRole("checkbox", { name: /Nobody else will review this/ })).toBeInTheDocument();
  });

  it("a ticked acknowledgement does not survive a switch to Reject and back", async () => {
    const user = userEvent.setup();
    const { api } = await clientWith(true);
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    await user.click(screen.getByRole("checkbox", { name: /Nobody else will review this/ }));
    await user.click(nth(screen.getAllByRole("radio", { name: "Reject" }), 1));
    expect(screen.queryByRole("checkbox", { name: /Nobody else will review this/ })).not.toBeInTheDocument();
    await user.click(nth(screen.getAllByRole("radio", { name: "Approve" }), 1));
    expect(screen.getByRole("checkbox", { name: /Nobody else will review this/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Submit decision" })).toBeDisabled();
  });

  it("explains a 422 and a changed round in words", async () => {
    const user = userEvent.setup();
    const bad = await clientWith(false, vi.fn(() => Promise.reject(new InvalidRequestError("findings: missing c2"))));
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, bad.api);
    await answer(user, PASS);
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/The server did not accept this decision: findings: missing c2/);
  });

  it("a task with more criteria than the gate checks: only the gate's are asked and sent", async () => {
    const user = userEvent.setup();
    const { api, decide } = await clientWith(true);
    const wide: RunTask = { ...task, criteria: [{ id: "c0", text: "The tests pass" }, ...(task.criteria ?? []), { id: "c3", text: "The style is right" }] };
    renderSafety(<GateDecisionForm runId="r1" task={wide} gate={{ id: "review", checks: ["c2"] }} />, api);
    expect(await screen.findByRole("group", { name: "No private data" })).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByText("The tests pass")).toBeNull();
    await answer(user, [["approve", "clean"]]);
    // the acknowledgement follows the gate's own findings: the task's other criteria have no say
    await user.click(await screen.findByRole("checkbox", { name: /Nobody else will review this/ }));
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    await waitFor(() => {
      expect(decide).toHaveBeenCalledTimes(1);
    });
    expect(nth(decide.mock.calls, 0)[3]).toEqual({ attempt: 2, submission: 4, findings: { c2: { approved: true, reason: "clean" } }, acknowledgeSolo: true });
  });

  it("a check the task's criteria do not name is still asked, by its id", async () => {
    const { api } = await clientWith(false);
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={{ id: "review", checks: ["c2", "legal_ok"] }} />, api);
    expect(await screen.findByRole("group", { name: "legal_ok" })).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(2);
  });

  it("a gate that was decided in another window says so (409 already_decided)", async () => {
    const user = userEvent.setup();
    const { api } = await clientWith(false, vi.fn(() => Promise.reject(new ProblemError("the gate already has a decision in this round", 409, "already_decided"))));
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already has a decision in this round.*Reload/);
  });

  it("a round that moved on is told to reload, not retried", async () => {
    const user = userEvent.setup();
    const { api } = await clientWith(false, vi.fn(() => Promise.reject(new ProblemError("not the current round", 409, "stale_round"))));
    renderSafety(<GateDecisionForm runId="r1" task={task} gate={review} />, api);
    await answer(user, PASS);
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed since you opened it.*reload/i);
  });
});
