import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import type { AuthApi, AuthState } from "@/auth/types";
import { renderAt, serverLike } from "@/test/server-like";

import { COPY } from "./copy";
import { firstRunKey } from "./storage";

const ORG = "org-one";
const USER = "user-one";

function authOf(role: string): AuthApi {
  const state: AuthState = {
    kind: "signed-in",
    user: { id: USER, orgId: ORG, role: role as "owner", email: "owner@example.test", displayName: "Owner" },
  };
  return {
    state: () => state,
    subscribe: () => () => undefined,
    load: () => Promise.resolve(),
    login: () => Promise.resolve({ kind: "signed-in" as const }),
    completeMfa: () => Promise.resolve(),
    logout: () => Promise.resolve(),
    setupStatus: () => Promise.resolve(false),
    redeemSetup: () => Promise.resolve(),
  };
}

interface Reads {
  runs?: () => Promise<unknown>;
  approvals?: () => Promise<unknown>;
  invites?: () => Promise<unknown>;
}

function clientWith(reads: Reads = {}, role = "owner", source: "server" | "fixtures" = "server") {
  const calls = { runs: 0, approvals: 0, invites: 0 };
  const read = (name: keyof Reads, fallback: unknown) => () => {
    calls[name] += 1;
    return (reads[name] ?? (() => Promise.resolve(fallback)))();
  };
  const base = serverLike({
    firstRun: {
      runs: read("runs", { runs: [] }),
      approvals: read("approvals", { items: [], next_cursor: null }),
      invites: read("invites", { invites: [], next_cursor: null }),
    },
  });
  const api = { ...base, source, auth: authOf(role) } as ApiClient;
  return { api, calls };
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("/start page", () => {
  it("shows the four steps to a new owner, with only the owner step done, and counts them in the sidebar", async () => {
    const { api } = clientWith();
    renderAt("/start", api);
    expect(await screen.findByRole("heading", { level: 1, name: "Get started" })).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Setup steps" })).getAllByRole("listitem");
    expect(steps.map((s) => s.getAttribute("data-step"))).toEqual(["owner", "run", "approve", "invite"]);
    expect(steps[0]).toHaveTextContent("Done");
    expect(steps[1]).toHaveTextContent("Not done");
    const link = await screen.findByRole("link", { name: /Get started.*1 of 4/ });
    expect(link).toHaveAttribute("href", "/start");
  });

  it("marks a step done only from the server's answer, and the sidebar follows", async () => {
    const { api } = clientWith({
      runs: () => Promise.resolve({ runs: [{ id: "r1" }] }),
      approvals: () => Promise.resolve({ items: [{ status: "approved" }], next_cursor: null }),
    });
    renderAt("/start", api);
    await screen.findByRole("heading", { name: "Get started" });
    await waitFor(() => {
      expect(screen.getByTestId("step-run")).toHaveTextContent("Done");
    });
    expect(screen.getByTestId("step-approve")).toHaveTextContent("Done");
    expect(screen.getByTestId("step-invite")).toHaveTextContent("Not done");
    expect(await screen.findByRole("link", { name: /Get started.*3 of 4/ })).toBeInTheDocument();
  });

  it("shows no checklist and makes no request for a person who is not an owner", async () => {
    for (const role of ["approver", "viewer", "admin", "builder"]) {
      const { api, calls } = clientWith({}, role);
      renderAt("/start", api);
      expect(await screen.findByText("Nothing left to set up")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Open the Builder" })).toHaveAttribute("href", "/builder");
      expect(screen.queryByTestId("step-run")).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /Get started/ })).not.toBeInTheDocument();
      expect(calls).toEqual({ runs: 0, approvals: 0, invites: 0 });
      cleanup();
    }
  });

  it("makes no request on the sample source", async () => {
    const { api, calls } = clientWith({}, "owner", "fixtures");
    renderAt("/start", api);
    expect(await screen.findByText("Nothing left to set up")).toBeInTheDocument();
    expect(calls).toEqual({ runs: 0, approvals: 0, invites: 0 });
  });

  it("says a step could not be checked, never marks it done, and retries on request", async () => {
    let fail = true;
    const { api } = clientWith({
      runs: () => (fail ? Promise.reject(new Error("boom")) : Promise.resolve({ runs: [{}] })),
    });
    renderAt("/start", api);
    const step = await screen.findByTestId("step-run");
    await waitFor(() => {
      expect(step).toHaveTextContent("Could not check. Try again.");
    });
    expect(step).not.toHaveTextContent("Done");
    fail = false;
    await userEvent.click(within(step).getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(step).toHaveTextContent("Done");
    });
  });

  it("treats a body of an unexpected shape as unknown", async () => {
    const { api } = clientWith({ approvals: () => Promise.resolve({ nope: 1 }) });
    renderAt("/start", api);
    await waitFor(() => {
      expect(screen.getByTestId("step-approve")).toHaveTextContent("Could not check. Try again.");
    });
  });

  it("points the approve step at the Approvals page and says nothing about a cause when none was asked", async () => {
    const { api } = clientWith({ runs: () => Promise.resolve({ runs: [{}] }) });
    renderAt("/start", api);
    const step = await screen.findByTestId("step-approve");
    expect(within(step).getByRole("link", { name: "Open approvals" })).toHaveAttribute("href", "/approvals");
    await waitFor(() => {
      expect(step).toHaveTextContent("No approval has been asked yet");
    });
  });

  it("offers the command line, not a demo button or any claim about cost, and no invite link yet", async () => {
    const { api } = clientWith();
    renderAt("/start", api);
    const run = await screen.findByTestId("step-run");
    expect(within(run).getByText("nuramesh try my-team --fake")).toBeInTheDocument();
    // `nuramesh try` needs a team folder: the shown line must carry one before the flags.
    expect(COPY.cliLine).toMatch(/^nuramesh try [^\s-]\S* --fake$/);
    expect(screen.queryByRole("button", { name: "Start the demo run" })).not.toBeInTheDocument();
    const invite = screen.getByTestId("step-invite");
    expect(invite).toHaveTextContent("Invites arrive with the next release");
    expect(within(invite).queryByRole("link")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\bfree\b|fake model/i);
  });

  it("skips only the invite step; skipped counts for completion and the page says You are set up", async () => {
    const { api } = clientWith({
      runs: () => Promise.resolve({ runs: [{}] }),
      approvals: () => Promise.resolve({ items: [{ status: "denied" }], next_cursor: null }),
    });
    renderAt("/start", api);
    const invite = await screen.findByTestId("step-invite");
    expect(within(screen.getByTestId("step-run")).queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
    await userEvent.click(within(invite).getByRole("button", { name: "Skip for now" }));
    expect(await screen.findByText("You are set up")).toBeInTheDocument();
    const next = within(screen.getByRole("navigation", { name: "Next" }));
    for (const name of ["Ledger", "Builder", "Approvals"]) {
      expect(next.getByRole("link", { name })).toBeInTheDocument();
    }
    expect(JSON.parse(localStorage.getItem(firstRunKey(ORG, USER)) ?? "null")).toEqual({
      v: 1,
      dismissed: false,
      invite_skipped: true,
    });
    expect(screen.queryByRole("link", { name: /Get started/ })).not.toBeInTheDocument();
  });

  it("hides the checklist on request: no sidebar entry, the page still opens, Settings brings it back", async () => {
    const { api } = clientWith();
    renderAt("/start", api);
    await userEvent.click(await screen.findByRole("button", { name: "Hide this checklist" }));
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: /Get started/ })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: "Get started" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(firstRunKey(ORG, USER)) ?? "null")).toMatchObject({ dismissed: true });
  });

  it("announces a step that becomes done in a polite live region", async () => {
    let runs: unknown = { runs: [] };
    const { api } = clientWith({ runs: () => Promise.resolve(runs) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt("/start", api);
    await screen.findByTestId("step-run");
    const live = screen.getByTestId("first-run-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    runs = { runs: [{}] };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5100);
    });
    await waitFor(() => {
      expect(live).toHaveTextContent("Start the demo run: done");
    });
  });

  it("re-reads the three answers every 5 seconds while the page shows", async () => {
    const { api, calls } = clientWith();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt("/start", api);
    await screen.findByTestId("step-run");
    const before = { ...calls };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5100);
    });
    expect(calls.runs).toBeGreaterThan(before.runs);
    expect(calls.approvals).toBeGreaterThan(before.approvals);
    expect(calls.invites).toBeGreaterThan(before.invites);
  });
});

describe("index redirect", () => {
  it("goes to /start while the checklist is active", async () => {
    const { api } = clientWith();
    const router = renderAt("/", api);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/start");
    });
  });

  it("goes to /builder once the person dismissed it", async () => {
    localStorage.setItem(firstRunKey(ORG, USER), '{"v":1,"dismissed":true,"invite_skipped":false}');
    const { api } = clientWith();
    const router = renderAt("/", api);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/builder");
    });
  });

  it("goes to /builder when the checklist is complete, and a person who is not an owner skips the checklist", async () => {
    const done = clientWith({
      runs: () => Promise.resolve({ runs: [{}] }),
      approvals: () => Promise.resolve({ items: [{ status: "approved" }], next_cursor: null }),
      invites: () => Promise.resolve({ invites: [{}], next_cursor: null }),
    });
    const a = renderAt("/", done.api);
    await waitFor(() => {
      expect(a.state.location.pathname).toBe("/builder");
    });
    const other = clientWith({}, "approver");
    const b = renderAt("/", other.api);
    // An approver is not offered the checklist; the index sends them to their role's landing page (AE-1).
    await waitFor(() => {
      expect(b.state.location.pathname).toBe("/approvals");
    });
    expect(other.calls).toEqual({ runs: 0, approvals: 0, invites: 0 });
  });
});

describe("Settings link", () => {
  it("shows 'Show the setup checklist' only while dismissed, and clears the flag", async () => {
    localStorage.setItem(firstRunKey(ORG, USER), '{"v":1,"dismissed":true,"invite_skipped":false}');
    const { api } = clientWith();
    renderAt("/settings", api);
    await userEvent.click(await screen.findByRole("button", { name: "Show the setup checklist" }));
    expect(JSON.parse(localStorage.getItem(firstRunKey(ORG, USER)) ?? "null")).toMatchObject({ dismissed: false });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Show the setup checklist" })).not.toBeInTheDocument();
    });
    expect(await screen.findByRole("link", { name: /Get started/ })).toBeInTheDocument();
  });

  it("is absent when the checklist was not dismissed", async () => {
    const { api } = clientWith();
    renderAt("/settings", api);
    await screen.findByRole("heading", { level: 1, name: "Settings" });
    expect(screen.queryByRole("button", { name: "Show the setup checklist" })).not.toBeInTheDocument();
  });
});

describe("C34: the checklist reads, it never decides", () => {
  it("imports no ledger, approval-answer, policy or budget-write call", () => {
    const dir = __dirname;
    const files = readdirSync(dir).filter((f) => /\.tsx?$/.test(f) && !f.includes(".test."));
    expect(files.length).toBeGreaterThan(4);
    for (const f of files) {
      const text = readFileSync(join(dir, f), "utf8");
      expect(text, f).not.toMatch(/\.(ledger|policyPresets|budgets|spend|org|breakers)\.|approvals\.(decide|approve|deny)/);
    }
  });
});
