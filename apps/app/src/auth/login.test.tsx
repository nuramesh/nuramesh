import { QueryClient } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { createServerClient } from "@/api/server-client";
import { ProblemError } from "@/api/client";
import { renderAt, serverLike } from "@/test/server-like";

import type { AuthApi, AuthState } from "./types";

const ACCOUNT = { email: "ada@example.org", password: "obviously-fake-test-password", displayName: "Ada Lovelace" };
const OUT: AuthState = { kind: "signed-out" };
const TOKEN = "obviously-fake-setup-token";

function fixture(extra: { mfa?: { code: string; recoveryCode: string }; enroll?: boolean; signedIn?: boolean; lockAfter?: number; lockSeconds?: number; setupToken?: string; ownerMissing?: boolean; mailEnabled?: boolean } = {}) {
  return createFixtureClient({ signIn: { account: ACCOUNT, setupToken: TOKEN, ...extra } });
}

async function signIn(user: ReturnType<typeof userEvent.setup>, password = ACCOUNT.password) {
  await user.type(await screen.findByLabelText("Email"), ACCOUNT.email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("the gate", () => {
  it("needs no sign-in where the source has none (sample data)", async () => {
    renderAt("/runs", createFixtureClient());
    expect(await screen.findByRole("heading", { level: 1, name: /Runs/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("sends nobody to a page until signed in, and remembers where they were going", async () => {
    const router = renderAt("/approvals", fixture());
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    await signIn(userEvent.setup());
    await waitFor(() => {
      expect(router.state.location.pathname).toMatch(/^\/approvals/); // /approvals shows the first request
    });
    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("shows nothing but a status while it asks the server who is signed in", async () => {
    const hook: { finish: (() => void) | null } = { finish: null }; // set once the app has asked load()
    const listeners = new Set<() => void>();
    let state: AuthState = { kind: "checking" };
    const auth: AuthApi = {
      state: () => state,
      subscribe: (l) => {
        listeners.add(l);
        return () => {
          listeners.delete(l);
        };
      },
      load: () =>
        new Promise<void>((resolve) => {
          hook.finish = () => {
            state = { kind: "signed-out" };
            listeners.forEach((l) => { l(); });
            resolve();
          };
        }),
      login: () => Promise.resolve({ kind: "signed-in" as const }),
      completeMfa: () => Promise.resolve(),
      logout: () => Promise.resolve(),
      redeemSetup: () => Promise.resolve(),
      setupStatus: () => Promise.resolve(false),
    };
    renderAt("/runs", serverLike({ auth }));
    expect(await screen.findByText("Checking your session…")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    // "checking" renders before the effect that calls load(): wait for the call, not a delay
    await waitFor(() => {
      expect(hook.finish).not.toBeNull();
    });
    act(() => {
      hook.finish?.();
    });
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
  });

  it("does not take the person for signed out when the server can't be reached", async () => {
    let calls = 0;
    const state: AuthState = { kind: "unavailable", message: "Couldn't reach the NuraMesh server." };
    const auth: AuthApi = {
      state: () => state,
      subscribe: () => () => undefined,
      load: () => {
        calls += 1;
        return Promise.resolve();
      },
      login: () => Promise.resolve({ kind: "signed-in" as const }),
      completeMfa: () => Promise.resolve(),
      logout: () => Promise.resolve(),
      redeemSetup: () => Promise.resolve(),
      setupStatus: () => Promise.resolve(false),
    };
    renderAt("/runs", serverLike({ auth }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't reach the NuraMesh server.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(calls).toBeGreaterThanOrEqual(1);
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });
});

describe("the login screen", () => {
  it("signs in and shows who, with a sign-out button", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture());
    await signIn(user);
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("a wrong password says so once, in plain words, and empties the password field", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture());
    await signIn(user, "obviously-fake-wrong-password");
    expect(await screen.findByTestId("login-failure")).toHaveTextContent("The email or password is not right.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByLabelText("Email")).toHaveValue(ACCOUNT.email);
  });

  it("marks the form for password managers and phones", async () => {
    renderAt("/login", fixture());
    const email = await screen.findByLabelText("Email");
    expect(email).toHaveAttribute("autocomplete", "username");
    expect(email).toHaveAttribute("inputmode", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    // 44 px touch targets at phone width
    expect(email.className).toMatch(/\bh-11\b/);
    expect(screen.getByRole("button", { name: "Sign in" }).className).toMatch(/min-h-11/);
  });

  it("keeps the button off until both fields have something", async () => {
    renderAt("/login", fixture());
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("the button is the primary brand colour and enabled as soon as both fields have something", async () => {
    renderAt("/login", fixture());
    const button = await screen.findByRole("button", { name: "Sign in" });
    await userEvent.setup().type(screen.getByLabelText("Email"), ACCOUNT.email);
    expect(button).toBeDisabled();
    await userEvent.setup().type(screen.getByLabelText("Password"), "x");
    expect(button).toBeEnabled();
    expect(button.className).toMatch(/\bbg-primary\b/);
  });

  it("centres the logo, title, subtitle and footer; the fields stay left and full width", async () => {
    renderAt("/login", fixture({ ownerMissing: true }));
    const title = await screen.findByRole("heading", { name: "Sign in to NuraMesh" });
    expect(title.parentElement?.className).toMatch(/\btext-center\b/);
    expect(screen.getByText("Use the account your administrator gave you.").closest("header")?.className).toMatch(/\btext-center\b/);
    expect(screen.getByText(/Forgot your password/).className).toMatch(/\btext-center\b/);
    expect(screen.getByLabelText("Email").className).toMatch(/\bw-full\b|\bh-11\b/);
    expect(screen.getByText("Email").className).not.toMatch(/text-center/);
  });

  it("says what to use, and what to do about a forgotten password, under the button", async () => {
    renderAt("/login", fixture());
    expect(await screen.findByText("Use the account your administrator gave you.")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Sign in" });
    const forgot = screen.getByText("Forgot your password? Ask your administrator.");
    // after the button in the page order
    expect(button.compareDocumentPosition(forgot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("offers the reset by email, and no administrator text, where the server has email on", async () => {
    renderAt("/login", fixture({ mailEnabled: true }));
    const link = await screen.findByRole("link", { name: "Reset it by email" });
    expect(link).toHaveAttribute("href", "/forgot-password");
    expect(screen.queryByText(/Ask your administrator/)).not.toBeInTheDocument();
  });

  it("hides the reset link and says to ask the administrator where email is off", async () => {
    renderAt("/login", fixture({ mailEnabled: false }));
    expect(await screen.findByText("Forgot your password? Ask your administrator.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Reset it by email" })).not.toBeInTheDocument();
  });

  it("the password can be shown and hidden with a labelled, keyboard-operable button, autofill unchanged", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture());
    const password = await screen.findByLabelText("Password");
    await user.type(password, "obviously-fake-test-password");
    const toggle = screen.getByRole("button", { name: "Show password" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toHaveAttribute("type", "button"); // never submits the form
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(password).toHaveAttribute("type", "text");
    expect(password).toHaveValue("obviously-fake-test-password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    const hide = screen.getByRole("button", { name: "Hide password" });
    expect(hide).toHaveAttribute("aria-pressed", "true");
    await user.click(hide);
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByRole("button", { name: "Show password" }).className).toMatch(/size-11|min-h-11|h-11/);
  });

  it("a failed sign-in hides the password again and empties it", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture());
    await user.click(await screen.findByRole("button", { name: "Show password" }));
    await signIn(user, "obviously-fake-wrong-password");
    await screen.findByTestId("login-failure");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("offers 'Create the owner account' only while the server has no owner", async () => {
    renderAt("/login", fixture());
    await screen.findByRole("heading", { name: "Sign in to NuraMesh" });
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Create the owner account" })).not.toBeInTheDocument();
    });
  });

  it("shows the setup link when the server says it has no owner, in every login state", async () => {
    renderAt("/login", fixture({ ownerMissing: true }));
    expect(await screen.findByRole("link", { name: "Create the owner account" })).toBeInTheDocument();
  });

  it("never lets the sign-in carry the person to another site", async () => {
    const router = renderAt("/login", fixture());
    await router.navigate("/login", { state: { from: "//evil.example/x" } });
    await signIn(userEvent.setup());
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/builder");
    });
  });

  it("a signed-in person who opens /login goes straight on", async () => {
    const router = renderAt("/login", fixture({ signedIn: true }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/builder");
    });
  });
});

describe("lockout (429 with Retry-After)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says how long to wait, counts down, and lets the person try again after it", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) });
    renderAt("/login", fixture({ lockAfter: 2, lockSeconds: 90 }));
    await signIn(user, "obviously-fake-wrong-password");
    await signIn(user, "obviously-fake-wrong-password");
    // the third attempt is refused with a lock
    await signIn(user, ACCOUNT.password);
    expect(await screen.findByTestId("login-failure")).toHaveTextContent("Too many sign-in attempts.");
    expect(screen.getByTestId("login-wait")).toHaveTextContent("Try again in 1 minute 30 seconds.");
    expect(screen.getByRole("button", { name: /^Wait 1 minute/ })).toBeDisabled();
    // the live alert holds only the stable sentence, not the ticking time
    expect(screen.getByRole("alert")).not.toHaveTextContent(/\d+ (second|minute)/);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByTestId("login-wait")).toHaveTextContent("Try again in 1 minute.");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000);
    });
    expect(screen.getByTestId("login-wait")).toHaveTextContent("You can try again now.");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled(); // password was cleared
  });

  it("a 429 without a wait still stops the person from hammering", async () => {
    const auth: AuthApi = {
      state: () => OUT,
      subscribe: () => () => undefined,
      load: () => Promise.resolve(),
      login: () => Promise.reject(new ProblemError("x", 429, "rate_limited")),
      completeMfa: () => Promise.resolve(),
      logout: () => Promise.resolve(),
      redeemSetup: () => Promise.resolve(),
      setupStatus: () => Promise.resolve(false),
    };
    renderAt("/login", serverLike({ auth }));
    await signIn(userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) }));
    expect(await screen.findByTestId("login-failure")).toHaveTextContent("Wait a little and try again.");
  });
});

describe("sign out and expiry", () => {
  it("Sign out returns to the login screen, drops the fetched data and says so", async () => {
    const user = userEvent.setup();
    const router = renderAt("/runs", fixture({ signedIn: true }));
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You are signed out.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("an ended session lands on the login screen with the reason, then back on the page", async () => {
    let state: AuthState = { kind: "signed-in", user: { id: "human:u_0a1b2c3d4e5f60718293", orgId: "acme", role: "owner", email: ACCOUNT.email, displayName: "Ada" } };
    const listeners = new Set<() => void>();
    const set = (next: AuthState): void => {
      state = next;
      listeners.forEach((l) => { l(); });
    };
    const auth: AuthApi = {
      state: () => state,
      subscribe: (l) => {
        listeners.add(l);
        return () => {
          listeners.delete(l);
        };
      },
      load: () => Promise.resolve(),
      login: () => {
        set({ kind: "signed-in", user: { id: "human:u_0a1b2c3d4e5f60718293", orgId: "acme", role: "owner", email: ACCOUNT.email, displayName: "Ada" } });
        return Promise.resolve({ kind: "signed-in" as const });
      },
      completeMfa: () => Promise.resolve(),
      logout: () => Promise.resolve(),
      redeemSetup: () => Promise.resolve(),
      setupStatus: () => Promise.resolve(false),
    };
    const router = renderAt("/ledger", serverLike({ auth }));
    await screen.findByRole("navigation", { name: "Main" });
    act(() => {
      set({ kind: "signed-out", reason: "expired" });
    });
    expect(await screen.findByText("Your session ended. Sign in again to carry on.")).toBeInTheDocument();
    await signIn(userEvent.setup());
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/ledger");
    });
  });
});

describe("first-owner setup", () => {
  async function fill(user: ReturnType<typeof userEvent.setup>, over: { token?: string; password?: string; again?: string } = {}) {
    await user.type(await screen.findByLabelText("Setup token"), over.token ?? TOKEN);
    await user.type(screen.getByLabelText("Email"), ACCOUNT.email);
    await user.type(screen.getByLabelText("Name (optional)"), "Ada");
    await user.type(screen.getByLabelText("Password"), over.password ?? ACCOUNT.password);
    await user.type(screen.getByLabelText("Password again"), over.again ?? over.password ?? ACCOUNT.password);
  }

  it("makes the owner, then asks them to sign in (it never signs anyone in by itself)", async () => {
    const user = userEvent.setup();
    const router = renderAt("/setup", fixture());
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create owner account" }));
    expect(await screen.findByText("The owner account is ready. Sign in with it.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("refuses a password under 12 characters before asking the server", async () => {
    const user = userEvent.setup();
    renderAt("/setup", fixture());
    await fill(user, { password: "short-pass" });
    expect(screen.getByText("That is shorter than 12 characters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create owner account" })).toBeDisabled();
  });

  it("asks for the password twice", async () => {
    const user = userEvent.setup();
    renderAt("/setup", fixture());
    await fill(user, { again: "obviously-fake-other-password" });
    expect(screen.getByText("The two passwords are not the same.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create owner account" })).toBeDisabled();
  });

  it("a token that can't be used gets one message and the way to make a new one", async () => {
    const user = userEvent.setup();
    renderAt("/setup", fixture());
    await fill(user, { token: "obviously-fake-wrong-token" });
    await user.click(screen.getByRole("button", { name: "Create owner account" }));
    const failure = await screen.findByTestId("setup-failure");
    expect(failure).toHaveTextContent(/wrong, already used or expired/);
    expect(failure).toHaveTextContent("nuramesh auth new-setup-token");
    expect(screen.getByRole("button", { name: "Create owner account" })).toBeEnabled();
  });

  it("is reachable from the login screen", async () => {
    const user = userEvent.setup();
    const router = renderAt("/login", fixture({ ownerMissing: true }));
    await user.click(await screen.findByRole("link", { name: "Create the owner account" }));
    expect(router.state.location.pathname).toBe("/setup");
  });
});

describe("with the real server client (fake network)", () => {
  const USER = { id: "human:u_0a1b2c3d4e5f60718293", role: "owner", email: ACCOUNT.email, display_name: "Ada" };
  const json = (status: number, body: unknown): Response =>
    new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } });
  const invalid = (): Response => json(401, { title: "session_invalid", status: 401, detail: "x" });

  it("asks who is signed in, shows the login, signs in, and when the session later ends says so", async () => {
    let signedIn = false;
    let sessionEnded = false;
    const seen: string[] = [];
    const fetchFn = vi.fn((input: string, init?: RequestInit): Promise<Response> => {
      const path = new URL(input, "http://127.0.0.1:5173").pathname;
      const key = `${init?.method ?? "GET"} ${path}`;
      seen.push(key);
      if (key === "GET /v1/auth/session") return Promise.resolve(signedIn ? json(200, { user: USER, session: { id: "s" }, csrf_token: "c" }) : invalid());
      if (key === "POST /v1/auth/refresh") return Promise.resolve(invalid());
      if (key === "POST /v1/auth/login") {
        signedIn = true;
        return Promise.resolve(json(200, { user: USER, session: { id: "s" }, csrf_token: "c" }));
      }
      if (key === "GET /v1/runs") return Promise.resolve(sessionEnded ? invalid() : json(200, { runs: [], truncated: false }));
      return Promise.resolve(json(404, { title: "not_found", status: 404, detail: "x" }));
    });
    const client = createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } });
    const router = renderAt("/runs", client);
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
    expect(screen.queryByText("Your session ended. Sign in again to carry on.")).not.toBeInTheDocument();
    await signIn(userEvent.setup());
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/runs");
    // The server ends the session; the next read finds out and the person is sent back to sign in.
    sessionEnded = true;
    await act(async () => {
      await client.runs.list().catch(() => undefined);
    });
    expect(await screen.findByText("Your session ended. Sign in again to carry on.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(seen.filter((k) => k === "POST /v1/auth/refresh").length).toBeGreaterThan(0);
  });
});

const MFA = { code: "246810", recoveryCode: "AAAAAAAAAAAAAAAA" };

describe("a second step (MFA)", () => {
  it("shows the code screen after the password, not a session, and signs in with a right code", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    expect(await screen.findByRole("heading", { name: "Enter your code" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Code from your authenticator app"), MFA.code);
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
  });

  it("a wrong code gives one fixed message, keeps the screen and empties the field", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByTestId("login-failure")).toHaveTextContent("That code is not right. Check it and try again.");
    expect(screen.getByLabelText("Code from your authenticator app")).toHaveValue("");
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
  });

  it("moves focus to the code field when the second step appears", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    const field = await screen.findByLabelText("Code from your authenticator app");
    expect(field).toHaveFocus();
  });

  it("puts focus back in the emptied code field after a wrong code (Verify button pressed)", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByTestId("login-failure")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Code from your authenticator app")).toHaveFocus();
    });
    expect(screen.getByRole("alert")).toHaveTextContent("That code is not right");
  });

  it("moves focus to the recovery-code field after the toggle, and back again", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    await user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    expect(screen.getByLabelText("Recovery code")).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Use the authenticator app instead" }));
    expect(screen.getByLabelText("Code from your authenticator app")).toHaveFocus();
  });

  it("a recovery code works from its own link", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    await user.click(await screen.findByRole("button", { name: "Use a recovery code instead" }));
    await user.type(screen.getByLabelText("Recovery code"), MFA.recoveryCode);
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
  });

  it("goes back to the password step on request", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ mfa: MFA }));
    await signIn(user);
    await user.click(await screen.findByRole("button", { name: "Back to sign in" }));
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
  });

  it("tells a person whose organization needs enrolment to do it through the API", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture({ enroll: true }));
    await signIn(user);
    expect(await screen.findByTestId("login-failure")).toHaveTextContent(/second step/i);
    expect(screen.getByTestId("login-failure")).not.toHaveTextContent("/v1/auth/login/mfa/enroll");
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
  });

  it("does not change a sign-in without MFA", async () => {
    const user = userEvent.setup();
    renderAt("/login", fixture());
    await signIn(user);
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Enter your code" })).not.toBeInTheDocument();
  });

  it("with the real server client: a pending answer shows the code screen (no TypeError), then the code signs in", async () => {
    const USER = { id: "human:u_0a1b2c3d4e5f60718293", role: "owner", email: ACCOUNT.email, display_name: "Ada" };
    const json = (status: number, body: unknown): Response =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } });
    let signedIn = false;
    const bodies: unknown[] = [];
    const fetchFn = vi.fn((input: string, init?: RequestInit): Promise<Response> => {
      const key = `${init?.method ?? "GET"} ${new URL(input, "http://127.0.0.1:5173").pathname}`;
      if (key === "GET /v1/auth/session")
        return Promise.resolve(signedIn ? json(200, { user: USER, session: { id: "s" }, csrf_token: "c" }) : json(401, { title: "session_invalid", status: 401, detail: "x" }));
      if (key === "POST /v1/auth/refresh") return Promise.resolve(json(401, { title: "session_invalid", status: 401, detail: "x" }));
      if (key === "POST /v1/auth/login")
        return Promise.resolve(json(200, { status: "mfa_required", challenge: "nmc_obviously-fake-challenge", methods: ["totp", "recovery_code"], expires_in: 300 }));
      if (key === "POST /v1/auth/login/mfa") {
        bodies.push(JSON.parse(init?.body as string));
        signedIn = true;
        return Promise.resolve(json(200, { user: USER, session: { id: "s" }, csrf_token: "c" }));
      }
      return Promise.resolve(json(404, { title: "not_found", status: 404, detail: "x" }));
    });
    const client = createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } });
    renderAt("/login", client);
    const user = userEvent.setup();
    await signIn(user);
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "246810");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(bodies).toEqual([{ challenge: "nmc_obviously-fake-challenge", code: "246810" }]);
  });
});

describe("a second step (MFA) refused by the real server client", () => {
  const json = (status: number, body: unknown, headers: Record<string, string> = {}): Response =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json", ...headers } });
  function serverFor(mfaAnswer: () => Response) {
    const fetchFn = vi.fn((input: string, init?: RequestInit): Promise<Response> => {
      const key = `${init?.method ?? "GET"} ${new URL(input, "http://127.0.0.1:5173").pathname}`;
      if (key === "GET /v1/auth/session" || key === "POST /v1/auth/refresh") return Promise.resolve(json(401, { title: "session_invalid", status: 401, detail: "x" }));
      if (key === "POST /v1/auth/login")
        return Promise.resolve(json(200, { status: "mfa_required", challenge: "nmc_obviously-fake-challenge", methods: ["totp", "recovery_code"], expires_in: 300 }));
      if (key === "POST /v1/auth/login/mfa") return Promise.resolve(mfaAnswer());
      return Promise.resolve(json(404, { title: "not_found", status: 404, detail: "x" }));
    });
    return createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } });
  }

  it("a used-up or expired challenge (401 challenge_invalid) goes back to the password step with the AU-5 timeout text", async () => {
    const user = userEvent.setup();
    renderAt("/login", serverFor(() => json(401, { title: "challenge_invalid", status: 401, detail: "x" })));
    await signIn(user);
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "246810");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
    expect(screen.getByTestId("login-failure")).toHaveTextContent("That sign-in step timed out. Start again.");
  });

  it("a 429 with Retry-After on the code step shows a countdown and disables Verify", async () => {
    const user = userEvent.setup();
    renderAt("/login", serverFor(() => json(429, { title: "too_many_attempts", status: 429, detail: "x" }, { "Retry-After": "30" })));
    await signIn(user);
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "246810");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByTestId("login-wait")).toHaveTextContent(/Try again in/);
    await user.type(screen.getByLabelText("Code from your authenticator app"), "111111");
    expect(screen.getByRole("button", { name: "Verify" })).toBeDisabled();
  });

  it("a password step that succeeds clears an earlier wait (no 'try again now' on the code screen)", async () => {
    const user = userEvent.setup();
    let calls = 0;
    const fetchFn = vi.fn((input: string, init?: RequestInit): Promise<Response> => {
      const key = `${init?.method ?? "GET"} ${new URL(input, "http://127.0.0.1:5173").pathname}`;
      if (key === "GET /v1/auth/session" || key === "POST /v1/auth/refresh") return Promise.resolve(json(401, { title: "session_invalid", status: 401, detail: "x" }));
      if (key === "POST /v1/auth/login") {
        calls += 1;
        return Promise.resolve(
          calls === 1
            ? json(429, { title: "too_many_attempts", status: 429, detail: "x" }, { "Retry-After": "1" })
            : json(200, { status: "mfa_required", challenge: "nmc_obviously-fake-challenge", methods: ["totp"], expires_in: 300 }),
        );
      }
      return Promise.resolve(json(404, { title: "not_found", status: 404, detail: "x" }));
    });
    renderAt("/login", createServerClient({ fetch: fetchFn, socket: () => { throw new Error("no sockets"); } }));
    await signIn(user);
    await screen.findByTestId("login-wait");
    await waitFor(() => { expect(screen.getByTestId("login-wait")).toHaveTextContent("You can try again now."); }, { timeout: 3000 });
    await user.type(screen.getByLabelText("Password"), ACCOUNT.password);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Enter your code" })).toBeInTheDocument();
    expect(screen.queryByTestId("login-wait")).not.toBeInTheDocument();
  });
});

describe("sign out failure", () => {
  it("shows a visible error when sign out fails", async () => {
    const user = userEvent.setup();
    const client = fixture({ signedIn: true });
    const auth = client.auth;
    if (auth === undefined) throw new Error("fixture has auth");
    vi.spyOn(auth, "logout").mockRejectedValue(new ProblemError("The server is busy.", 503, "unavailable"));
    renderAt("/runs", client);
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't sign out");
    expect(alert.className).not.toMatch(/sr-only/);
  });
});

describe("another tab changes the person", () => {
  it("drops the rendered name and the cached data at once", async () => {
    const listeners = new Set<() => void>();
    let state: AuthState = { kind: "signed-in", user: { id: "u1", orgId: "acme", role: "owner", email: "ada@example.org", displayName: "Ada Lovelace" } };
    const set = (next: AuthState): void => {
      state = next;
      for (const l of [...listeners]) l();
    };
    const auth: AuthApi = {
      state: () => state,
      subscribe: (l) => {
        listeners.add(l);
        return () => {
          listeners.delete(l);
        };
      },
      load: () => Promise.resolve(),
      login: () => Promise.resolve({ kind: "signed-in" as const }),
      completeMfa: () => Promise.resolve(),
      logout: () => Promise.resolve(),
      redeemSetup: () => Promise.resolve(),
      setupStatus: () => Promise.resolve(false),
    };
    const cleared = vi.spyOn(QueryClient.prototype, "clear");
    renderAt("/runs", { ...createFixtureClient(), auth });
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    act(() => {
      set({ kind: "checking" }); // the signal from the other tab
    });
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
    expect(cleared).toHaveBeenCalled(); // the cached queries are gone
    act(() => {
      set({ kind: "signed-in", user: { id: "u2", orgId: "acme", role: "viewer", email: "grace@example.org", displayName: "Grace Hopper" } });
    });
    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
  });
});

describe("two real tabs sharing one browser session", () => {
  const ADA = { id: "human:u_ada", role: "owner", email: "ada@example.org", display_name: "Ada Lovelace" };
  const GRACE = { id: "human:u_grace", role: "viewer", email: "grace@example.org", display_name: "Grace Hopper" };
  const json = (status: number, body: unknown): Response =>
    new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } });
  const invalid = (): Response => json(401, { title: "session_invalid", status: 401, detail: "x" });

  /** Two endpoints of one channel: what one notifies, only the other hears (as BroadcastChannel does). */
  function pairedSync() {
    const heard: ((() => void) | null)[] = [null, null];
    const end = (me: 0 | 1) => ({
      notify: () => {
        heard[me === 0 ? 1 : 0]?.();
      },
      listen: (onChange: () => void) => {
        heard[me] = onChange;
        return () => {
          heard[me] = null;
        };
      },
    });
    return [end(0), end(1)] as const;
  }

  /** The "server": whoever the browser's cookie belongs to, shared by both tabs. */
  function network(cookie: { who: typeof ADA | null }) {
    return vi.fn((input: string, init?: RequestInit): Promise<Response> => {
      const path = new URL(input, "http://127.0.0.1:5173").pathname;
      const key = `${init?.method ?? "GET"} ${path}`;
      if (key === "GET /v1/auth/session") return Promise.resolve(cookie.who === null ? invalid() : json(200, { user: cookie.who, session: { id: "s" }, csrf_token: "c" }));
      if (key === "POST /v1/auth/refresh") return Promise.resolve(invalid());
      if (key === "POST /v1/auth/logout") {
        cookie.who = null;
        return Promise.resolve(json(204, null));
      }
      if (key === "GET /v1/runs") return Promise.resolve(cookie.who === null ? invalid() : json(200, { runs: [], truncated: false }));
      return Promise.resolve(json(404, { title: "not_found", status: 404, detail: "x" }));
    });
  }
  const noSocket = (): never => {
    throw new Error("no sockets");
  };

  it("tab A signs out: tab B drops to the login at once, the old name is gone, its cache is cleared", async () => {
    const cookie: { who: typeof ADA | null } = { who: ADA };
    const [syncA, syncB] = pairedSync();
    const fetchFn = network(cookie);
    const tabA = createServerClient({ fetch: fetchFn, socket: noSocket, tabSync: syncA });
    const tabB = createServerClient({ fetch: fetchFn, socket: noSocket, tabSync: syncB });
    await tabA.auth?.load();
    const cleared = vi.spyOn(QueryClient.prototype, "clear");
    const router = renderAt("/runs", tabB);
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    cleared.mockClear();
    await act(async () => {
      await tabA.auth?.logout();
    });
    expect(await screen.findByRole("heading", { name: "Sign in to NuraMesh" })).toBeInTheDocument();
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
    expect(cleared).toHaveBeenCalled();
    expect(router.state.location.pathname).toBe("/login");
    expect(screen.getByText("Your session ended. Sign in again to carry on.")).toBeInTheDocument();
  });

  it("tab A switches to another person: tab B never shows Ada again, only Grace", async () => {
    const cookie: { who: typeof ADA | null } = { who: ADA };
    const [syncA, syncB] = pairedSync();
    const fetchFn = network(cookie);
    const tabA = createServerClient({ fetch: fetchFn, socket: noSocket, tabSync: syncA });
    const tabB = createServerClient({ fetch: fetchFn, socket: noSocket, tabSync: syncB });
    await tabA.auth?.load();
    renderAt("/runs", tabB);
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    const names: string[] = [];
    const watch = new MutationObserver(() => {
      if (screen.queryByText("Ada Lovelace") !== null) names.push("Ada");
    });
    watch.observe(document.body, { childList: true, subtree: true, characterData: true });
    cookie.who = GRACE; // the browser's cookie now belongs to Grace
    await act(async () => {
      await tabA.auth?.load();
      syncA.notify(); // what a login in tab A announces
    });
    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    watch.disconnect();
    expect(names).toEqual([]);
    expect(screen.queryByText("Ada Lovelace")).not.toBeInTheDocument();
  });
});
