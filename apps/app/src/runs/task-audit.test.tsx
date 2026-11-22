import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NotFoundError, type ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import {
  taskAuditBroken,
  taskAuditF1,
  taskAuditF11,
  taskAuditF2,
  taskAuditF3,
  taskAuditF5,
  taskAuditF6,
  taskAuditF8,
  taskAuditF9,
} from "@/api/fixtures/task-audit";
import type { RawTaskAudit } from "@/api/server/raw";
import { taskAudit } from "@/api/server/map";
import type { LiveStatus, RunEvent } from "@/api/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";
import { createUnavailableClient } from "@/test/unavailable-client";

import { formatClock } from "./format";

afterEach(() => {
  vi.useRealTimers();
});

/** A client whose audit answer a test controls; the stream is a fake the test pushes into. */
function world(initial: RawTaskAudit = taskAuditF1()) {
  let current = initial;
  let failNext: Error | null = null;
  const calls: number[] = [];
  let push: (e: RunEvent) => void = () => undefined;
  let report: (s: LiveStatus) => void = () => undefined;
  const base = createFixtureClient();
  const client: ApiClient = {
    ...base,
    runs: {
      ...base.runs,
      taskAudit: () => {
        calls.push(Date.now());
        if (failNext !== null) return Promise.reject(failNext);
        return Promise.resolve(taskAudit(current));
      },
      events: () => Promise.resolve({ events: [], nextSeq: 0 }),
      subscribe: (_id, on, options) => {
        push = on;
        report = options?.onStatus ?? (() => undefined);
        return () => undefined;
      },
    },
  };
  return {
    client,
    calls,
    set: (next: RawTaskAudit) => {
      current = next;
    },
    fail: (error: Error) => {
      failNext = error;
    },
    push: (e: RunEvent) => {
      act(() => {
        push(e);
      });
    },
    status: (s: LiveStatus) => {
      act(() => {
        report(s);
      });
    },
  };
}

const ev = (seq: number, taskId: string | null, kind = "model.called"): RunEvent => ({
  seq,
  at: "2026-10-25T10:01:00Z",
  kind,
  category: "spend",
  text: `event ${String(seq)}`,
  tone: "muted",
  entry: `run:r1@${String(seq)}`,
  taskId,
});

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

const open = async (client: ApiClient = createFixtureClient(), path = "/runs/r1/tasks/t1") => {
  renderAt(path, client);
  await screen.findByRole("heading", { level: 1 });
};
const region = (name: string) => screen.getByRole("region", { name });
const seqsShown = () => within(screen.getByRole("log")).getAllByRole("listitem").map((li) => li.getAttribute("data-seq"));

describe("the task audit page, fixture F1", () => {
  it("U1 shows the task, the spend and the chain", async () => {
    await open();
    expect(screen.getByRole("heading", { level: 1, name: "Draft summary" })).toBeInTheDocument();
    const summary = region("Summary");
    expect(within(summary).getByText("done")).toBeInTheDocument();
    expect(within(summary).getByText("agent:writer")).toBeInTheDocument();
    expect(within(summary).getByText("attempt 2 of 3")).toBeInTheDocument();
    // the server's `at_least` (the known 0.06 only: the charge is an upper bound), never the bare total
    expect(within(summary).getByText("at least $0.06000000")).toBeInTheDocument();
    expect(within(summary).getByText("3 calls, 1 with cost unknown (charged the reservation)")).toBeInTheDocument();
    expect(summary).not.toHaveTextContent(/unknown price|no price/i);
    expect(within(summary).getByText("Chain verified")).toBeInTheDocument();
    expect(within(summary).getByText("head: entry 24")).toBeInTheDocument();
  });

  it("U1b a call of unknown cost reads 'Cost unknown (charged the reservation)' in the ledger, from the server's cost basis", async () => {
    await open();
    const log = within(screen.getByRole("log"));
    expect(log.getAllByText("Cost unknown (charged the reservation)")).toHaveLength(1);
    expect(screen.getByRole("log")).not.toHaveTextContent(/price unknown|No price/i);
  });

  it("U2 lists the attempts with their gates and each attempt's spend", async () => {
    await open();
    const attempts = region("Attempts");
    expect(within(attempts).getByRole("heading", { name: "Attempt 1" })).toBeInTheDocument();
    expect(within(attempts).getByRole("heading", { name: "Attempt 2" })).toBeInTheDocument();
    expect(within(attempts).getAllByText("g-rule")).toHaveLength(2);
    expect(within(attempts).getByText("Not met:")).toBeInTheDocument();
    expect(within(attempts).getAllByText("Second thing")).toHaveLength(2);
    expect(within(attempts).getByText("Attempt 1: $0.06000000 in 2 calls")).toBeInTheDocument();
    expect(within(attempts).getByText("Attempt 2: at least $0.00000000 in 1 call, 1 with cost unknown (charged the reservation)")).toBeInTheDocument();
    expect(attempts).not.toHaveTextContent(/(?<!at least )\$0(?![.\d])/); // a bare "$0" never; "at least $0" is the known lower bound
  });

  it("U3 shows the rework round", async () => {
    await open();
    const rework = region("Rework");
    expect(rework).toHaveTextContent("sent back by a gate: gate failed: g-ver");
    expect(rework).toHaveTextContent(`feedback kept as hash ${"a".repeat(12)}...`);
    expect(rework).toHaveTextContent("attempt 2 of 3");
  });

  it("U4 shows the approval and its reason only as a hash, with no reveal", async () => {
    await open();
    const approvals = region("Approvals and decisions");
    expect(approvals).toHaveTextContent("a1");
    expect(approvals).toHaveTextContent("approved by human:u1");
    expect(approvals).toHaveTextContent(`Reason kept in the content store (hash ${"b".repeat(12)}...)`);
    expect(screen.queryByRole("button", { name: /Show reason/ })).toBeNull();
  });

  it("U6 lists 21 entries in order and filters them", async () => {
    const user = userEvent.setup();
    await open();
    const seqs = () => seqsShown().map(Number);
    expect(seqs()).toEqual(Array.from({ length: 21 }, (_, i) => i + 2));
    const log = screen.getByRole("log");
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(within(log).getByRole("link", { name: "#13" })).toHaveAttribute("href", "/ledger/run%3Ar1%4013");
    const pick = async (name: string) => {
      await user.click(screen.getByRole("button", { name }));
      return seqs();
    };
    expect(await pick("Gates")).toEqual([11, 13, 20, 21]);
    expect(await pick("Spend")).toEqual([6, 7, 8, 12, 15]);
    expect(await pick("Approvals")).toEqual([18, 19]);
    expect(await pick("Rework")).toEqual([14]);
    expect(await pick("All")).toContain(5); // policy.decided is under All only
  });

  it("U19 the filter pills are buttons that toggle with Enter and Space", async () => {
    const user = userEvent.setup();
    await open();
    const gates = screen.getByRole("button", { name: "Gates" });
    gates.focus();
    await user.keyboard("{Enter}");
    expect(gates).toHaveAttribute("aria-pressed", "true");
    await user.keyboard(" ");
    expect(gates).toHaveAttribute("aria-pressed", "true"); // pressing a pill selects it; it is a radio-like filter
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "false");
  });
});

describe("the other states", () => {
  it("U5 a person's reason is not on the page", async () => {
    const w = world(taskAuditF5());
    await open(w.client, "/runs/r1/tasks/t5");
    expect(region("Rework")).toHaveTextContent("sent back by a person: A person's reason (not shown here)");
    expect(document.body).not.toHaveTextContent("please try again with the new data");
    expect(region("Approvals and decisions")).toHaveTextContent("human:u1 decided gate g-human: rejected");
    expect(region("Approvals and decisions")).toHaveTextContent(`reason hash ${"c".repeat(12)}...`);
  });

  it("U12 a source without the audit says so and shows no sections", async () => {
    renderAt("/runs/r1/tasks/t1", createUnavailableClient());
    expect(await screen.findByText("This data source cannot give a task's audit trail")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Attempts" })).toBeNull();
  });

  // 1000 list items render slowly under load: the generous timeout only guards a hang; nothing asserts a duration.
  it("U13 a task with 1001 entries shows 1000 and says so, with the full spend", { timeout: 60_000 }, async () => {
    const w = world(taskAuditF8());
    await open(w.client, "/runs/r1/tasks/t4");
    const notice = screen.getByText(/^Showing the first 1000 of 1001 entries; the rest are in the/);
    expect(notice).toHaveTextContent("Showing the first 1000 of 1001 entries; the rest are in the ledger");
    expect(within(notice).getByRole("link", { name: "ledger" })).toHaveAttribute("href", expect.stringContaining("/ledger"));
    expect(screen.getByRole("log").querySelectorAll("li[data-seq]")).toHaveLength(1000);
    expect(region("Summary")).toHaveTextContent("1000 calls");
    expect(document.body).not.toHaveTextContent(/partial/i);
  });

  it("U14 a task with only task.created has empty sections and no error", async () => {
    const w = world(taskAuditF9());
    await open(w.client, "/runs/r1/tasks/t3");
    expect(region("Attempts")).toHaveTextContent("Nothing yet");
    expect(region("Rework")).toHaveTextContent("Not sent back.");
    expect(region("Approvals and decisions")).toHaveTextContent("Nothing yet.");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("U15 an unknown task is the shared not-found page", async () => {
    renderAt("/runs/r1/tasks/nope");
    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });

  it("U21 failed calls without a cost are counted as failed, not as unknown price", async () => {
    const w = world(taskAuditF11());
    await open(w.client, "/runs/r1/tasks/t12");
    const summary = region("Summary");
    expect(within(summary).getByText("$0.01000000")).toBeInTheDocument();
    expect(summary).toHaveTextContent("2 failed, no cost");
    expect(summary).not.toHaveTextContent(/unknown price|cost unknown/);
    expect(screen.getAllByText("no cost (call failed)")).toHaveLength(2);
  });

  it("U22 every kind of decision in its own words, and the incident note is never shown", async () => {
    // The fixture really holds the note text (on the wire), so this test can fail if the page ever shows it.
    expect(JSON.stringify(taskAuditF6())).toContain("NOTE-TEXT-FIXTURE");
    const w = world(taskAuditF6());
    await open(w.client, "/runs/r1/tasks/t1");
    const items = within(region("Approvals and decisions")).getAllByTestId("decision");
    expect(items).toHaveLength(8);
    expect(new Set(items.map((i) => i.textContent)).size).toBe(8);
    expect(items[1]).toHaveTextContent("Engine incident: the task is stuck in needs_human, a person is needed");
    expect(items[3]).toHaveTextContent("Scripted approval of gate g1 as human:u3, no human review");
    expect(document.body).not.toHaveTextContent("NOTE-TEXT-FIXTURE");
  });

  it("U25 spend by attempt, with a running attempt that has no gate round", async () => {
    const w = world(taskAuditF2());
    await open(w.client, "/runs/r1/tasks/t1");
    const attempts = region("Attempts");
    expect(within(attempts).getByText("Before work started: $0.01000000 in 1 call")).toBeInTheDocument();
    expect(within(attempts).getByText("Attempt 1: $0.03000000 in 1 call")).toBeInTheDocument();
    expect(within(attempts).getByText("Attempt 2: $0.00500000 in 1 call (still running)")).toBeInTheDocument();
    expect(within(attempts).queryByRole("heading", { name: "Attempt 2" })).toBeNull();
  });

  it("U26 two submissions in one attempt show one spend line", async () => {
    const w = world(taskAuditF3());
    await open(w.client, "/runs/r1/tasks/t1");
    const attempts = region("Attempts");
    expect(within(attempts).getAllByText(/^Attempt 1: /)).toHaveLength(1);
    expect(within(attempts).getByText("Attempt 1: $0.03000000 in 2 calls")).toBeInTheDocument();
  });

  it("U27 a broken chain is named and every number is still shown", async () => {
    const w = world(taskAuditBroken());
    await open(w.client);
    const summary = region("Summary");
    expect(within(summary).getByText("Chain broken")).toBeInTheDocument();
    expect(within(summary).getByText("head: entry 24")).toBeInTheDocument();
    expect(within(summary).getByText("at least $0.06000000")).toBeInTheDocument();
    expect(seqsShown()).toHaveLength(21);
  });
});

describe("live behaviour", () => {
  const newRow = (): RawTaskAudit => {
    const a = taskAuditF2();
    return { ...a, entries: [...a.entries, ...a.entries.slice(1, 2).map((e) => ({ ...e, seq: 40 }))], total_entries: a.entries.length + 1 };
  };
  const start = async (w: ReturnType<typeof world>) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await open(w.client);
    w.status("live");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
  };
  const run = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  it("U7 a new entry of this task refetches once after 300 ms and adds a row", async () => {
    const w = world(taskAuditF2());
    await start(w);
    const before = seqsShown().length;
    w.calls.length = 0;
    w.set(newRow());
    w.push(ev(40, "t1"));
    await run(200);
    expect(w.calls).toHaveLength(0);
    await run(300);
    expect(w.calls).toHaveLength(1);
    await waitFor(() => { expect(seqsShown()).toHaveLength(before + 1); });
    expect(screen.getByTestId("audit-mode")).toHaveTextContent("live");
  });

  it("U8 when the socket drops the page polls every 3 s and refetches once on reconnect", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.status("reconnecting");
    expect(screen.getByTestId("audit-mode")).toHaveTextContent("connection lost, reconnecting");
    w.calls.length = 0;
    w.set(newRow());
    await run(3_500);
    expect(w.calls.length).toBeGreaterThanOrEqual(1);
    await waitFor(() => { expect(seqsShown()).toHaveLength(3); });
    w.calls.length = 0;
    w.status("live");
    await run(2_600); // the last refetch was the poll at 3 s: the reconnect one waits out the 2 s gap
    expect(w.calls).toHaveLength(1);
  });

  it("U9 when the task is done and the run ends there is one last refetch and then silence", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.set({ ...taskAuditF1() });
    w.push(ev(41, "t1", "task.transitioned"));
    w.push(ev(42, null, "run.ended"));
    await run(3_000);
    await waitFor(() => { expect(screen.getByTestId("audit-mode")).toHaveTextContent("not live (the run has ended)"); });
    const n = w.calls.length;
    await run(10_000);
    expect(w.calls).toHaveLength(n);
  });

  it("U10 a failed refetch keeps the old data and says when it is from", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-10-25T10:00:03.000Z"));
    const w = world(taskAuditF2());
    await open(w.client);
    w.status("live");
    await run(50);
    w.fail(new Error("boom"));
    w.push(ev(40, "t1"));
    await run(400);
    expect(await screen.findByText(`Could not refresh; showing the data from ${formatClock("2026-10-25T10:00:03.000Z")}`)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(seqsShown().length).toBeGreaterThan(0);
  });

  it("U11 a refetch that answers 404 replaces the page with not-found", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.fail(new NotFoundError("Task t1"));
    w.push(ev(40, "t1"));
    await run(400);
    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });

  it("U23 an event of another task does not refetch; a run-level event does", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.calls.length = 0;
    w.push(ev(40, "t2"));
    await run(1_000);
    expect(w.calls).toHaveLength(0);
    w.push(ev(41, null, "run.ended"));
    await run(3_000);
    expect(w.calls).toHaveLength(1);
  });

  it("U24 no poll while the socket is open; the poll starts when it drops", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.calls.length = 0;
    await run(10_000);
    expect(w.calls).toHaveLength(0);
    w.status("reconnecting");
    await run(3_500);
    expect(w.calls.length).toBeGreaterThanOrEqual(1);
  });

  it("U28 two events 0.4 s apart give a refetch at 0.3 s and one at 2.3 s, never two within 2 s", async () => {
    const w = world(taskAuditF2());
    await start(w);
    w.calls.length = 0;
    const t0 = Date.now();
    w.push(ev(40, "t1"));
    await run(400);
    w.push(ev(41, "t1"));
    await run(600);
    expect(w.calls).toHaveLength(1);
    const first = w.calls[0] ?? 0;
    expect(first - t0).toBeGreaterThanOrEqual(290);
    expect(first - t0).toBeLessThan(450);
    await run(1_600); // 2.6 s in: the held one ran at 2.3 s
    expect(w.calls).toHaveLength(2);
    expect((w.calls[1] ?? 0) - first).toBeGreaterThanOrEqual(1_990);
  });
});

describe("the fixture client's seeded run (run-128)", () => {
  // The first render of a lazy route is slow under load; the timeouts only guard a hang.
  it("U29 the run page links each task to its audit trail and the page opens with the failed call and the incident", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    renderAt("/runs/run-128");
    const links = await screen.findAllByRole("link", { name: /Audit trail/ }, { timeout: 20_000 });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/runs/run-128/tasks/t-plan",
      "/runs/run-128/tasks/t-retries",
      "/runs/run-128/tasks/t-retest",
    ]);
    const link = links[1];
    if (link === undefined) throw new Error("no link");
    await user.click(link);
    expect(await screen.findByRole("heading", { level: 1, name: "Add retries to the payment webhook" })).toBeInTheDocument();
    expect(region("Summary")).toHaveTextContent("4 calls, 1 with cost unknown (charged the reservation), 1 failed, no cost");
    expect(region("Approvals and decisions")).toHaveTextContent("Engine incident: the task is stuck in in_progress");
    expect(document.body).not.toHaveTextContent("NOTE-TEXT-FIXTURE");
    expect(document.body).not.toHaveTextContent("not found");
  });

  it("U30 the pending approval of run-128 links to its task's audit trail", async () => {
    renderAt("/approvals");
    expect((await screen.findAllByRole("link", { name: "t-retries" }))[0]).toHaveAttribute("href", "/runs/run-128/tasks/t-retries");
  });

  it("U31 taskAuditUnavailable makes the audit say it is not available", async () => {
    renderAt("/runs/run-128/tasks/t-plan", createFixtureClient({ taskAuditUnavailable: true }));
    expect(await screen.findByText("This data source cannot give a task's audit trail")).toBeInTheDocument();
  });

  it("U32 a task the seeded run does not have is the not-found page", async () => {
    renderAt("/runs/run-128/tasks/nope");
    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });
});
