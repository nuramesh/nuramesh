import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { runTask } from "@/api/server/map";
import type { RawRunDetail } from "@/api/server/raw";
import { renderSafety } from "@/org-safety/test-utils";
import { body } from "@/test/contract";
import { recorded, recordedServer, type Sent } from "@/test/recorded-server";

import { TaskGates } from "./run-details";

/**
 * The gate form against the server's recorded answers (`contract/solo.json`): the engine's
 * reference task has three criteria and its human gate `deploy` checks only `approved`. The fake
 * server answers a decision as the real one did: 422 for any other set of findings, 403 without
 * the sole decider's acknowledgement, 201 for the gate's own checks.
 */
const DECISIONS = "POST /v1/runs/r1/tasks/t1/gates/deploy/decisions";

function decision(sent: Sent) {
  const sentBody = sent.body as { findings: Record<string, unknown>; acknowledge_solo?: boolean };
  if (Object.keys(sentBody.findings).join(",") !== "approved") return recorded("solo", "decide_every_criterion");
  return sentBody.acknowledge_solo === true ? recorded("solo", "decide_ok") : recorded("solo", "decide_no_acknowledgement");
}

function show() {
  const server = recordedServer({ "GET /v1/org/status": () => recorded("solo", "org_status"), [DECISIONS]: decision });
  const raw = body<RawRunDetail>("solo", "run_waiting");
  renderSafety(
    <MemoryRouter>
      <TaskGates runId="r1" tasks={raw.tasks.map((t) => runTask(t, "run:r1"))} />
    </MemoryRouter>,
    server.api,
  );
  return server;
}

describe("the gate form against the real server's answers (SA-19)", () => {
  it("the recorded server refuses one finding per task criterion and takes the gate's checks", () => {
    expect(recorded("solo", "decide_every_criterion")).toMatchObject({ status: 422, body: { title: "invalid_decision", detail: "a decision must cover exactly the criteria ['approved']" } });
    expect(recorded("solo", "decide_ok")).toMatchObject({ status: 201, body: { outcome: "passed", gate: "deploy" } });
    expect(recorded("solo", "decide_no_acknowledgement")).toMatchObject({ status: 403, body: { title: "acknowledge_required" } });
    expect(recorded("solo", "decide_other_answer")).toMatchObject({ status: 409, body: { title: "already_decided" } });
  });

  it("a sole decider passes the reference task's gate: findings for the gate's checks only", async () => {
    const user = userEvent.setup();
    const { calls } = show();
    await screen.findByRole("form", { name: "Decide gate deploy of Task t1" });
    for (const approve of screen.getAllByRole("radio", { name: "Approve" })) await user.click(approve);
    for (const box of screen.getAllByRole("textbox")) await user.type(box, "read the diff");
    await user.click(await screen.findByRole("checkbox", { name: /Nobody else will review this/ }));
    await user.click(screen.getByRole("button", { name: "Submit decision" }));
    expect(await screen.findByText(/Gate passed|did not accept/)).toHaveTextContent("Gate passed. Your decision is in the ledger.");
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toEqual({ attempt: 1, submission: 1, findings: { approved: { approved: true, reason: "read the diff" } }, acknowledge_solo: true });
  });

  it("asks only for the criteria the gate checks, with their texts", async () => {
    show();
    await screen.findByRole("form", { name: "Decide gate deploy of Task t1" });
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.getByRole("group", { name: "A human approves the deploy" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "The function returns the sum" })).toBeNull();
  });

  it("the acknowledgement depends on the gate's findings only: rejecting needs none", async () => {
    const user = userEvent.setup();
    show();
    await screen.findByRole("form", { name: "Decide gate deploy of Task t1" });
    await user.click(screen.getByRole("radio", { name: "Reject" }));
    await user.type(screen.getByRole("textbox"), "not yet");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Submit decision" })).toBeEnabled();
    });
    expect(screen.queryByRole("checkbox", { name: /Nobody else will review this/ })).toBeNull();
  });
});
