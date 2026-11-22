import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { GateOutcome, RunTask } from "@/api/types";
import { usd } from "@/api/money";
import { renderSafety } from "@/org-safety/test-utils";

import { TaskGates } from "./run-details";

const task = (patch: Partial<RunTask>): RunTask => ({
  id: "t1",
  title: "Ship the report",
  assignee: "writer",
  status: "verifying",
  attempt: 1,
  maxAttempts: 3,
  note: "",
  gates: [],
  spend: usd("0"),
  calls: 0,
  submission: 1,
  criteria: [{ id: "c1", text: "Numbers match the source" }],
  humanGates: [{ id: "review", checks: ["c1"] }],
  ...patch,
});

const result = (patch: Partial<GateOutcome>): GateOutcome => ({
  gate: "review",
  kind: "human",
  required: true,
  outcome: "passed",
  checkedBy: "human:u_1",
  attempt: 1,
  submission: 1,
  ledgerEntry: "run:r1#9",
  verdicts: [],
  ...patch,
});

const show = (t: RunTask) =>
  renderSafety(
    <MemoryRouter>
      <TaskGates runId="r1" tasks={[t]} />
    </MemoryRouter>,
  );

describe("TaskGates: the human gate decision form (SA-19)", () => {
  it("offers the form on a task that waits in verifying with a human gate", async () => {
    show(task({}));
    expect(await screen.findByRole("form", { name: "Decide gate review of Ship the report" })).toBeInTheDocument();
  });

  const closed: [string, Partial<RunTask>][] = [
    ["a finished task", { status: "done" }],
    ["a task with no human gate", { humanGates: [] }],
    ["a server that did not name the round", { submission: undefined }],
    ["a gate whose checks the server did not name", { humanGates: [{ id: "review", checks: [] }] }],
    ["a gate that already has a result in this round", { gates: [result({})] }],
  ];
  it.each(closed)("offers nothing for %s", (_, patch) => {
    show(task(patch));
    expect(screen.queryByRole("form")).toBeNull();
  });

  const earlier: [string, Partial<RunTask>][] = [
    ["an earlier attempt", { attempt: 2, gates: [result({ outcome: "failed" })] }],
    ["an earlier submission of this attempt", { submission: 2, gates: [result({ outcome: "failed" })] }],
    ["another gate in this round", { gates: [result({ gate: "verify", kind: "verifier" })] }],
  ];
  it.each(earlier)("still offers the form when the only result is of %s", async (_, patch) => {
    show(task(patch));
    expect(await screen.findByRole("form", { name: "Decide gate review of Ship the report" })).toBeInTheDocument();
  });

  it("offers one form per human gate that is still open", async () => {
    show(task({ humanGates: [{ id: "review", checks: ["c1"] }, { id: "legal", checks: ["c1"] }], gates: [result({})] }));
    expect(await screen.findByRole("form", { name: "Decide gate legal of Ship the report" })).toBeInTheDocument();
    expect(screen.getAllByRole("form")).toHaveLength(1);
  });
});
