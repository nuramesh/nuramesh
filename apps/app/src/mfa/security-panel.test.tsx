/* eslint-disable @typescript-eslint/no-non-null-assertion -- test file: fixed arrays and elements that the line above has just checked exist */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { createServerAuth } from "@/auth/server-auth";
import type { TabSync } from "@/auth/tab-sync";
import { renderAt } from "@/test/server-like";

import {
  CODES,
  codesAnswer,
  enrollAnswer,
  enrolled,
  fakeFetch,
  type Handler,
  type Reply,
  NO_STORE,
  noLock,
  PASSWORD,
  problem,
  SECRET,
  session,
  status,
  URI,
  USER,
} from "./mfa-test-kit";
import { CODES_UNSAVED_KEY } from "./notice";

type Routes = Record<string, Handler | Reply[]>;

function fakeSync() {
  let listener: (() => void) | undefined;
  const sync: TabSync = { notify: vi.fn(), listen: (fn) => { listener = fn; return () => { listener = undefined; }; } };
  return { sync, fire: () => { listener?.(); } };
}

function open(routes: Routes, path = "/settings") {
  const server = fakeFetch({ "GET /v1/auth/session": [session()], ...routes });
  const tabs = fakeSync();
  const auth = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, sync: tabs.sync });
  const base = createFixtureClient({ signIn: { account: { email: "ada@example.org", password: PASSWORD, role: "owner" }, signedIn: true } });
  const router = renderAt(path, { ...base, auth });
  return { ...server, auth, tabs, router, user: userEvent.setup() };
}

const panel = async () => within(await screen.findByTestId("mfa-security"));
const MFA = "GET /v1/auth/mfa";
const ENROLL = "POST /v1/auth/mfa/enroll";
const CONFIRM = "POST /v1/auth/mfa/enroll/confirm";
const REGEN = "POST /v1/auth/mfa/recovery-codes";
const DISABLE = "POST /v1/auth/mfa/disable";

async function toS4(ctx: ReturnType<typeof open>) {
  const p = await panel();
  await ctx.user.click(await p.findByRole("button", { name: "Set up" }));
  await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
  await ctx.user.click(p.getByRole("button", { name: "Start setup" }));
  await p.findByTestId("mfa-setup");
  return p;
}
async function toS5(ctx: ReturnType<typeof open>) {
  const p = await toS4(ctx);
  await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
  await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
  await p.findByTestId("mfa-recovery-codes");
  return p;
}
async function regenerateForm(ctx: ReturnType<typeof open>, code = "123456") {
  const p = await panel();
  await ctx.user.click(await p.findByRole("button", { name: "Regenerate recovery codes" }));
  await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
  await ctx.user.type(p.getByLabelText("6-digit code from your app"), code);
  return p;
}

beforeEach(() => {
  globalThis.sessionStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  globalThis.sessionStorage.clear();
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

describe("M14: what Settings > Security shows", () => {
  it("available:false and not enrolled: 'not set up', no buttons", async () => {
    open({ [MFA]: [status({ available: false })] });
    const p = await panel();
    expect(await p.findByText("Not set up on this server. Ask the operator.")).toBeInTheDocument();
    expect(p.queryByRole("button")).not.toBeInTheDocument();
  });
  it("available:false but enrolled: the lost-key text, the remaining codes, no regenerate or turn-off", async () => {
    open({ [MFA]: [enrolled({ available: false, recovery_codes_remaining: 7 })] });
    const p = await panel();
    expect(await p.findByText(/Codes can't be checked on this server right now; your recovery codes still work/)).toBeInTheDocument();
    expect(p.getByTestId("mfa-remaining")).toHaveTextContent("Recovery codes left: 7");
    expect(p.queryByRole("button", { name: "Regenerate recovery codes" })).not.toBeInTheDocument();
    expect(p.queryByRole("button", { name: "Turn off" })).not.toBeInTheDocument();
  });
  it("not enrolled offers Set up; enrolled with 2 codes left warns", async () => {
    open({ [MFA]: [status()] });
    expect(await (await panel()).findByRole("button", { name: "Set up" })).toBeEnabled();
  });
  it("enrolled with 3 or fewer codes left warns", async () => {
    open({ [MFA]: [enrolled({ recovery_codes_remaining: 2 })] });
    const p = await panel();
    expect(await p.findByTestId("mfa-remaining")).toHaveTextContent("Make new ones before you run out.");
  });
  it("an owner under the policy cannot turn off: disabled with the reason; a forced 409 shows the policy text", async () => {
    open({ [MFA]: [enrolled({ required: true })] });
    const p = await panel();
    const off = await p.findByRole("button", { name: "Turn off" });
    expect(off).toBeDisabled();
    expect(p.getByText("Your organization requires it for owners.")).toBeInTheDocument();
  });
  it("409 mfa_required_by_policy on turn off shows the policy text and keeps the form", async () => {
    const ctx = open({ [MFA]: [enrolled()], [DISABLE]: [problem(409, "mfa_required_by_policy")] });
    const p = await panel();
    await ctx.user.click(await p.findByRole("button", { name: "Turn off" }));
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Turn off MFA" }));
    expect(await p.findByTestId("mfa-error")).toHaveTextContent("Your organization requires MFA for owners");
    expect(p.getByTestId("mfa-form-disable")).toBeInTheDocument();
  });
});

describe("set up: S3 to S4 to S5 (M9 for Settings, M10, M16, M31, M46)", () => {
  it("shows the QR and the key behind a click, then ten codes; Continue needs the tick; nothing is left behind", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS4(ctx);
    expect(ctx.calls.find((c) => c.path === "/v1/auth/mfa/enroll")?.body).toEqual({ password: PASSWORD });
    expect(await p.findByTestId("mfa-qr")).toBeInTheDocument();
    expect(p.queryByTestId("mfa-key-text")).not.toBeInTheDocument();
    expect(ctx.auth.held()).toBe(true);
    await ctx.user.click(p.getByRole("button", { name: "Can't scan? Show the key" }));
    expect(p.getByTestId("mfa-key-text")).toHaveTextContent(SECRET);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "12 34 56");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    const s5 = await p.findByTestId("mfa-recovery-codes");
    expect(ctx.calls.find((c) => c.path.endsWith("/confirm"))?.body).toEqual({ factor_id: "mf_" + "0".repeat(20), code: "123456" });
    expect(within(s5).getAllByRole("listitem").map((li) => li.textContent)).toEqual(CODES);
    expect(within(s5).getByText("Other devices were signed out.")).toBeInTheDocument();
    expect(within(s5).getByText(/Do not show this screen on a shared or recorded screen/)).toBeInTheDocument();
    expect(within(s5).getByText("Delete the downloaded file after you import it.")).toBeInTheDocument();
    expect(p.queryByTestId("mfa-qr")).not.toBeInTheDocument();
    const next = within(s5).getByRole("button", { name: "Continue" });
    expect(next).toBeDisabled();
    await ctx.user.click(within(s5).getByLabelText("I saved these codes"));
    expect(next).toBeEnabled();
    await ctx.user.click(next);
    await waitFor(() => { expect(screen.queryByTestId("mfa-recovery-codes")).not.toBeInTheDocument(); });
    for (const code of CODES) expect(document.body.innerHTML).not.toContain(code);
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(ctx.auth.held()).toBe(false);
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBeNull();
  });

  it("M31: mounting makes no enrol call; a double click sends one request", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()] });
    const p = await panel();
    await p.findByRole("button", { name: "Set up" });
    expect(ctx.count(ENROLL)).toBe(0);
    await ctx.user.click(p.getByRole("button", { name: "Set up" }));
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.dblClick(p.getByRole("button", { name: "Start setup" }));
    await p.findByTestId("mfa-setup");
    expect(ctx.count(ENROLL)).toBe(1);
  });

  it("M46: autocomplete attributes keep codes out of autofill history", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()] });
    const p = await toS4(ctx);
    expect(p.getByLabelText("6-digit code from your app")).toHaveAttribute("autocomplete", "one-time-code");
  });

  it("M46: the password field is current-password and the code field one-time-code on regenerate", async () => {
    const ctx = open({ [MFA]: [enrolled()] });
    const p = await regenerateForm(ctx);
    expect(p.getByLabelText("Your password")).toHaveAttribute("autocomplete", "current-password");
    expect(p.getByLabelText("6-digit code from your app")).toHaveAttribute("autocomplete", "one-time-code");
  });

  it("M15: a wrong password shows the password-or-SSO text and clears the field; reauth_required on confirm returns to the password step", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [problem(401, "invalid_credentials"), enrollAnswer()], [CONFIRM]: [problem(409, "reauth_required")] });
    const p = await panel();
    await ctx.user.click(await p.findByRole("button", { name: "Set up" }));
    await ctx.user.type(p.getByLabelText("Your password"), "obviously-wrong-password");
    await ctx.user.click(p.getByRole("button", { name: "Start setup" }));
    expect(await p.findByTestId("mfa-error")).toHaveTextContent(/MFA does not apply to your account here/);
    expect(p.getByLabelText("Your password")).toHaveValue("");
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.click(p.getByRole("button", { name: "Start setup" }));
    await p.findByTestId("mfa-setup");
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    expect(await p.findByText("For your security, enter your password again.")).toBeInTheDocument();
    expect(p.getByLabelText("Your password")).toHaveValue("");
    expect(p.queryByTestId("mfa-setup")).not.toBeInTheDocument();
    expect(ctx.auth.held()).toBe(false);
  });

  it("M45: three wrong passwords shut the form for 60 s; the fourth request is not sent before", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [problem(401, "invalid_credentials")] });
    const p = await panel();
    await ctx.user.click(await p.findByRole("button", { name: "Set up" }));
    for (let i = 0; i < 3; i += 1) {
      await ctx.user.type(p.getByLabelText("Your password"), "obviously-wrong-password");
      await ctx.user.click(p.getByRole("button", { name: "Start setup" }));
      await waitFor(() => { expect(ctx.count(ENROLL)).toBe(i + 1); });
      await waitFor(() => { expect(p.getByLabelText("Your password")).toHaveValue(""); });
    }
    await waitFor(() => { expect(p.getByLabelText("Your password")).toBeDisabled(); });
    expect(p.getByRole("button", { name: "Start setup" })).toBeDisabled();
    expect(ctx.count(ENROLL)).toBe(3);
  });
});

describe("M30: S4 and enrol error map", () => {
  const cases: [string, Reply, RegExp][] = [
    ["401 mfa_invalid", problem(401, "mfa_invalid"), /^That code is not right\.$/],
    ["429", { status: 429, body: { title: "too_many_attempts", status: 429, detail: "x" }, headers: { "retry-after": "30" } }, /Too many tries/],
    ["503 mfa_unavailable", problem(503, "mfa_unavailable"), /Ask the operator/],
    ["503 busy", problem(503, "busy"), /server is busy/],
  ];
  it.each(cases)("confirm: %s gives one fixed text and stays on S4", async (_name, reply, text) => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [reply] });
    const p = await toS4(ctx);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    expect(await p.findByTestId("mfa-error")).toHaveTextContent(text);
    expect(p.getByTestId("mfa-setup")).toBeInTheDocument();
    expect(p.queryByTestId("mfa-recovery-codes")).not.toBeInTheDocument();
    expect(p.getByTestId("mfa-error").textContent).not.toMatch(/\d+ (tries|attempts)/);
  });
  it("a 429 on confirm disables the field with a countdown", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [{ status: 429, body: { title: "too_many_attempts", status: 429, detail: "x" }, headers: { "retry-after": "30" } }] });
    const p = await toS4(ctx);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    await waitFor(() => { expect(p.getByLabelText("6-digit code from your app")).toBeDisabled(); });
  });
  it("409 no_pending_factor clears S4 and goes back to the start", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(409, "no_pending_factor")] });
    const p = await toS4(ctx);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    expect(await p.findByText("Setup expired. Start again.")).toBeInTheDocument();
    expect(p.queryByTestId("mfa-setup")).not.toBeInTheDocument();
    expect(p.getByTestId("mfa-form-enrol")).toBeInTheDocument();
  });
  it("409 mfa_already_enrolled refreshes the status", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(409, "mfa_already_enrolled")] });
    const p = await toS4(ctx);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    expect(await p.findByText("MFA is already set up for this account.")).toBeInTheDocument();
    await p.findByRole("button", { name: "Regenerate recovery codes" });
  });
  it("enrol: 409 mfa_already_enrolled, 503 mfa_unavailable and 503 busy each show their fixed text", async () => {
    for (const [reply, text] of [
      [problem(409, "mfa_already_enrolled"), /already set up/],
      [problem(503, "mfa_unavailable"), /Ask the operator/],
      [problem(503, "busy"), /server is busy/],
    ] as const) {
      const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [reply] });
      const p = await panel();
      await ctx.user.click(await p.findByRole("button", { name: "Set up" }));
      await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
      await ctx.user.click(p.getByRole("button", { name: "Start setup" }));
      expect((await p.findAllByText(text)).length).toBeGreaterThan(0);
      expect(p.queryByTestId("mfa-setup")).not.toBeInTheDocument();
      expect(ctx.count(ENROLL)).toBe(1);
      document.body.innerHTML = "";
    }
  });
});

describe("regenerate and turn off (M32, M33, M16)", () => {
  it("M33: regenerate with the password and a new code shows ten new codes through S5", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [codesAnswer()] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    const s5 = await p.findByTestId("mfa-recovery-codes");
    expect(within(s5).getAllByRole("listitem")).toHaveLength(10);
    expect(ctx.calls.find((c) => c.path.endsWith("recovery-codes"))?.body).toEqual({ password: PASSWORD, code: "123456" });
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });

  it("M32: a replayed code (401 mfa_invalid) shows the replay text and keeps the form, on regenerate and on turn off", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [problem(401, "mfa_invalid")], [DISABLE]: [problem(401, "mfa_invalid")] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    expect(await p.findByTestId("mfa-error")).toHaveTextContent("That code is not right, or it was already used. Wait for the next code, then try again.");
    expect(p.getByTestId("mfa-form-regenerate")).toBeInTheDocument();
    await ctx.user.click(p.getByRole("button", { name: "Cancel" }));
    await ctx.user.click(p.getByRole("button", { name: "Turn off" }));
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Turn off MFA" }));
    expect(await p.findByTestId("mfa-error")).toHaveTextContent("it was already used");
    expect(p.getByTestId("mfa-form-disable")).toBeInTheDocument();
  });

  it("M16: turning off says other devices were signed out and shows the off state; the session still works", async () => {
    const ctx = open({ [MFA]: [enrolled(), status()], [DISABLE]: [{ headers: NO_STORE, body: { enrolled: false } }] });
    const p = await panel();
    await ctx.user.click(await p.findByRole("button", { name: "Turn off" }));
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Turn off MFA" }));
    await waitFor(() => { expect(p.getByTestId("mfa-info")).toHaveTextContent("MFA is off. Other devices were signed out."); });
    await p.findByRole("button", { name: "Set up" });
    expect(ctx.auth.state().kind).toBe("signed-in");
    expect(ctx.calls.find((c) => c.path.endsWith("/disable"))?.body).toEqual({ password: PASSWORD, code: "123456" });
  });

  it("a bad code shape is never sent", async () => {
    const ctx = open({ [MFA]: [enrolled()] });
    const p = await regenerateForm(ctx, "12345a");
    expect(p.getByRole("button", { name: "Make new codes" })).toBeDisabled();
    expect(ctx.count(REGEN)).toBe(0);
  });
});

describe("M13: an unsafe answer", () => {
  it("regenerate answered without no-store: nothing shown, the text says the setup may have changed, the button stays disabled, the notice flag is set", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [{ body: { recovery_codes: CODES }, headers: { "cache-control": "no-cache" } }] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    expect((await p.findAllByText(/This server answered unsafely\. Your setup may have changed/)).length).toBeGreaterThan(0);
    expect(p.queryByTestId("mfa-recovery-codes")).not.toBeInTheDocument();
    for (const code of CODES) expect(document.body.innerHTML).not.toContain(code);
    expect(document.body.textContent).not.toMatch(/nothing was set up/i);
    expect(p.getByRole("button", { name: "Regenerate recovery codes" })).toBeDisabled();
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });
});

describe("M28, M29: clearing the secrets (W-6)", () => {
  it("S4 is emptied after five minutes with the tab hidden; S5 is not, even after 30", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer(), enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS4(ctx);
    vi.useFakeTimers({ shouldAdvanceTime: false });
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    act(() => { vi.advanceTimersByTime(5 * 60 * 1000 + 1000); });
    expect(p.getByText("Setup was interrupted. Start again.")).toBeInTheDocument();
    expect(p.queryByTestId("mfa-qr")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(ctx.auth.held()).toBe(false);
    vi.useRealTimers();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    await ctx.user.click(p.getByRole("button", { name: "Start again" }));
    expect(p.getByTestId("mfa-form-enrol")).toBeInTheDocument();
  });

  it("S5 survives 30 minutes hidden", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS5(ctx);
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    act(() => { vi.advanceTimersByTime(30 * 60 * 1000); });
    expect(p.getByTestId("mfa-code-list")).toBeInTheDocument();
  });

  it("pagehide empties S5 at once and the page then says the codes were not saved, never 'Start again'", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS5(ctx);
    act(() => { globalThis.dispatchEvent(new Event("pagehide")); });
    expect(document.body.innerHTML).not.toContain(CODES[0]!);
    expect(p.getByTestId("mfa-cleared")).toHaveTextContent("MFA is on. Your recovery codes were not saved: regenerate them in Settings > Security.");
    expect(p.queryByText(/Start again/)).not.toBeInTheDocument();
    expect(ctx.auth.held()).toBe(false);
  });

  it("pageshow from the back/forward cache: S4 before a confirm says 'interrupted'; no secret in the DOM", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()] });
    const p = await toS4(ctx);
    act(() => { globalThis.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })); });
    expect(await p.findByText("Setup was interrupted. Start again.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain("otpauth");
  });

  it("an open S5 blocks in-app navigation until confirmed; 'Stay' keeps the codes; after Continue navigation is free; beforeunload is set and removed", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS5(ctx);
    const event = new Event("beforeunload", { cancelable: true });
    globalThis.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await act(async () => { await Promise.resolve(); }); // let the blocker's effect register before the person can navigate
    await act(async () => { await ctx.router.navigate("/runs"); });
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    await ctx.user.click(screen.getByRole("button", { name: "Stay" }));
    expect(ctx.router.state.location.pathname).toBe("/settings");
    expect(p.getByTestId("mfa-code-list")).toBeInTheDocument();
    await ctx.user.click(p.getByLabelText("I saved these codes"));
    const after = new Event("beforeunload", { cancelable: true });
    globalThis.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
    await ctx.user.click(p.getByRole("button", { name: "Continue" }));
    await act(async () => { await ctx.router.navigate("/runs"); });
    expect(ctx.router.state.location.pathname).toBe("/runs");
  });
});

describe("M20b (screen part): other tabs cannot unmount S4 or S5", () => {
  it("a sign-in then a sign-out announced by another tab leaves S5 mounted; after Continue the held event runs once", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const p = await toS5(ctx);
    const loads = ctx.count("GET /v1/auth/session");
    act(() => { ctx.tabs.fire(); ctx.tabs.fire(); });
    expect(ctx.auth.state().kind).toBe("signed-in");
    expect(screen.queryByText("Checking your session…")).not.toBeInTheDocument();
    expect(p.getByTestId("mfa-code-list")).toBeInTheDocument();
    expect(ctx.count("GET /v1/auth/session")).toBe(loads);
    await ctx.user.click(p.getByLabelText("I saved these codes"));
    await ctx.user.click(p.getByRole("button", { name: "Continue" }));
    await waitFor(() => { expect(ctx.count("GET /v1/auth/session")).toBe(loads + 1); });
    expect(ctx.auth.held()).toBe(false);
  });

  it("the Back / Cancel path releases the hold (no stuck hold)", async () => {
    const ctx = open({ [MFA]: [status()], [ENROLL]: [enrollAnswer()] });
    await toS4(ctx);
    expect(ctx.auth.held()).toBe(true);
    act(() => { globalThis.dispatchEvent(new Event("pagehide")); });
    expect(ctx.auth.held()).toBe(false);
  });

  it("refresh refused while S5 is open: the panel stays mounted with the codes (n1)", async () => {
    const ctx = open({
      [MFA]: [status(), enrolled()],
      [ENROLL]: [enrollAnswer()],
      [CONFIRM]: [codesAnswer()],
      "POST /v1/auth/refresh": [problem(401, "session_invalid")],
    });
    const p = await toS5(ctx);
    await act(async () => { await ctx.auth.recover(); });
    expect(ctx.auth.state().kind).toBe("signed-in");
    expect(p.getByTestId("mfa-code-list")).toBeInTheDocument();
  });
});

describe("M38: the lost-codes notice (W-23)", () => {
  it("the flag exists, holds only '1', before S5 renders; reload during S5 shows the notice once on the first page; Continue clears it", async () => {
    const ctx = open({ [MFA]: [status(), enrolled(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    await toS5(ctx);
    expect(globalThis.sessionStorage.length).toBe(1);
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
    document.body.innerHTML = "";
    // a reload: new page view, same sessionStorage, the session cookie still valid
    open({ [MFA]: [enrolled()] }, "/runs");
    expect(await screen.findByTestId("mfa-lost-notice-page")).toHaveTextContent("MFA is on. Your recovery codes were not saved: regenerate them in Settings > Security.");
  });

  it("S3 shows the same line above Regenerate while the flag is set and MFA is on; the line is not shown when enrolled is false", async () => {
    globalThis.sessionStorage.setItem(CODES_UNSAVED_KEY, "1");
    open({ [MFA]: [enrolled()] });
    expect(await screen.findByTestId("mfa-lost-notice")).toBeInTheDocument();
    document.body.innerHTML = "";
    open({ [MFA]: [status()] }, "/runs");
    await screen.findByTestId("app-shell").catch(() => undefined);
    expect(screen.queryByTestId("mfa-lost-notice-page")).not.toBeInTheDocument();
  });

  it("a failing sessionStorage breaks nothing", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    await toS5(ctx);
  });

  it("M39: confirm answered by a timeout, 502 or 500 with the server's work committed: the re-read runs, no 'nothing happened', one request", async () => {
    for (const reply of [undefined, problem(502, "bad_gateway"), problem(500, "internal")]) {
      const routes: Routes = { [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()] };
      if (reply !== undefined) routes[CONFIRM] = [reply];
      const ctx = open(routes);
      const p = await toS4(ctx);
      await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
      await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
      expect((await p.findAllByText("MFA is on. Your recovery codes were not saved: regenerate them in Settings > Security.")).length).toBeGreaterThan(0);
      expect(ctx.calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(1);
      expect(document.body.textContent).not.toMatch(/nothing happened|nothing was set up/i);
      expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
      document.body.innerHTML = "";
      globalThis.sessionStorage.clear();
    }
  });

  it("M39: an unknown confirm with enrolled:false says 'Setup may not have finished' and clears the flag; a failing re-read says 'We could not tell'", async () => {
    const a = open({ [MFA]: [status(), status()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(500, "internal")] });
    const pa = await toS4(a);
    await a.user.type(pa.getByLabelText("6-digit code from your app"), "123456");
    await a.user.click(pa.getByRole("button", { name: "Confirm" }));
    expect(await pa.findByText("Setup may not have finished. Start again.")).toBeInTheDocument();
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBeNull();
    document.body.innerHTML = "";
    const b = open({ [MFA]: [status(), problem(500, "internal")], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(500, "internal")] });
    const pb = await toS4(b);
    await b.user.type(pb.getByLabelText("6-digit code from your app"), "123456");
    await b.user.click(pb.getByRole("button", { name: "Confirm" }));
    expect(await pb.findByText(/We could not tell whether this worked/)).toBeInTheDocument();
  });

  it("M39: regenerate answered by a 502: the 'could not tell whether new codes were made' text and one request", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [problem(502, "bad_gateway")] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    expect(await p.findByText(/We could not tell whether new codes were made/)).toBeInTheDocument();
    expect(ctx.count(REGEN)).toBe(1);
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });

  it("a change of the signed-in person clears the flag (a different user id announced by another tab)", async () => {
    globalThis.sessionStorage.setItem(CODES_UNSAVED_KEY, "1");
    const other = { ...USER, id: "human:u_ffffffffffffffffffff" };
    const ctx = open({
      [MFA]: [enrolled()],
      "GET /v1/auth/session": [session(), { body: { user: other, session: { id: "s2" }, csrf_token: "csrf-two" } }],
    }, "/runs");
    await screen.findByTestId("mfa-lost-notice-page");
    act(() => { ctx.tabs.fire(); });
    await waitFor(() => { expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBeNull(); });
  });
});

describe("M40: an expired access token on Settings > Security (W-21)", () => {
  it("401 session_invalid: recover() once, renewed -> 'Your session was renewed. Submit again.', fields cleared, nothing resent, never the password text", async () => {
    const ctx = open({
      [MFA]: [enrolled()],
      [REGEN]: [problem(401, "session_invalid"), codesAnswer()],
      "POST /v1/auth/refresh": [{ body: { user: { id: "human:u_0a1b2c3d4e5f60718293", role: "owner" }, csrf_token: "csrf-two" } }],
    });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    await waitFor(() => { expect(p.getByTestId("mfa-info")).toHaveTextContent("Your session was renewed. Submit again."); });
    expect(ctx.count(REGEN)).toBe(1);
    expect(p.queryByText(/password is not right/)).not.toBeInTheDocument();
    expect(p.queryByTestId("mfa-recovery-codes")).not.toBeInTheDocument();
  });

  it("M40 (S4, AU-6): an expired access token while the setup screen is open renews the session, keeps S4 and never says the session ended", async () => {
    const ctx = open({
      [MFA]: [status()],
      [ENROLL]: [enrollAnswer()],
      [CONFIRM]: [problem(401, "session_invalid")],
      "POST /v1/auth/refresh": [{ body: { user: { id: "human:u_0a1b2c3d4e5f60718293", role: "owner" }, csrf_token: "csrf-two" } }],
    });
    const p = await toS4(ctx);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "123456");
    await ctx.user.click(p.getByRole("button", { name: "Confirm" }));
    await waitFor(() => { expect(p.getByTestId("mfa-info")).toHaveTextContent("Your session was renewed. Submit again."); });
    expect(ctx.count("POST /v1/auth/refresh")).toBe(1);
    expect(ctx.count(CONFIRM)).toBe(1);
    expect(p.getByTestId("mfa-setup")).toBeInTheDocument();
    expect(p.queryByText(/session ended/)).not.toBeInTheDocument();
  });

  it("M40 (S4, AU-6): a renewal while held updates only the CSRF token; user and session state do not change", async () => {
    const ctx = open({
      [MFA]: [status()],
      [ENROLL]: [enrollAnswer()],
      "POST /v1/auth/refresh": [{ body: { user: { id: "human:u_other0000000000000000", role: "viewer" }, csrf_token: "csrf-two" } }],
    });
    const p = await toS4(ctx);
    expect(ctx.auth.held()).toBe(true);
    const before = ctx.auth.state();
    expect(await ctx.auth.recover()).toBe("renewed");
    expect(ctx.count("POST /v1/auth/refresh")).toBe(1);
    expect(ctx.auth.state()).toBe(before); // the very same object: nothing was set
    expect(ctx.auth.csrf()).toBe("csrf-two");
    expect(p.getByTestId("mfa-setup")).toBeInTheDocument();
  });

  it("recover() answering 'failed' keeps the fields and resends nothing", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [problem(401, "session_invalid")], "POST /v1/auth/refresh": [problem(503, "busy")] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    expect(await p.findByText("The server could not renew your session. Try again in a moment.")).toBeInTheDocument();
    expect(p.getByLabelText("Your password")).toHaveValue(PASSWORD);
    expect(ctx.count(REGEN)).toBe(1);
  });

  it("any other 401 title never says the password is wrong", async () => {
    const ctx = open({ [MFA]: [enrolled()], [REGEN]: [problem(401, "something_else")] });
    const p = await regenerateForm(ctx);
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    const text = (await p.findByTestId("mfa-error")).textContent;
    expect(text).not.toMatch(/password/i);
  });
});

describe("M8, W-6: nothing secret leaves the page", () => {
  it("storage, URL, history state, cookies and the console hold no code, key, URI or password after setup and regenerate", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const ctx = open({ [MFA]: [status(), enrolled(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()], [REGEN]: [codesAnswer()] });
    const p = await toS5(ctx);
    const inspect = (): void => {
      const seen = [
        JSON.stringify(Object.entries(globalThis.localStorage)),
        JSON.stringify(Object.entries(globalThis.sessionStorage)),
        globalThis.location.href,
        JSON.stringify(globalThis.history.state),
        document.cookie,
        ...spies.flatMap((s) => s.mock.calls.map((c) => JSON.stringify(c))),
      ].join("\n");
      for (const secret of [SECRET, URI, PASSWORD, "123456", ...CODES]) expect(seen).not.toContain(secret);
    };
    inspect();
    await ctx.user.click(p.getByLabelText("I saved these codes"));
    await ctx.user.click(p.getByRole("button", { name: "Continue" }));
    await ctx.user.click(await p.findByRole("button", { name: "Regenerate recovery codes" }));
    await ctx.user.type(p.getByLabelText("Your password"), PASSWORD);
    await ctx.user.type(p.getByLabelText("6-digit code from your app"), "654321");
    await ctx.user.click(p.getByRole("button", { name: "Make new codes" }));
    await p.findByTestId("mfa-recovery-codes");
    inspect();
    expect(globalThis.sessionStorage.length).toBe(1);
  });

  it("copy, download and print do nothing before a click; after the click the file has the codes and a date and no email, org name or key", async () => {
    const ctx = open({ [MFA]: [status(), enrolled()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [codesAnswer()] });
    const write = vi.fn(() => Promise.resolve());
    Object.defineProperty(globalThis.navigator, "clipboard", { value: { writeText: write }, configurable: true });
    const urls: Blob[] = [];
    const create = vi.fn((blob: Blob) => { urls.push(blob); return "blob:fake"; });
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    const print = vi.spyOn(globalThis, "print").mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const p = await toS5(ctx);
    expect(create).not.toHaveBeenCalled();
    expect(write).not.toHaveBeenCalled();
    expect(print).not.toHaveBeenCalled();
    await ctx.user.click(p.getByRole("button", { name: "Download as text" }));
    expect(create).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith("blob:fake");
    expect(click).toHaveBeenCalled();
    const text = await (urls[0]!).text();
    for (const code of CODES) expect(text).toContain(code);
    expect(text).toMatch(/Made: \d{4}-\d{2}-\d{2}/);
    for (const banned of ["ada@example.org", "acme", SECRET, URI]) expect(text).not.toContain(banned);
    await ctx.user.click(p.getByRole("button", { name: "Copy" }));
    expect(write).toHaveBeenCalledWith(CODES.join("\n"));
    await ctx.user.click(p.getByRole("button", { name: "Print" }));
    expect(print).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("mfa-print-block")).not.toBeInTheDocument();
  });
});
