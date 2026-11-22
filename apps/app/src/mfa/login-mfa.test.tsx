/* eslint-disable @typescript-eslint/no-non-null-assertion -- test file: fixed arrays whose elements the test has just shown exist */
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { createServerAuth } from "@/auth/server-auth";
import type { TabSync } from "@/auth/tab-sync";
import { renderAt } from "@/test/server-like";

import {
  CHALLENGE,
  CODES,
  enrollAnswer,
  enrollRequired,
  enrolled,
  fakeFetch,
  type Handler,
  loginConfirmAnswer,
  mfaRequired,
  NO_STORE,
  noLock,
  PASSWORD,
  problem,
  type Reply,
  SECRET,
  session,
  URI,
} from "./mfa-test-kit";
import { CODES_UNSAVED_KEY } from "./notice";
import { recoverySignInPending } from "./recovery-signin";

type Routes = Record<string, Handler | Reply[]>;
interface Live {
  on: boolean;
}

const LOGIN = "POST /v1/auth/login";
const MFA = "POST /v1/auth/login/mfa";
const ENROLL = "POST /v1/auth/login/mfa/enroll";
const CONFIRM = "POST /v1/auth/login/mfa/enroll/confirm";
const SESSION = "GET /v1/auth/session";

/** A storage as a plain object (a `Storage` cannot be spread). */
function dump(storage: Storage): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (let i = 0; i < storage.length; i += 1) {
    const key = storage.key(i);
    if (key !== null) out[key] = storage.getItem(key);
  }
  return out;
}

function fakeSync() {
  let listener: (() => void) | undefined;
  const sync: TabSync = { notify: vi.fn(), listen: (fn) => { listener = fn; return () => { listener = undefined; }; } };
  return { sync, fire: () => { listener?.(); } };
}

/** The server knows nobody until a handler flips `live.on` (the session cookie a real answer sets); then the session exists. */
function open(build: (live: Live) => Routes = () => ({}), path = "/runs") {
  const live: Live = { on: false };
  const server = fakeFetch({
    [SESSION]: () => (live.on ? session() : problem(401, "session_invalid")),
    "POST /v1/auth/refresh": [problem(401, "session_invalid")],
    "GET /v1/auth/mfa": [enrolled({ recovery_codes_remaining: 4 })],
    [LOGIN]: [mfaRequired()],
    ...build(live),
  });
  const tabs = fakeSync();
  const auth = createServerAuth({ baseUrl: "", fetch: server.fetchFn, lock: noLock, sync: tabs.sync });
  const base = createFixtureClient({ signIn: { account: { email: "ada@example.org", password: PASSWORD, role: "owner" }, signedIn: false } });
  const router = renderAt(path, { ...base, auth });
  return { ...server, auth, tabs, router, live, user: userEvent.setup() };
}
type Ctx = ReturnType<typeof open>;

/** A handler that answers `reply` and makes the session exist. */
const signing = (live: Live, reply: Reply): Handler => () => {
  live.on = true;
  return reply;
};

async function password(ctx: Ctx) {
  await ctx.user.type(await screen.findByLabelText("Email"), "ada@example.org");
  await ctx.user.type(screen.getByLabelText("Password"), PASSWORD);
  await ctx.user.click(screen.getByRole("button", { name: "Sign in" }));
}
const codeField = () => screen.findByLabelText("Code from your authenticator app");
const submit = (ctx: Ctx) => ctx.user.click(screen.getByRole("button", { name: "Verify" }));
const sentTo = (ctx: Ctx, path: string) => ctx.calls.filter((c) => c.path === path);
const typeSetupCode = async (ctx: Ctx, code = "123456") => {
  await ctx.user.type(screen.getByLabelText("6-digit code from your app"), code);
  await ctx.user.click(screen.getByRole("button", { name: "Confirm" }));
};

beforeEach(() => {
  globalThis.sessionStorage.clear();
  globalThis.localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  globalThis.sessionStorage.clear();
});

describe("the recovery-sign-in flag and the console", () => {
  it("the enrolment steps (start, confirm, codes) write nothing to console", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });
});

describe("the password step with a second step on the server (W-2, AU-1)", () => {
  it("503 mfa_unavailable: the operator text", async () => {
    const ctx = open(() => ({ [LOGIN]: [problem(503, "mfa_unavailable")] }));
    await password(ctx);
    expect(await screen.findByText("Two-step sign-in is not available on this server. Ask the operator.")).toBeInTheDocument();
  });

  it("503 store_busy: the busy text", async () => {
    const ctx = open(() => ({ [LOGIN]: [problem(503, "store_busy")] }));
    await password(ctx);
    expect(await screen.findByText("The server is busy. Try again in a moment.")).toBeInTheDocument();
  });

  it("M26: a challenge, code or step in the URL is ignored: the password step, nothing prefilled, no request", async () => {
    const ctx = open(() => ({}), "/login?challenge=nmc_00000000000000000000&code=123456&step=mfa#challenge=nmc_1&code=123456");
    expect(await screen.findByLabelText("Password")).toHaveValue("");
    expect(screen.queryByLabelText("6-digit code")).not.toBeInTheDocument();
    expect(ctx.calls.filter((c) => c.method === "POST" && c.path !== "/v1/auth/refresh")).toHaveLength(0); // only the start-up session renewal
  });
});

async function toS2(ctx: Ctx) {
  await password(ctx);
  await screen.findByTestId("mfa-enroll-required");
}
async function toS4(ctx: Ctx) {
  await toS2(ctx);
  await ctx.user.click(screen.getByRole("button", { name: "Set up" }));
  await screen.findByTestId("mfa-setup");
}
async function toS5(ctx: Ctx) {
  await toS4(ctx);
  await typeSetupCode(ctx);
  await screen.findByTestId("mfa-recovery-codes");
}
const enrolRoutes = (live: Live): Routes => ({
  [LOGIN]: [enrollRequired()],
  [ENROLL]: [enrollAnswer()],
  [CONFIRM]: signing(live, loginConfirmAnswer()),
});

describe("S2 -> S4 -> S5 / M9, M27: a required enrolment at sign-in", () => {
  it("S2 says why; Set up starts the enrolment; the key shows on request; a wrong code stays on S4; a right one shows the ten codes", async () => {
    let tries = 0;
    const ctx = open((live) => {
      const good = signing(live, loginConfirmAnswer());
      return {
        ...enrolRoutes(live),
        [CONFIRM]: (call) => {
          tries += 1;
          return tries === 1 ? problem(401, "mfa_invalid") : good(call);
        },
      };
    });
    await toS2(ctx);
    expect(screen.getByText("Your organization requires a second step for owners.")).toBeInTheDocument();
    expect(sentTo(ctx, "/v1/auth/login/mfa/enroll")).toHaveLength(0); // nothing is sent on mount
    await ctx.user.click(screen.getByRole("button", { name: "Set up" }));
    await screen.findByTestId("mfa-setup");
    expect(sentTo(ctx, "/v1/auth/login/mfa/enroll")[0]?.body).toEqual({ challenge: CHALLENGE });
    expect(screen.queryByTestId("mfa-key-text")).not.toBeInTheDocument(); // behind a click
    await ctx.user.click(screen.getByRole("button", { name: "Can't scan? Show the key" }));
    expect(screen.getByTestId("mfa-key-text")).toHaveTextContent(SECRET);
    await typeSetupCode(ctx);
    expect(await screen.findByTestId("mfa-error")).toHaveTextContent("That code is not right.");
    expect(screen.queryByTestId("mfa-recovery-codes")).not.toBeInTheDocument();
    await typeSetupCode(ctx, "654321");
    expect(await screen.findByTestId("mfa-recovery-codes")).toHaveTextContent(CODES[0]!);
    expect(sentTo(ctx, "/v1/auth/login/mfa/enroll/confirm")[1]?.body).toMatchObject({ challenge: CHALLENGE, code: "654321" });
  });

  it("M27: after the confirm and before Continue there is no redirect and AuthState is signed out; Continue re-reads the session, then lands on next", async () => {
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    expect(ctx.router.state.location.pathname).toBe("/login");
    expect(ctx.auth.state().kind).toBe("signed-out");
    expect(ctx.count(SESSION)).toBe(1); // only the start-up check
    const cont = screen.getByRole("button", { name: "Continue" });
    expect(cont).toBeDisabled();
    await ctx.user.click(screen.getByLabelText("I saved these codes"));
    expect(cont).toBeEnabled();
    await ctx.user.click(cont);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(ctx.count(SESSION)).toBe(2); // Continue re-read the session
    expect(ctx.auth.state().kind).toBe("signed-in");
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBeNull();
    expect(screen.queryByText(CODES[0]!)).not.toBeInTheDocument();
  });

  it("the held screen cannot make a signed-out person signed in: sync events while S5 shows change nothing", async () => {
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    expect(ctx.auth.held()).toBe(true);
    act(() => { ctx.tabs.fire(); }); // another tab says "who is signed in changed" (the cookie now exists)
    act(() => { ctx.tabs.fire(); });
    expect(ctx.auth.state().kind).toBe("signed-out");
    expect(screen.getByTestId("mfa-recovery-codes")).toBeInTheDocument();
    expect(screen.queryByText("Checking your session…")).not.toBeInTheDocument();
    expect(ctx.router.state.location.pathname).toBe("/login");
    expect(ctx.count(SESSION)).toBe(1);
    await ctx.user.click(screen.getByLabelText("I saved these codes"));
    await ctx.user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(ctx.auth.held()).toBe(false);
  });

  it("Continue with the session gone (signed out in another tab) lands on the password step, signed out", async () => {
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    ctx.live.on = false;
    await ctx.user.click(screen.getByLabelText("I saved these codes"));
    await ctx.user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Your session ended. Sign in again to carry on.")).toBeInTheDocument();
    expect(ctx.auth.state().kind).toBe("signed-out");
    expect(ctx.router.state.location.pathname).toBe("/login");
    expect(ctx.auth.held()).toBe(false);
  });

  it("a double click on Set up sends exactly one request (W-19)", async () => {
    const ctx = open(enrolRoutes);
    await toS2(ctx);
    await ctx.user.dblClick(screen.getByRole("button", { name: "Set up" }));
    await screen.findByTestId("mfa-setup");
    expect(sentTo(ctx, "/v1/auth/login/mfa/enroll")).toHaveLength(1);
  });

  it("a start answered without no-store is discarded: the unsafe text at the password step", async () => {
    const ctx = open(() => ({ [LOGIN]: [enrollRequired()], [ENROLL]: [{ body: enrollAnswer().body, headers: { "cache-control": "no-cache" } }] }));
    await toS2(ctx);
    await ctx.user.click(screen.getByRole("button", { name: "Set up" }));
    expect(await screen.findByText(/This server answered unsafely/)).toBeInTheDocument();
    expect(screen.queryByTestId("mfa-setup")).not.toBeInTheDocument();
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(await screen.findByLabelText("Password")).toHaveValue("");
  });

  it("503 mfa_unavailable at the start: the operator text, back at the password step", async () => {
    const ctx = open(() => ({ [LOGIN]: [enrollRequired()], [ENROLL]: [problem(503, "mfa_unavailable")] }));
    await toS2(ctx);
    await ctx.user.click(screen.getByRole("button", { name: "Set up" }));
    expect(await screen.findByText("Two-step sign-in is not available on this server. Ask the operator.")).toBeInTheDocument();
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  });

  it("an unknown outcome of the confirm (502): the session check runs; none -> 'could not tell' at the password step; the flag is set; one request", async () => {
    const ctx = open(() => ({ [LOGIN]: [enrollRequired()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(502, "bad_gateway")] }));
    await toS4(ctx);
    await typeSetupCode(ctx);
    expect(await screen.findByText(/We could not tell whether this worked/)).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(sentTo(ctx, "/v1/auth/login/mfa/enroll/confirm")).toHaveLength(1);
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
    expect(ctx.auth.held()).toBe(false);
  });

  it("an unknown outcome with a session on the server lands on the page with the lost-codes notice", async () => {
    const ctx = open((live) => ({
      [LOGIN]: [enrollRequired()],
      [ENROLL]: [enrollAnswer()],
      [CONFIRM]: () => { live.on = true; return problem(502, "bad_gateway"); },
    }));
    await toS4(ctx);
    await typeSetupCode(ctx);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(await screen.findByTestId("mfa-lost-notice-page")).toBeInTheDocument();
  });

  it("an unsafe confirm answer (no no-store) shows no codes and sets the flag; the session check finds the session", async () => {
    const ctx = open((live) => ({
      [LOGIN]: [enrollRequired()],
      [ENROLL]: [enrollAnswer()],
      [CONFIRM]: () => { live.on = true; return { body: loginConfirmAnswer().body, headers: { "cache-control": "no-cache" } }; },
    }));
    await toS4(ctx);
    await typeSetupCode(ctx);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(screen.queryByText(CODES[0]!)).not.toBeInTheDocument();
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });

  it("409 no_pending_factor on the confirm: 'Setup expired' at the password step, nothing shown", async () => {
    const ctx = open(() => ({ [LOGIN]: [enrollRequired()], [ENROLL]: [enrollAnswer()], [CONFIRM]: [problem(409, "no_pending_factor")] }));
    await toS4(ctx);
    await typeSetupCode(ctx);
    expect(await screen.findByText("Setup expired. Start again.")).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(ctx.auth.held()).toBe(false);
  });
});

describe("clearing the secrets (W-6, M28, M29) and timers (AU-5, M37)", () => {
  it("pagehide before a confirm empties S4: 'Setup was interrupted', no secret, no URI; Start again goes to the password step", async () => {
    const ctx = open(enrolRoutes);
    await toS4(ctx);
    await ctx.user.click(screen.getByRole("button", { name: "Can't scan? Show the key" }));
    act(() => { globalThis.dispatchEvent(new Event("pagehide")); });
    expect(screen.getByText("Setup was interrupted. Start again.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain("otpauth");
    expect(ctx.auth.held()).toBe(false);
    await ctx.user.click(screen.getByRole("button", { name: "Start again" }));
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  });

  it("pagehide on S5 empties the codes and shows the post-commit text, never 'Start again'; Continue goes to Settings", async () => {
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    act(() => { globalThis.dispatchEvent(new Event("pagehide")); });
    expect(document.body.innerHTML).not.toContain(CODES[0]!);
    expect(screen.getByText("MFA is on. Your recovery codes were not saved: regenerate them in Settings > Security.")).toBeInTheDocument();
    expect(screen.queryByText("Start again")).not.toBeInTheDocument();
    await ctx.user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/settings"); });
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });

  it("a restore from the back/forward cache (pageshow persisted) shows the cleared state", async () => {
    const ctx = open(enrolRoutes);
    await toS4(ctx);
    act(() => { globalThis.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true })); });
    expect(screen.getByText("Setup was interrupted. Start again.")).toBeInTheDocument();
  });

  it("the enrolment challenge lapses at 300 s even when the server said 900 (M7)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ctx = open((live) => ({ ...enrolRoutes(live), [LOGIN]: [enrollRequired({ expires_in: 900 })] }));
    await toS2(ctx);
    await act(async () => { await vi.advanceTimersByTimeAsync(299_000); });
    expect(screen.getByTestId("mfa-enroll-required")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByText("That sign-in step timed out. Start again.")).toBeInTheDocument();
  });

  it("M37: with S5 open, past the challenge life, the codes are still shown and nothing redirects", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ctx = open((live) => ({ ...enrolRoutes(live), [LOGIN]: [enrollRequired({ expires_in: 120 })] }));
    await toS5(ctx);
    await act(async () => { await vi.advanceTimersByTimeAsync(180_000); });
    expect(screen.getByTestId("mfa-recovery-codes")).toBeInTheDocument();
    expect(screen.queryByText(/timed out/)).not.toBeInTheDocument();
    expect(ctx.router.state.location.pathname).toBe("/login");
  });
});

describe("M8: nothing secret is stored or left in the address", () => {
  it("while the enrolment codes show, storage, cookies, the URL and history hold no challenge, secret, URI or code", async () => {
    const ctx = open(enrolRoutes);
    await toS5(ctx);
    const everything =
      JSON.stringify(dump(globalThis.localStorage)) +
      JSON.stringify(dump(globalThis.sessionStorage)) +
      document.cookie +
      globalThis.location.href +
      JSON.stringify(ctx.router.state.location) +
      JSON.stringify(globalThis.history.state);
    for (const secret of [CHALLENGE, SECRET, URI, "123456", ...CODES]) expect(everything).not.toContain(secret);
    expect(Object.keys(dump(globalThis.sessionStorage))).toEqual([CODES_UNSAVED_KEY]);
    expect(globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY)).toBe("1");
  });
});

// The code-step safeguards of the accepted #1082 head, on main's inline code step (#922).
describe("S1 / M1 / M3: an enrolled person gets the code step, then a session", () => {
  it("shows the code step, signed out, with no password field", async () => {
    const ctx = open();
    await password(ctx);
    expect(await codeField()).toBeInTheDocument();
    expect(ctx.auth.state().kind).toBe("signed-out");
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(ctx.router.state.location.pathname).toBe("/login");
  });

  it("a right code (typed with spaces) is sent as {challenge, code} only, signs in and lands on the page wanted", async () => {
    const ctx = open((live) => ({ [MFA]: signing(live, session()) }));
    await password(ctx);
    await ctx.user.type(await codeField(), "12 34 56");
    await submit(ctx);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(sentTo(ctx, "/v1/auth/login/mfa")[0]?.body).toEqual({ challenge: CHALLENGE, code: "123456" });
    expect(ctx.auth.state().kind).toBe("signed-in");
  });

  it("M4: seven digits, a letter, or Arabic-Indic digits disable submit and send nothing", async () => {
    const ctx = open();
    await password(ctx);
    const field = await codeField();
    for (const bad of ["1234567", "12345a", "١٢٣٤٥٦"]) {
      await ctx.user.clear(field);
      await ctx.user.type(field, bad);
      expect(screen.getByRole("button", { name: "Verify" })).toBeDisabled();
    }
    expect(sentTo(ctx, "/v1/auth/login/mfa")).toHaveLength(0);
  });

  it("the code field keeps spaced input and has the autofill hints (W-24, M46)", async () => {
    const ctx = open();
    await password(ctx);
    const field = await codeField();
    expect(field).toHaveAttribute("autocomplete", "one-time-code");
    expect(field).not.toHaveAttribute("maxlength");
    await ctx.user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    const recovery = screen.getByLabelText("Recovery code");
    expect(recovery).toHaveAttribute("autocomplete", "off");
    expect(Number(recovery.getAttribute("maxlength"))).toBeLessThanOrEqual(40);
    expect(Number(recovery.getAttribute("maxlength"))).toBeGreaterThanOrEqual(19);
  });

  it("S8: tells a locked-out person to ask an owner, and prints no operator command", async () => {
    const ctx = open();
    await password(ctx);
    const line = await screen.findByTestId("login-mfa-lost");
    expect(line).toHaveTextContent("Ask an owner to reset your MFA. If you are the only owner, ask whoever runs the server.");
    expect(line.textContent).not.toMatch(/nuramesh|auth mfa|\//);
  });

  it("Back drops the challenge and returns to an empty password field (W-15)", async () => {
    const ctx = open();
    await password(ctx);
    await codeField();
    await ctx.user.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(await screen.findByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Email")).toHaveValue("ada@example.org");
  });
});

describe("S1: a recovery code (M5, M19)", () => {
  it("is normalised and sent as recovery_code only; a code with I, O, 0 or 1 is never sent; the banner shows once", async () => {
    const ctx = open((live) => ({ [MFA]: signing(live, session()) }));
    await password(ctx);
    await ctx.user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    const field = screen.getByLabelText("Recovery code");
    for (const bad of ["ABCD-EFGH-JKLM-NPQ1", "ABCD-EFGH-JKLM-NPQO", "ABCD-EFGH-JKLM-NPQI", "ABCD-EFGH-JKLM-NPQ0"]) {
      await ctx.user.clear(field);
      await ctx.user.type(field, bad);
      expect(screen.getByRole("button", { name: "Verify" })).toBeDisabled();
    }
    expect(sentTo(ctx, "/v1/auth/login/mfa")).toHaveLength(0);
    await ctx.user.clear(field);
    await ctx.user.type(field, "aaaa aaaa-aaaa-aaaa");
    await submit(ctx);
    expect(await screen.findByTestId("mfa-recovery-signin-notice")).toHaveTextContent("You signed in with a recovery code. 4 left.");
    expect(sentTo(ctx, "/v1/auth/login/mfa")[0]?.body).toEqual({ challenge: CHALLENGE, recovery_code: "AAAAAAAAAAAAAAAA" });
    expect(JSON.stringify({ session: dump(globalThis.sessionStorage), local: dump(globalThis.localStorage) })).not.toContain("recovery");
    await act(async () => { await ctx.router.navigate("/approvals"); });
    await waitFor(() => { expect(screen.queryByTestId("mfa-recovery-signin-notice")).not.toBeInTheDocument(); });
  });

  it("an app-code sign-in shows no banner", async () => {
    const ctx = open((live) => ({ [MFA]: signing(live, session()) }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(screen.queryByTestId("mfa-recovery-signin-notice")).not.toBeInTheDocument();
  });
});


describe("the code step and the back/forward cache (W-6)", () => {
  it("pagehide drops the challenge: the password step shows, no code step, no request", async () => {
    const ctx = open();
    await password(ctx);
    await codeField();
    act(() => { globalThis.dispatchEvent(new Event("pagehide")); });
    expect(screen.queryByTestId("mfa-code-step")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(sentTo(ctx, "/v1/auth/login/mfa")).toHaveLength(0);
  });

  it("a restore from the back/forward cache (pageshow persisted) never shows the code step; a normal pageshow changes nothing", async () => {
    const ctx = open();
    await password(ctx);
    await codeField();
    act(() => { globalThis.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: false })); });
    expect(screen.getByTestId("mfa-code-step")).toBeInTheDocument();
    act(() => { globalThis.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true })); });
    expect(screen.queryByTestId("mfa-code-step")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });
});


describe("the recovery-sign-in flag and the console", () => {
  it("a recovery sign-in with an unsafe answer leaves no banner on a later password login", async () => {
    let logins = 0;
    const ctx = open((live) => ({
      [LOGIN]: () => { logins += 1; if (logins === 1) return mfaRequired(); live.on = true; return session(); },
      [MFA]: [{ body: { csrf_token: null }, headers: NO_STORE }],
    }));
    await password(ctx);
    await ctx.user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    await ctx.user.type(screen.getByLabelText("Recovery code"), "abcd-efgh-jklm-npqr");
    await submit(ctx);
    expect(await screen.findByText(/This server answered unsafely/)).toBeInTheDocument();
    expect(recoverySignInPending()).toBe(false);
    // later, on the same page load, a plain password login
    await ctx.user.click(screen.getByRole("button", { name: "Back to sign in" }));
    await ctx.user.type(await screen.findByLabelText("Password"), PASSWORD);
    await ctx.user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    expect(screen.queryByTestId("mfa-recovery-signin-notice")).not.toBeInTheDocument();
  });

  it("the code and recovery steps, a wrong code and a 429 write nothing to console", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const ctx = open(() => ({ [MFA]: [problem(401, "mfa_invalid"), problem(429, "too_many_attempts", { "retry-after": "30" })] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx); // wrong code
    await screen.findByTestId("login-failure");
    await ctx.user.type(screen.getByLabelText("Code from your authenticator app"), "123456");
    await submit(ctx); // 429
    await screen.findByTestId("login-wait");
    await ctx.user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    expect(screen.getByLabelText("Recovery code")).toBeInTheDocument();
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });


});

describe("S1: errors (W-2, W-3, M6)", () => {
  it("401 mfa_invalid: one fixed text, no counts, field cleared, one request, no retry", async () => {
    const ctx = open(() => ({ [MFA]: [problem(401, "mfa_invalid")] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    const text = await screen.findByTestId("login-failure");
    expect(text).toHaveTextContent("That code is not right.");
    expect(text.textContent).not.toMatch(/\d|tries|left|remaining|authenticator|recovery/i);
    expect(screen.getByLabelText("Code from your authenticator app")).toHaveValue("");
    expect(sentTo(ctx, "/v1/auth/login/mfa")).toHaveLength(1);
    expect(ctx.auth.state().kind).toBe("signed-out");
  });

  it("401 challenge_invalid: back to the password step, empty, with the timeout text", async () => {
    const ctx = open(() => ({ [MFA]: [problem(401, "challenge_invalid")] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    expect(await screen.findByLabelText("Password")).toHaveValue("");
    expect(screen.getByText("That sign-in step timed out. Start again.")).toBeInTheDocument();
  });

  it("429 with Retry-After: the field is disabled with a countdown", async () => {
    const ctx = open(() => ({ [MFA]: [problem(429, "too_many_attempts", { "retry-after": "30" })] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    expect(await screen.findByTestId("login-wait")).toHaveTextContent("Try again in 30 seconds.");
    expect(screen.getByLabelText("Code from your authenticator app")).toBeDisabled();
  });

  it("503 mfa_unavailable at the code step: the recovery-code text, the recovery link focused (M44)", async () => {
    const ctx = open(() => ({ [MFA]: [problem(503, "mfa_unavailable")] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    expect(await screen.findByText("Two-step sign-in with an app code is not available on this server right now. Use a recovery code.")).toBeInTheDocument();
    await waitFor(() => { expect(screen.getByRole("button", { name: "Use a recovery code instead" })).toHaveFocus(); });
  });

  it("a success body with no user: nobody is signed in, the unsafe text shows, the page then checks for a session", async () => {
    const ctx = open(() => ({ [MFA]: [{ body: { csrf_token: null }, headers: NO_STORE }] }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    expect(await screen.findByText(/This server answered unsafely/)).toBeInTheDocument();
    expect(ctx.auth.state().kind).not.toBe("signed-in");
    expect(ctx.count(SESSION)).toBeGreaterThanOrEqual(2); // the start-up check and the one after the unsafe answer
  });

  it("the challenge timer returns to the password step by itself (AU-5)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ctx = open(() => ({ [LOGIN]: [mfaRequired({ expires_in: 60 })] }));
    await password(ctx);
    await codeField();
    await act(async () => { await vi.advanceTimersByTimeAsync(59_000); });
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(await screen.findByLabelText("Password")).toHaveValue("");
    expect(screen.getByText("That sign-in step timed out. Start again.")).toBeInTheDocument();
  });
});


describe("the password step: unknown answers (M2)", () => {
  it("an answer that is none of the known shapes signs nobody in and says so (M2)", async () => {
    const ctx = open(() => ({ [LOGIN]: [{ body: { status: "mfa_required", challenge: "x", methods: ["totp"], expires_in: 60 } }] }));
    await password(ctx);
    expect(await screen.findByText(/answer this page does not understand/)).toBeInTheDocument();
    expect(ctx.auth.state().kind).toBe("signed-out");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });



  it("the code sign-in leaves no challenge or code in storage or the address", async () => {
    const ctx = open((live) => ({ [MFA]: signing(live, session()) }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    // S1 is shown: the sign-in challenge and the typed code are not in history.state.
    expect(JSON.stringify(globalThis.history.state)).not.toContain(CHALLENGE);
    expect(JSON.stringify(globalThis.history.state)).not.toContain("123456");
    await submit(ctx);
    await waitFor(() => { expect(ctx.router.state.location.pathname).toBe("/runs"); });
    const all = JSON.stringify(dump(globalThis.localStorage)) + JSON.stringify(dump(globalThis.sessionStorage)) + document.cookie + globalThis.location.href;
    expect(all).not.toContain(CHALLENGE);
    expect(all).not.toContain("123456");
    expect(JSON.stringify(ctx.router.state.location)).not.toContain(CHALLENGE);
  });

  it("the timer firing while a code check is in flight does not hide an unsafe answer: the session check still runs (AU-5)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const ctx = open(() => ({
      [LOGIN]: [mfaRequired({ expires_in: 60 })],
      [MFA]: async () => {
        await gate;
        return { body: { csrf_token: null }, headers: NO_STORE };
      },
    }));
    await password(ctx);
    await ctx.user.type(await codeField(), "123456");
    await submit(ctx);
    const before = ctx.count(SESSION);
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
    await act(async () => { release(); await vi.advanceTimersByTimeAsync(10); });
    expect(await screen.findByText(/This server answered unsafely/)).toBeInTheDocument();
    expect(ctx.count(SESSION)).toBeGreaterThan(before);
  });
});
