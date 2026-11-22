import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConflictError, NotAllowedError, ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";

import { OrgStatusBar } from "./org-status-bar";
import { deferred, fixture, renderSafety } from "./test-utils";

afterEach(() => {
  vi.useRealTimers();
});

const bar = () => screen.findByRole("region", { name: "Organization status" });

describe("status bar: running", () => {
  it("shows the state in words and a danger Halt button to a person", async () => {
    renderSafety(<OrgStatusBar />);
    const region = await bar();
    expect(await within(region).findByText("Organization running")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Halt organization" })).toBeInTheDocument();
    expect(within(region).queryByText(/Only a signed-in person/)).not.toBeInTheDocument();
  });

  it("shows no button, and says why in the bar's description, to anyone who is not a person", async () => {
    renderSafety(<OrgStatusBar />, fixture("viewer"));
    const region = await bar();
    await within(region).findByText("Organization running");
    expect(within(region).queryByRole("button", { name: /Halt|Resume|Pause/ })).not.toBeInTheDocument();
    const note = within(region).getByText("Only a signed-in person can halt the organization.");
    expect(region.getAttribute("aria-describedby")).toBe(note.id);
  });
});

describe("status bar: halted", () => {
  it("names who halted it, tells what stops, and offers Resume to a person; announced once as an alert", async () => {
    renderSafety(<OrgStatusBar />, fixture("halted"));
    const region = await bar();
    expect(await within(region).findByText("Organization halted")).toBeInTheDocument();
    expect(within(region).getByText(/^human:ann halted it at /)).toBeInTheDocument();
    expect(within(region).getByText("No new work or model calls start. Steps already running finish and are charged.")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Resume organization" })).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Halt organization" })).not.toBeInTheDocument();
    expect(within(region).getByRole("alert")).toHaveTextContent("Organization halted");
  });

  it("stops being an alert after the first announcement (plain text afterwards)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderSafety(<OrgStatusBar />, fixture("halted"));
    const region = await bar();
    expect(await within(region).findByRole("alert")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(region).getByText("Organization halted")).toBeInTheDocument();
  });

  it("names the breaker that halted it", async () => {
    renderSafety(<OrgStatusBar />, fixture("tripped"));
    expect(await screen.findByText(/^Breaker denied-loop halted it at /)).toBeInTheDocument();
  });

  it("explains a halt caused by failing breaker checks: error class, phase, rule, count, what to do", async () => {
    renderSafety(<OrgStatusBar />, fixture("poison"));
    expect(
      await screen.findByText(/Breaker checks were failing, so it was halted at .*\. Cause: KeyError while evaluating rule spend-spike, after 3 failed checks\./),
    ).toBeInTheDocument();
    expect(screen.getByText("Fix the cause, restart the server, then resume.")).toBeInTheDocument();
    expect(screen.queryByText("Fix the cause, restart the server, then resume.", { selector: "[hidden]" })).not.toBeInTheDocument();
  });

  it("does not show the restart advice for other causes", async () => {
    renderSafety(<OrgStatusBar />, fixture("halted"));
    await screen.findByText("Organization halted");
    expect(screen.queryByText(/restart the server/)).not.toBeInTheDocument();
  });

  it("shows no Resume button to anyone who is not a person", async () => {
    const api = fixture("halted");
    const status = await api.org.status();
    api.org.status = () => Promise.resolve({ ...status, canAct: false });
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    expect(await within(region).findByText("Only a signed-in person can resume it.")).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /Resume organization/ })).not.toBeInTheDocument();
  });
});

describe("status bar: solo approvals after a removal (solo approval SA-18 item 4)", () => {
  const LINE = /^solo approvals within 7 days of an approver being removed: /;

  it.each(["running", "halted"] as const)("prints the count while %s, whenever it is above 0", async (state) => {
    const api = fixture(state);
    const status = await api.org.status();
    api.org.status = () => Promise.resolve({ ...status, soloAfterRemoval: 2 });
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    expect(await within(region).findByText("solo approvals within 7 days of an approver being removed: 2")).toBeInTheDocument();
  });

  it("prints nothing when no solo approval followed a removal", async () => {
    renderSafety(<OrgStatusBar />);
    const region = await bar();
    await within(region).findByText("Organization running");
    expect(within(region).queryByText(LINE)).not.toBeInTheDocument();
  });
});

describe("status bar: loading, error, stale", () => {
  it("says it is checking while the first read is out, with no buttons", async () => {
    const api = createFixtureClient();
    const pending = deferred<Awaited<ReturnType<typeof api.org.status>>>();
    api.org.status = () => pending.promise;
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    expect(within(region).getByRole("status")).toHaveTextContent("Checking organization status...");
    expect(within(region).queryByRole("button")).not.toBeInTheDocument();
    pending.resolve(await createFixtureClient().org.status());
    expect(await within(region).findByText("Organization running")).toBeInTheDocument();
  });

  it("says it could not read the state and offers Try again; the pages stay usable (no alert role)", async () => {
    const user = userEvent.setup();
    const api = createUnavailableClient();
    const status = vi.spyOn(api.org, "status");
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    expect(await within(region).findByText("Could not read the organization status.")).toBeInTheDocument();
    expect(within(region).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /Halt|Resume/ })).not.toBeInTheDocument();
    const calls = status.mock.calls.length;
    await user.click(within(region).getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(status.mock.calls.length).toBeGreaterThan(calls);
    });
  });

  it("keeps the last good state and adds a stale notice when a later read fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = createFixtureClient();
    const first = await api.org.status();
    let fail = false;
    api.org.status = () => (fail ? Promise.reject(new ProblemError("down", 0, "unreachable")) : Promise.resolve(first));
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await within(region).findByText("Organization running");
    expect(within(region).queryByText(/Status may be out of date/)).not.toBeInTheDocument();
    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(await within(region).findByText(/Status may be out of date \(last read /)).toBeInTheDocument();
    expect(within(region).getByText("Organization running")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Halt organization" })).toBeInTheDocument();
  });
});

describe("status bar: polling", () => {
  it("reads every 5 seconds and shows a halt that someone else made", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = createFixtureClient();
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await within(region).findByText("Organization running");
    await api.org.halt();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(await within(region).findByText("Organization halted")).toBeInTheDocument();
  });

  it("reads again when the window comes back to the front", async () => {
    const api = createFixtureClient();
    const status = vi.spyOn(api.org, "status");
    renderSafety(<OrgStatusBar />, api);
    await within(await bar()).findByText("Organization running");
    const before = status.mock.calls.length;
    act(() => {
      window.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => {
      expect(status.mock.calls.length).toBeGreaterThan(before);
    });
  });
});

describe("halt dialog", () => {
  async function open(api = createFixtureClient()) {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    const opener = await within(region).findByRole("button", { name: "Halt organization" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Halt the whole organization?" });
    return { user, api, region, opener, dialog };
  }

  it("asks first, with the exact words, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("Every open run is asked to stop and no new task or model call starts until a person resumes. Steps already running finish and are charged.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "Halt organization" })).toBeInTheDocument();
  });

  it("Escape cancels without halting and focus returns to the opener", async () => {
    const { user, api, opener } = await open();
    const halt = vi.spyOn(api.org, "halt");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(halt).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  });

  it("Cancel does nothing else", async () => {
    const { user, api, dialog } = await open();
    const halt = vi.spyOn(api.org, "halt");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(halt).not.toHaveBeenCalled();
    expect(await screen.findByText("Organization running")).toBeInTheDocument();
  });

  it("halts once, shows Halting... while sending with both buttons off, then the bar turns halted and says so politely", async () => {
    const api = createFixtureClient();
    const real = api.org.halt.bind(api.org);
    const gate = deferred<undefined>();
    const halt = vi.fn(async () => {
      await gate.promise;
      return real();
    });
    api.org.halt = halt;
    const { user, region, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Halt organization" }));
    expect(await within(dialog).findByRole("button", { name: "Halting..." })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Halting..." }));
    gate.resolve(undefined);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(halt).toHaveBeenCalledTimes(1);
    expect(await within(region).findByText("Organization halted")).toBeInTheDocument();
    expect(document.querySelector("[aria-live='polite']")).toHaveTextContent("Organization halted");
    expect(within(region).getByRole("button", { name: "Resume organization" })).toHaveFocus();
  });

  it("treats 409 already_halted as done: closes and shows the halted bar", async () => {
    const api = fixture("halted");
    const status = await api.org.status();
    // the bar still believes it is running when the person presses the button
    let reads = 0;
    api.org.status = () => Promise.resolve(reads++ === 0 ? { ...status, halted: false, by: null, at: null, cause: null } : status);
    const { user, region, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Halt organization" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await within(region).findByText("Organization halted")).toBeInTheDocument();
  });

  it("keeps the dialog open with the problem when the halt fails", async () => {
    const api = createFixtureClient();
    api.org.halt = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Halt organization" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not halt: Only a signed-in person can do this");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Halt organization" })).toBeEnabled();
  });

  it("shows the server's problem title for a server problem", async () => {
    const api = createFixtureClient();
    api.org.halt = () => Promise.reject(new ProblemError("store broke", 500, "store_error"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Halt organization" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not halt: store_error");
  });
});

describe("resume dialog", () => {
  async function open(scenario: Parameters<typeof fixture>[0] = "halted", api = fixture(scenario)) {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await user.click(await within(region).findByRole("button", { name: "Resume organization" }));
    const dialog = await screen.findByRole("dialog", { name: "Resume the organization?" });
    return { user, api, region, dialog };
  }

  it("says what a resume does not undo, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("New work can start again. Stopped runs are not restarted, paused roles stay paused and tripped breakers stay tripped until a person resumes or resets them.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("lists the breaker that is still tripped", async () => {
    const { dialog } = await open("tripped");
    expect(await within(dialog).findByText("Breaker denied-loop is still tripped.")).toBeInTheDocument();
  });

  it("lists paused roles", async () => {
    const api = fixture("halted");
    await api.org.pauseRole("writer");
    const { dialog } = await open("halted", api);
    expect(within(dialog).getByText("Role writer is still paused.")).toBeInTheDocument();
  });

  it("lists at most three of each, then counts the rest", async () => {
    const api = fixture("halted");
    for (const role of ["researcher", "writer", "verifier"]) await api.org.pauseRole(role);
    const status = await api.org.status();
    api.org.status = () => Promise.resolve({ ...status, roles: [...status.roles, ...["a", "b", "c"].map((role) => ({ ...status.roles[0]!, role }))] });
    const { dialog } = await open("halted", api);
    expect(within(dialog).getAllByText(/^Role .* is still paused\.$/)).toHaveLength(3);
    expect(within(dialog).getByText("and 3 more")).toBeInTheDocument();
  });

  it("resumes once and the bar turns to running", async () => {
    const { user, api, region, dialog } = await open();
    const resume = vi.spyOn(api.org, "resume");
    await user.click(within(dialog).getByRole("button", { name: "Resume organization" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(resume).toHaveBeenCalledTimes(1);
    expect(await within(region).findByText("Organization running")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Halt organization" })).toHaveFocus();
  });

  it("treats 409 already_running as done", async () => {
    const api = fixture("halted");
    api.org.resume = () => Promise.reject(new ConflictError("not halted", "already_running"));
    const { user, dialog } = await open("halted", api);
    await user.click(within(dialog).getByRole("button", { name: "Resume organization" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("shows an error and stays open when the resume is refused", async () => {
    const api = fixture("halted");
    api.org.resume = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open("halted", api);
    await user.click(within(dialog).getByRole("button", { name: "Resume organization" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not resume: Only a signed-in person can do this");
  });
});
