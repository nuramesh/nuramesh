import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConflictError, NotAllowedError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { BreakerRow, BreakersView } from "@/api/types";
import { createUnavailableClient } from "@/test/unavailable-client";

import { BreakersPanel } from "./breakers-panel";
import { deferred, fixture, renderSafety } from "./test-utils";

afterEach(() => {
  vi.useRealTimers();
});

const panel = () => screen.findByRole("region", { name: "Circuit breakers" });

/** A client whose breakers list is the fixture's, changed by `edit`. */
function edited(scenario: Parameters<typeof fixture>[0], edit: (view: BreakersView) => BreakersView) {
  const api = fixture(scenario);
  const real = api.breakers.list.bind(api.breakers);
  api.breakers.list = async () => edit(await real());
  return api;
}

describe("breakers panel: table", () => {
  it("is a labelled section with a captioned table and real column headers", async () => {
    renderSafety(<BreakersPanel />);
    const region = await panel();
    expect(within(region).getByRole("heading", { name: "Circuit breakers" })).toBeInTheDocument();
    const table = await within(region).findByRole("table", { name: "Circuit breakers" });
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual(["Rule", "Signal", "Action", "Window", "Observed / limit", "State", "Tripped", "Actions"]);
    for (const h of headers) expect(h).toHaveAttribute("scope", "col");
    expect(within(table).getAllByRole("rowheader")).toHaveLength(3);
    // the table scrolls inside a focusable, labelled region on a narrow screen
    expect(table.closest("[tabindex='0']")).toHaveAttribute("aria-label");
  });

  it("shows an ok rule: signal, action, window, live count and an OK chip in words", async () => {
    renderSafety(<BreakersPanel />);
    await panel();
    const row = await screen.findByRole("row", { name: /denied-loop/ });
    expect(within(row).getByText("Denied actions")).toBeInTheDocument();
    expect(within(row).getByText("Halt organization")).toBeInTheDocument();
    expect(within(row).getByText("10 minutes")).toBeInTheDocument();
    expect(within(row).getByText("4 of 20 events")).toBeInTheDocument();
    expect(within(row).getByText("OK")).toBeInTheDocument();
  });

  it("shows spend in USD with a 1 hour window", async () => {
    renderSafety(<BreakersPanel />);
    const row = await screen.findByRole("row", { name: /spend-spike/ });
    expect(within(row).getByText("Spend")).toBeInTheDocument();
    expect(within(row).getByText("1 hour")).toBeInTheDocument();
    expect(within(row).getByText("3.5 of 25 USD")).toBeInTheDocument();
  });

  it("marks a dry-run rule with a chip", async () => {
    renderSafety(<BreakersPanel />);
    const row = await screen.findByRole("row", { name: /watch-gates/ });
    expect(within(row).getByText("Dry run")).toBeInTheDocument();
    expect(within(row).getByText("30 minutes")).toBeInTheDocument();
    expect(within(row).getByText("0 of 5 events")).toBeInTheDocument();
  });

  it("shows a tripped rule: the recorded count at trip, who tripped it and when, a Tripped chip", async () => {
    renderSafety(<BreakersPanel />, fixture("tripped"));
    const row = await screen.findByRole("row", { name: /denied-loop/ });
    expect(within(row).getByText("21 of 20 events at trip")).toBeInTheDocument();
    expect(within(row).getByText("Tripped")).toBeInTheDocument();
    expect(within(row).getByText(/ by service:breaker$/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Reset breaker denied-loop" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Disable breaker/ })).not.toBeInTheDocument();
  });

  it("says a manual trip is a manual trip", async () => {
    const api = edited("tripped", (v) => ({ ...v, breakers: v.breakers.map((b) => (b.state === "tripped" ? { ...b, manual: true, trippedBy: "human:ann" } : b)) }));
    renderSafety(<BreakersPanel />, api);
    const row = await screen.findByRole("row", { name: /denied-loop/ });
    expect(within(row).getByText("Manual trip by human:ann")).toBeInTheDocument();
  });

  it("shows a disabled rule: chip word, who and when, the reason label, and Enable", async () => {
    renderSafety(<BreakersPanel />, fixture("disabled"));
    const row = await screen.findByRole("row", { name: /watch-gates/ });
    expect(within(row).getByText("Disabled", { selector: ".chip" })).toBeInTheDocument();
    expect(within(row).getByText(/^Disabled .* by human:ann: Planned work$/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Enable breaker watch-gates" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Disable breaker|Reset breaker/ })).not.toBeInTheDocument();
    expect(within(row).getByText("0 of 5 events")).toBeInTheDocument(); // the live count is still shown
  });

  it("counts calls with unknown cost, never guessing their price", async () => {
    const api = edited("running", (v) => ({ ...v, breakers: v.breakers.map((b) => (b.name === "spend-spike" ? { ...b, unknownCostCalls: 2 } : b)) }));
    renderSafety(<BreakersPanel />, api);
    const row = await screen.findByRole("row", { name: /spend-spike/ });
    expect(within(row).getByText("3.5 of 25 USD + 2 calls with unknown cost")).toBeInTheDocument();
  });

  it("scopes a rule to a role and says what it does to it", async () => {
    const scoped: Partial<BreakerRow> = { name: "writer-gates", signal: "gate_failed", role: "writer", action: "pause_role", windowSeconds: 45 };
    const api = edited("running", (v) => ({ ...v, breakers: [...v.breakers, { ...v.breakers[1]!, ...scoped }] }));
    renderSafety(<BreakersPanel />, api);
    const row = await screen.findByRole("row", { name: /writer-gates/ });
    expect(within(row).getByText("Failed gates for role writer")).toBeInTheDocument();
    expect(within(row).getByText("Pause role writer")).toBeInTheDocument();
    expect(within(row).getByText("45 seconds")).toBeInTheDocument();
  });

  it("shows a tripped rule that was removed from the config", async () => {
    const api = edited("tripped", (v) => ({ ...v, breakers: v.breakers.map((b) => (b.name === "denied-loop" ? { ...b, inConfig: false } : b)) }));
    renderSafety(<BreakersPanel />, api);
    const row = await screen.findByRole("row", { name: /denied-loop/ });
    expect(within(row).getByText("Tripped, removed from config")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Reset breaker denied-loop" })).toBeInTheDocument();
  });

  it("shows a disabled orphan, which has nothing to compute, with Enable", async () => {
    const orphan: BreakerRow = {
      ...seedRow(),
      name: "old-rule",
      signal: null,
      action: null,
      windowSeconds: null,
      limit: null,
      observed: null,
      observedAs: null,
      windowStart: null,
      state: "disabled",
      disabledAt: "2026-11-01T11:30:00+00:00",
      disabledBy: "human:ann",
      disabledReason: "rule_being_replaced",
      inConfig: false,
    };
    const api = edited("running", (v) => ({ ...v, breakers: [...v.breakers, orphan] }));
    renderSafety(<BreakersPanel />, api);
    const row = await screen.findByRole("row", { name: /old-rule/ });
    expect(within(row).getByText(/: Rule is being replaced$/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Enable breaker old-rule" })).toBeInTheDocument();
  });

  it("says the line 'Checked <time> ago'", async () => {
    renderSafety(<BreakersPanel />);
    expect(await screen.findByText(/^Checked \d+ seconds? ago$/)).toBeInTheDocument();
  });

  it("says 'Not checked yet' when no check has run", async () => {
    const api = edited("running", (v) => ({ ...v, evaluation: { ...v.evaluation, lastEvaluatedAt: null, state: "starting" } }));
    renderSafety(<BreakersPanel />, api);
    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
  });

  it("counts disabled rules under the heading", async () => {
    renderSafety(<BreakersPanel />, fixture("disabled"));
    expect(await screen.findByText("1 breaker is disabled.")).toBeInTheDocument();
    const api = edited("disabled", (v) => ({ ...v, breakers: v.breakers.map((b) => (b.name === "denied-loop" ? { ...b, state: "disabled" as const, disabledReason: "other" as const, disabledBy: "human:ann", disabledAt: "2026-11-01T11:31:00+00:00" } : b)) }));
    renderSafety(<BreakersPanel />, api);
    expect(await screen.findByText("2 breakers are disabled.")).toBeInTheDocument();
  });

  it("says nothing about disabled rules when none is", async () => {
    renderSafety(<BreakersPanel />);
    await screen.findByRole("table");
    expect(screen.queryByText(/disabled\.$/)).not.toBeInTheDocument();
  });
});

function seedRow(): BreakerRow {
  return {
    name: "x", signal: "spend", action: "halt_org", role: null, windowSeconds: 60, dryRun: false, limit: "1", observed: "0", observedAs: "live",
    unknownCostCalls: 0, windowStart: null, state: "ok", trippedAt: null, trippedSeq: null, trippedBy: null, manual: false, lastResetAt: null,
    disabledAt: null, disabledBy: null, disabledReason: null, inConfig: true,
  };
}

describe("breakers panel: who sees what", () => {
  it("shows no Reset, Disable or Enable and no Actions column to anyone who is not a person, and says why", async () => {
    renderSafety(<BreakersPanel />, fixture("viewer"));
    const table = await screen.findByRole("table", { name: "Circuit breakers" });
    expect(within(table).queryByRole("button")).not.toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
    expect(screen.getByText("Only a signed-in person can reset, disable or enable a breaker.")).toBeInTheDocument();
  });

  it("does not show that line to a person", async () => {
    renderSafety(<BreakersPanel />);
    await screen.findByRole("table");
    expect(screen.queryByText(/Only a signed-in person can reset/)).not.toBeInTheDocument();
  });

  it("gives a person Disable on ok rows, Reset on tripped rows, Enable on disabled rows", async () => {
    renderSafety(<BreakersPanel />);
    await screen.findByRole("table");
    expect(screen.getAllByRole("button", { name: /^Disable breaker / })).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /^(Reset|Enable) breaker / })).not.toBeInTheDocument();
  });

  it("the buttons use the person's say only: canAct comes from the halt object of the same read", async () => {
    const api = edited("running", (v) => ({ ...v, halt: { ...v.halt, canAct: false } }));
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("breakers panel: evaluation, empty, loading, error, polling", () => {
  it("warns when the checks are failing, and says the organization will be halted", async () => {
    renderSafety(<BreakersPanel />, fixture("failing"));
    expect(await screen.findByText(/^Breaker checks are failing since .* \(KeyError in rule spend-spike\)\. After 3 failed checks the organization is halted automatically\.$/)).toBeInTheDocument();
  });

  it("leaves out the halt sentence when every rule is a dry run", async () => {
    const api = edited("failing", (v) => ({ ...v, breakers: v.breakers.map((b) => ({ ...b, dryRun: true })) }));
    renderSafety(<BreakersPanel />, api);
    const warning = await screen.findByText(/^Breaker checks are failing since /);
    expect(warning).not.toHaveTextContent("halted automatically");
  });

  it("leaves out the rule name when none failed", async () => {
    const api = edited("failing", (v) => ({ ...v, evaluation: { ...v.evaluation, lastFailingRule: null } }));
    renderSafety(<BreakersPanel />, api);
    expect(await screen.findByText(/\(KeyError\)\. After 3 failed checks/)).toBeInTheDocument();
  });

  it("says when the checks are not running on this server", async () => {
    const api = edited("running", (v) => ({ ...v, evaluation: { ...v.evaluation, state: "stopped" } }));
    renderSafety(<BreakersPanel />, api);
    expect(await screen.findByText("Breaker checks are not running on this server.")).toBeInTheDocument();
  });

  it("explains how to set breakers when there are none", async () => {
    renderSafety(<BreakersPanel />, fixture("empty"));
    expect(await screen.findByText("No circuit breakers are set. Add a breakers: section to your organization config to stop work automatically when spend or bad behaviour passes a limit.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows skeleton rows with a status text while loading", async () => {
    const api = createFixtureClient();
    api.breakers.list = () => deferred<BreakersView>().promise;
    renderSafety(<BreakersPanel />, api);
    const region = await panel();
    expect(within(region).getByRole("status")).toHaveTextContent("Loading breakers");
  });

  it("says it could not load, with Try again", async () => {
    const user = userEvent.setup();
    const api = createUnavailableClient();
    const list = vi.spyOn(api.breakers, "list");
    renderSafety(<BreakersPanel />, api);
    const region = await panel();
    expect(await within(region).findByText("Could not load the circuit breakers.")).toBeInTheDocument();
    const calls = list.mock.calls.length;
    await user.click(within(region).getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(list.mock.calls.length).toBeGreaterThan(calls);
    });
  });

  it("keeps the last good table when a later read fails, and says it may be out of date", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = createFixtureClient();
    const real = api.breakers.list.bind(api.breakers);
    let fail = false;
    api.breakers.list = () => (fail ? Promise.reject(new Error("down")) : real());
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    fail = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_100);
    });
    expect(await screen.findByText(/Breakers may be out of date \(last read /)).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("reads every 10 seconds and shows a trip that happened meanwhile", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = createFixtureClient();
    const real = api.breakers.list.bind(api.breakers);
    let tripped = false;
    api.breakers.list = async () => {
      const view = await real();
      return tripped ? { ...view, breakers: view.breakers.map((b) => (b.name === "denied-loop" ? { ...b, state: "tripped" as const, observed: "21", observedAs: "at_trip" as const, trippedAt: "2026-11-01T11:58:00+00:00", trippedBy: "service:breaker" } : b)) } : view;
    };
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    tripped = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_100);
    });
    expect(await screen.findByText("21 of 20 events at trip")).toBeInTheDocument();
  });
});

describe("reset dialog", () => {
  async function open(scenario: Parameters<typeof fixture>[0] = "tripped", api = fixture(scenario)) {
    const user = userEvent.setup();
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    const opener = await screen.findByRole("button", { name: "Reset breaker denied-loop" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Reset breaker denied-loop?" });
    return { user, api, opener, dialog };
  }

  it("says the count starts afresh and the organization stays halted, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("The count starts afresh from now. The organization stays halted until you resume it.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("leaves out the halted sentence when the organization is running", async () => {
    const api = edited("tripped", (v) => ({ ...v, halt: { ...v.halt, halted: false } }));
    const { dialog } = await open("tripped", api);
    expect(within(dialog).getByText("The count starts afresh from now.")).toBeInTheDocument();
  });

  it("says a paused role stays paused for a pause_role rule", async () => {
    const api = edited("tripped", (v) => ({
      ...v,
      halt: { ...v.halt, halted: false, roles: v.halt.roles.map((r) => (r.role === "writer" ? { ...r, paused: true } : r)) },
      breakers: v.breakers.map((b) => (b.name === "denied-loop" ? { ...b, action: "pause_role" as const, role: "writer" } : b)),
    }));
    const { dialog } = await open("tripped", api);
    expect(within(dialog).getByText("The count starts afresh from now. The role stays paused until you resume it.")).toBeInTheDocument();
  });

  it("resets once, and the row goes back to OK", async () => {
    const api = fixture("tripped");
    const reset = vi.spyOn(api.breakers, "reset");
    const { user, dialog } = await open("tripped", api);
    await user.click(within(dialog).getByRole("button", { name: "Reset breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(reset).toHaveBeenCalledTimes(1);
    expect(reset).toHaveBeenCalledWith("denied-loop");
    const row = await screen.findByRole("row", { name: /denied-loop/ });
    expect(within(row).getByText("OK")).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("Breaker denied-loop was reset.");
  });

  it("on a 409 not_tripped closes with 'was already reset' and refetches", async () => {
    const api = fixture("tripped");
    api.breakers.reset = () => Promise.reject(new ConflictError("not tripped", "not_tripped"));
    const { user, dialog } = await open("tripped", api);
    await user.click(within(dialog).getByRole("button", { name: "Reset breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Breaker denied-loop was already reset.");
  });

  it("stays open with the problem for anything else", async () => {
    const api = fixture("tripped");
    api.breakers.reset = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open("tripped", api);
    await user.click(within(dialog).getByRole("button", { name: "Reset breaker" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not reset: Only a signed-in person can do this");
  });

  it("Escape cancels, nothing is reset, focus returns to the opener", async () => {
    const api = fixture("tripped");
    const reset = vi.spyOn(api.breakers, "reset");
    const { user, opener } = await open("tripped", api);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(reset).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  });
});

describe("disable dialog", () => {
  async function open(api = fixture("running")) {
    const user = userEvent.setup();
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    const opener = await screen.findByRole("button", { name: "Disable breaker spend-spike" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Disable breaker spend-spike?" });
    return { user, api, opener, dialog };
  }

  it("says what disabling does and does not do, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("This rule stops being checked and cannot trip until a person enables it. Nothing is stopped or resumed by this.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("needs a reason: no default, the four labels, the button stays off until one is chosen", async () => {
    const { user, dialog } = await open();
    const select = within(dialog).getByRole("combobox", { name: "Reason" });
    expect(select).toHaveValue("");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Choose a reason", "False alarm", "Planned work", "Rule is being replaced", "Other"]);
    const confirm = within(dialog).getByRole("button", { name: "Disable breaker" });
    expect(confirm).toBeDisabled();
    await user.selectOptions(select, "Rule is being replaced");
    expect(confirm).toBeEnabled();
  });

  it("disables once with the chosen code; shows Disabling... while sending", async () => {
    const api = fixture("running");
    const real = api.breakers.disable.bind(api.breakers);
    const gate = deferred<undefined>();
    const disable = vi.fn(async (name: string, reason: Parameters<typeof real>[1]) => {
      await gate.promise;
      return real(name, reason);
    });
    api.breakers.disable = disable;
    const { user, dialog } = await open(api);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Reason" }), "Planned work");
    await user.click(within(dialog).getByRole("button", { name: "Disable breaker" }));
    expect(await within(dialog).findByRole("button", { name: "Disabling..." })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    gate.resolve(undefined);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(disable).toHaveBeenCalledTimes(1);
    expect(disable).toHaveBeenCalledWith("spend-spike", "planned_work");
    const row = await screen.findByRole("row", { name: /spend-spike/ });
    expect(within(row).getByText("Disabled", { selector: ".chip" })).toBeInTheDocument();
    expect(within(row).getByText(/: Planned work$/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Enable breaker spend-spike" })).toHaveFocus();
  });

  it("on 409 already_disabled closes with a message and refetches", async () => {
    const api = fixture("running");
    api.breakers.disable = () => Promise.reject(new ConflictError("already", "already_disabled"));
    const { user, dialog } = await open(api);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Reason" }), "Other");
    await user.click(within(dialog).getByRole("button", { name: "Disable breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Breaker spend-spike was already disabled.");
  });

  it("on 409 breaker_tripped closes and says to reset it first", async () => {
    const api = fixture("running");
    api.breakers.disable = () => Promise.reject(new ConflictError("tripped", "breaker_tripped"));
    const { user, dialog } = await open(api);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Reason" }), "False alarm");
    await user.click(within(dialog).getByRole("button", { name: "Disable breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Breaker spend-spike is tripped. Reset it first.");
  });

  it("stays open with the problem for anything else", async () => {
    const api = fixture("running");
    api.breakers.disable = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open(api);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Reason" }), "Other");
    await user.click(within(dialog).getByRole("button", { name: "Disable breaker" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not disable: Only a signed-in person can do this");
  });

  it("Escape cancels and nothing is disabled", async () => {
    const api = fixture("running");
    const disable = vi.spyOn(api.breakers, "disable");
    const { user, opener } = await open(api);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(disable).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  });

  it("forgets the chosen reason when it is opened again", async () => {
    const { user, dialog } = await open();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Reason" }), "Other");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(await screen.findByRole("button", { name: "Disable breaker spend-spike" }));
    expect(within(await screen.findByRole("dialog")).getByRole("combobox", { name: "Reason" })).toHaveValue("");
  });
});

describe("enable dialog", () => {
  async function open(api = fixture("disabled")) {
    const user = userEvent.setup();
    renderSafety(<BreakersPanel />, api);
    await screen.findByRole("table");
    const opener = await screen.findByRole("button", { name: "Enable breaker watch-gates" });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Enable breaker watch-gates?" });
    return { user, api, opener, dialog };
  }

  it("says the rule is checked again with a fresh count, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("The rule is checked again and its count starts afresh from now.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("enables once and the row is OK again", async () => {
    const api = fixture("disabled");
    const enable = vi.spyOn(api.breakers, "enable");
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Enable breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(enable).toHaveBeenCalledTimes(1);
    expect(enable).toHaveBeenCalledWith("watch-gates");
    const row = await screen.findByRole("row", { name: /watch-gates/ });
    expect(within(row).getByText("OK")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Disable breaker watch-gates" })).toHaveFocus();
  });

  it("on 409 not_disabled closes with 'was already enabled'", async () => {
    const api = fixture("disabled");
    api.breakers.enable = () => Promise.reject(new ConflictError("not disabled", "not_disabled"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Enable breaker" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Breaker watch-gates was already enabled.");
  });

  it("stays open with the problem for anything else", async () => {
    const api = fixture("disabled");
    api.breakers.enable = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Enable breaker" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not enable: Only a signed-in person can do this");
  });
});
