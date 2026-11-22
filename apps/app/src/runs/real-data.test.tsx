import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NotFoundError, ProblemError } from "@/api/client";
import { runDetail, runSummary } from "@/api/server/map";
import type { RawRunDetail } from "@/api/server/raw";
import { body } from "@/test/contract";
import type { LiveStatus, RunDetail, RunEvent } from "@/api/types";
import { rawRunList } from "@/test/contract";
import { renderAt, runLike, serverLike } from "@/test/server-like";

const RUN = "run-20261023-193348-ef9e";
const summaries = (rawRunList as unknown as { runs: Parameters<typeof runSummary>[0][] }).runs.map(runSummary);

/** A server-like client around a mutable run, with a Stop that works like the real one (the run becomes "stopping"). */
function setup(initial: RunDetail = runLike({ status: "running", ended_at: null })) {
  let run = initial;
  const stop = vi.fn<(id: string, reason?: string) => Promise<{ result: RunDetail; ledgerEntry: string }>>(() => {
    run = { ...run, status: "stopping", stopRequestedBy: "human:you" };
    return Promise.resolve({ result: run, ledgerEntry: "33" });
  });
  let push: (e: RunEvent) => void = () => undefined;
  let report: (s: LiveStatus) => void = () => undefined;
  const subscribe = vi.fn((_id: string, onEvent: (e: RunEvent) => void, options?: { onStatus?: (s: LiveStatus) => void }) => {
    push = onEvent;
    report = options?.onStatus ?? (() => undefined);
    return () => undefined;
  });
  const events = vi.fn(() => Promise.resolve({ events: [] as RunEvent[], nextSeq: 0 }));
  const client = serverLike({
    runs: { get: () => Promise.resolve(run), stop, subscribe, events, listPage: () => Promise.resolve({ runs: summaries, truncated: false }), list: () => Promise.resolve(summaries) },
    ledger: { verifyRun: () => Promise.resolve({ verified: true, chainOk: true, anchorState: "anchored" as const, entries: 32, gaps: 0, anchor: null, checkedAt: "2026-10-24T12:00:00Z" }) },
    info: () => Promise.resolve({ name: "demo", detail: "this host · people can act" }),
  });
  return { client, stop, events, subscribe, push: (e: RunEvent) => { act(() => { push(e); }); }, report: (s: LiveStatus) => { act(() => { report(s); }); } };
}

describe("Stop on a real run", () => {
  it("asks first, says an in-flight call is charged, sends the reason and then shows Stopping… until the run ends", async () => {
    const user = userEvent.setup();
    const { client, stop } = setup();
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull(); // pausing doesn't exist on the server yet
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: /Stop run #ef9e\?/ });
    expect(dialog).toHaveTextContent(/already in flight/);
    expect(dialog).toHaveTextContent(/finishes and is charged/);
    expect(dialog).toHaveTextContent(/under your name/);
    await user.type(within(dialog).getByLabelText(/Reason/), "wrong branch");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledWith(RUN, "wrong branch");
    await waitFor(() => { expect(screen.queryByRole("dialog")).toBeNull(); });
    expect(await screen.findByTestId("stopping-banner")).toHaveTextContent(/requested by human:you/);
    expect(screen.getByTestId("stopping-banner")).toHaveTextContent(/already in flight finishes and is charged/);
    expect(screen.getByText("Stopping…", { selector: "span" })).toBeInTheDocument(); // the status pill
    const button = screen.getByRole("button", { name: "Stopping…" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/Stop requested for #ef9e by human:you\. Ledger entry #33\./)).toBeInTheDocument();
  });

  it("'Keep running' cannot close the dialog while the stop is being sent, and a failure shows in it", async () => {
    const user = userEvent.setup();
    const { client } = setup();
    let fail: (e: Error) => void = () => undefined;
    client.runs.stop = () => new Promise((_resolve, reject) => { fail = reject; });
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(within(dialog).getByRole("button", { name: "Keep running" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    act(() => { fail(new ProblemError("x", 409, "run_finished")); });
    expect(await within(screen.getByRole("dialog")).findByText(/already ended/)).toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Keep running" })).toBeEnabled();
  });

  it("keeping the run running sends nothing", async () => {
    const user = userEvent.setup();
    const { client, stop } = setup();
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Keep running" }));
    expect(stop).not.toHaveBeenCalled();
    expect(screen.getByText("Running", { selector: "span" })).toBeInTheDocument();
  });

  it.each([
    [409, "run_finished", /already ended/],
    [409, "wrong_organization", /server text/],
    [503, "content_store_unavailable", /content store isn't set up/],
    [403, "authentication_required", /--auth/],
    [0, "unreachable", /Couldn't reach the NuraMesh server/],
  ])("%i %s: says so in the dialog, keeps it open and keeps what was typed", async (status, title, words) => {
    const user = userEvent.setup();
    const { client } = setup();
    const message = title === "unreachable" ? "Couldn't reach the NuraMesh server. Is \"nuramesh serve\" running?" : "server text";
    client.runs.stop = () => Promise.reject(new ProblemError(message, status, title));
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "my reason");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(await within(dialog).findByText(words)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Reason/)).toHaveValue("my reason");
    expect(screen.queryByTestId("stopping-banner")).toBeNull();
  });

  it("a stop of a foreign or missing run (the real client's NotFoundError) shows the path text and keeps the dialog", async () => {
    const user = userEvent.setup();
    const { client } = setup();
    client.runs.stop = () => Promise.reject(new NotFoundError(`/v1/runs/${RUN}/stop`));
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "my reason");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));
    expect(await within(dialog).findByText(`/v1/runs/${RUN}/stop was not found`)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Reason/)).toHaveValue("my reason");
    expect(screen.queryByTestId("stopping-banner")).toBeNull();
  });

  it("a run that already ended offers no Stop (a run waiting for a person is over)", async () => {
    const { client } = setup(runLike());
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText("Waiting for you", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("a stopped run says it was stopped, by whom, and that the spend is final", async () => {
    const { client } = setup(runLike({ status: "stopped", stop: { requested_by: "human:you" } }));
    renderAt(`/runs/${RUN}`, client);
    expect(await screen.findByText(/This run was stopped by human:you\. The spend shown is final\./)).toBeInTheDocument();
  });
});

describe("the live view of a real run", () => {
  it("shows entries as they arrive over the socket and says so when the connection drops and returns", async () => {
    const { client, push, report, subscribe, events } = setup();
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => { expect(subscribe).toHaveBeenCalled(); });
    expect(subscribe.mock.calls[0]?.[0]).toBe(RUN);
    report("connecting");
    push({ seq: 40, at: "2026-10-24T12:00:01Z", kind: "gate.completed", category: "gates", text: "Gate tests failed on fix-add (attempt 2, checked by gate:test_command)", tone: "warm" });
    const log = await screen.findByRole("log");
    expect(within(log).getByText(/Gate tests failed on fix-add/)).toBeInTheDocument();
    report("live");
    expect(screen.getByTestId("timeline-mode")).toHaveTextContent("live");
    report("reconnecting");
    expect(screen.getByTestId("timeline-mode")).toHaveTextContent(/reconnecting/);
    report("live");
    push({ seq: 41, at: "2026-10-24T12:00:02Z", kind: "budget.settled", category: "spend", text: "Charged dana's call $0.0005", tone: "muted" });
    expect(await within(screen.getByRole("log")).findByText(/Charged dana's call/)).toBeInTheDocument();
    // the same entry delivered twice (a replay after reconnecting) is shown once
    push({ seq: 41, at: "2026-10-24T12:00:02Z", kind: "budget.settled", category: "spend", text: "Charged dana's call $0.0005", tone: "muted" });
    expect(within(screen.getByRole("log")).getAllByText(/Charged dana's call/)).toHaveLength(1);
    // while the socket is live the page does not also poll
    expect(events).toHaveBeenCalledTimes(1);
  });

  it("a burst of entries (a replay) refreshes the run once, not once per entry", async () => {
    const { client, push, subscribe } = setup();
    const get = vi.spyOn(client.runs, "get");
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => { expect(subscribe).toHaveBeenCalled(); });
    const before = get.mock.calls.length;
    // Count the refresh timers the burst schedules (no sleeping to see whether more refreshes arrive).
    const realSetTimeout = globalThis.setTimeout;
    const refreshTimers: number[] = [];
    const spy = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: TimerHandler, ms?: number, ...rest: unknown[]) => {
      if (ms === 300) refreshTimers.push(ms);
      return realSetTimeout(fn, ms, ...rest);
    }) as typeof setTimeout);
    try {
      for (let seq = 1; seq <= 60; seq += 1) push({ seq, at: "2026-10-24T12:00:01Z", kind: "task.created", category: "tasks", text: `entry ${String(seq)}`, tone: "muted" });
      expect(refreshTimers).toHaveLength(1);
    } finally {
      spy.mockRestore();
    }
    await waitFor(() => { expect(get.mock.calls.length).toBeGreaterThan(before); });
    expect(get.mock.calls.length - before).toBeLessThanOrEqual(2);
  });

  it("a run whose state is unknown is not offered Stop and is not polled as live", async () => {
    const { client, subscribe } = setup(runLike({ status: "brand_new_state", ended_at: null }));
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText("Unknown", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(subscribe).not.toHaveBeenCalled();
  });

  it("no live connection is opened for a run that has ended", async () => {
    const { client, subscribe } = setup(runLike());
    renderAt(`/runs/${RUN}`, client);
    await screen.findByRole("heading", { level: 1 });
    expect(subscribe).not.toHaveBeenCalled();
  });

  it("shows tasks with gates and verdicts, the spend breakdown and what the verifier ledger says about itself", async () => {
    const { client } = setup(runLike());
    renderAt(`/runs/${RUN}`, client);
    const tasks = await screen.findByRole("list", { name: "Tasks" });
    expect(within(tasks).getAllByRole("listitem").length).toBeGreaterThan(2);
    const first = within(tasks).getByTestId(`task-${RUN}-fix-add`);
    expect(within(first).getByText(/Attempt 2/)).toBeInTheDocument();
    expect(within(first).getAllByText("failed").length).toBeGreaterThan(0);
    expect(within(first).getAllByText("The project's tests pass in the sandbox").length).toBeGreaterThan(0); // once per attempt
    expect(screen.getByText("Where the money went")).toBeInTheDocument();
    expect(screen.getByText("azure-openai/gpt-5.4-mini")).toBeInTheDocument();
    const state = await screen.findByTestId("run-ledger-state");
    expect(await within(state).findByText("Verified")).toBeInTheDocument();
  });

  it("an unanchored run's ledger is never shown as verified", async () => {
    const { client } = setup(runLike());
    client.ledger.verifyRun = () => Promise.resolve({ verified: false, chainOk: true, anchorState: "unanchored" as const, entries: 32, gaps: 0, anchor: null, checkedAt: "2026-10-24T12:00:00Z" });
    renderAt(`/runs/${RUN}`, client);
    const state = await screen.findByTestId("run-ledger-state");
    expect(await within(state).findByText("Chain intact, not anchored yet")).toBeInTheDocument();
    expect(within(state).queryByText("Verified")).toBeNull();
  });

  describe("budget alerts (the real recorded shapes, clock pinned)", () => {
    const fromContract = (name: string): RunDetail => runDetail(body<RawRunDetail>("windows", name), new Date("2026-10-24T12:00:00Z"));
    const at = (iso: string): void => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date(iso));
    };
    afterEach(() => {
      vi.useRealTimers();
    });

    it("held says held; a reset budget says 'Resets' while its reset time is ahead", async () => {
      at("2026-10-24T12:00:00Z");
      const { client } = setup(fromContract("run_alerts"));
      renderAt(`/runs/${RUN}`, client);
      const items = within(await screen.findByRole("region", { name: "Budget alerts" })).getAllByRole("listitem");
      expect(items[0]).toHaveTextContent(/dana-daily.*Held: stays blocked until a person lifts it/);
      expect(items[0]).toHaveAttribute("data-held", "true");
      expect(items[1]).toHaveTextContent(/rhea-daily.*Resets Sun 25 Oct, 00:00 UTC/);
      expect(items[1]).not.toHaveTextContent(/Only a person/);
    });

    it("says 'Reset' (past) once the reset time has gone by", async () => {
      at("2026-10-26T12:00:00Z");
      const { client } = setup(fromContract("run_alerts"));
      renderAt(`/runs/${RUN}`, client);
      const items = within(await screen.findByRole("region", { name: "Budget alerts" })).getAllByRole("listitem");
      expect(items[1]).toHaveTextContent(/rhea-daily.*\bReset Sun 25 Oct, 00:00 UTC/);
      expect(items[1]).not.toHaveTextContent(/Resets/);
    });

    it("a breach in a window that had ended: 'nothing is blocked, flagged for review', never 'only a person can lift it'", async () => {
      const { client } = setup(fromContract("run_closed_window"));
      renderAt(`/runs/${RUN}`, client);
      const [item] = within(await screen.findByRole("region", { name: "Budget alerts" })).getAllByRole("listitem");
      expect(item).toHaveTextContent(/omar-daily.*\(\$0\.01 daily, spent \$0\.03\) was passed\. Overspent in a window that has ended: nothing is blocked, flagged for review\./);
      expect(item).not.toHaveTextContent(/Only a person|Held|Resets/);
      expect(item).toHaveAttribute("data-held", "false");
    });

    it("a settlement after the window ended with nothing rolling it carries a past reset time: shown as 'Reset <time>'", async () => {
      at("2026-10-25T09:30:00Z");
      const { client } = setup(fromContract("run_unrolled_window"));
      renderAt(`/runs/${RUN}`, client);
      const [item] = within(await screen.findByRole("region", { name: "Budget alerts" })).getAllByRole("listitem");
      expect(item).toHaveTextContent(/theo-daily.*\bReset Sun 25 Oct, 00:00 UTC/);
      expect(item).not.toHaveTextContent(/Resets|Only a person/);
    });
  });
});

describe("Runs list on real data", () => {
  it("filters by status through the server and says when the server stopped looking", async () => {
    const user = userEvent.setup();
    const listPage = vi.fn((filter?: { status?: string }) =>
      Promise.resolve({ runs: filter?.status === undefined ? summaries : summaries.filter((r) => r.status === filter.status), truncated: filter?.status === undefined }),
    );
    const client = serverLike({ runs: { listPage }, info: () => Promise.resolve({ name: "demo", detail: "x" }) });
    renderAt("/runs", client);
    expect(await screen.findByTestId("runs-truncated")).toHaveTextContent(/may be missing some/);
    const table = await screen.findByRole("table", { name: "Runs, newest first" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getAllByText("demo")).toHaveLength(2); // the organization, where a template would be
    expect(within(table).getAllByText("Waiting for you")).toHaveLength(2); // column and phone line
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => { expect(listPage).toHaveBeenLastCalledWith({ status: "done" }); });
    await waitFor(() => { expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2); });
    expect(screen.queryByTestId("runs-truncated")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Stopped" }));
    expect(await screen.findByText("No stopped runs.")).toBeInTheDocument();
  });

  it("shows the empty state and the unreachable-server state with a retry", async () => {
    const user = userEvent.setup();
    let fail = true;
    const client = serverLike({
      runs: {
        listPage: () => (fail ? Promise.reject(new ProblemError("Couldn't reach the NuraMesh server. Is \"nuramesh serve\" running?", 0, "unreachable")) : Promise.resolve({ runs: [], truncated: false })),
      },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });
    renderAt("/runs", client);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't reach the NuraMesh server/);
    fail = false;
    await user.click(within(screen.getByRole("main")).getByRole("button", { name: "Try again" })); // not the status bar's
    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument();
    expect(screen.getByText(/nuramesh try/)).toBeInTheDocument();
  });

  it("an unknown run id says so instead of hanging", async () => {
    const client = serverLike({ runs: { get: () => Promise.reject(new NotFoundError("run nope")), subscribe: () => () => undefined, events: () => Promise.reject(new NotFoundError("run nope")) } });
    renderAt("/runs/nope", client);
    expect(await screen.findByRole("heading", { name: "Run not found" })).toBeInTheDocument();
  });
});
