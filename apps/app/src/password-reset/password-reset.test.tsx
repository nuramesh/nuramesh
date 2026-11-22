import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { PasswordResetApi } from "@/api/password-reset";
import { createFixtureAuth } from "@/auth/fixture-auth";
import { renderAt, serverLike } from "@/test/server-like";

import { resetLinkToken, revokeLinkToken } from "./fragment-token";

// Obviously fake: a repeated character, never a real token.
const RESET_TOKEN = `nmr_${"A".repeat(43)}`;
const REVOKE_TOKEN = `nmk_${"B".repeat(43)}`;
const PASSWORD = "obviously-fake-test-password";
const ACCOUNT = { email: "owner@example.org", password: PASSWORD, role: "owner" as const };

function clientWith(parts: Partial<PasswordResetApi>) {
  return serverLike({
    auth: createFixtureAuth({ account: ACCOUNT }),
    passwordReset: {
      request: () => Promise.resolve(),
      redeem: () => Promise.resolve(),
      revokeByLink: () => Promise.resolve(),
      ...parts,
    },
  });
}

function openLink(path: string, hash: string): void {
  globalThis.history.replaceState(null, "", `${path}${hash}`);
}

beforeEach(() => {
  resetLinkToken.forget();
  revokeLinkToken.forget();
});
afterEach(() => {
  vi.restoreAllMocks();
  globalThis.history.replaceState(null, "", "/");
});

function spyOnLeaks(): { sink: string[]; pushes: string[]; setItem: MockInstance<(key: string, value: string) => void> } {
  const sink: string[] = [];
  for (const level of ["log", "info", "warn", "error", "debug"] as const) {
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
      sink.push(args.map(String).join(" "));
    });
  }
  const pushes: string[] = [];
  for (const method of ["pushState", "replaceState"] as const) {
    const original = globalThis.history[method].bind(globalThis.history);
    vi.spyOn(globalThis.history, method).mockImplementation((data: unknown, unused: string, url?: string | URL | null) => {
      pushes.push(String(url ?? ""));
      original(data, unused, url);
    });
  }
  return { sink, pushes, setItem: vi.spyOn(Storage.prototype, "setItem") };
}

function expectNoLeak(token: string, spies: ReturnType<typeof spyOnLeaks>): void {
  expect(spies.pushes.join("\n")).not.toContain(token);
  expect(spies.setItem.mock.calls.flat().join("\n")).not.toContain(token);
  expect(JSON.stringify(Object.entries(localStorage))).not.toContain(token);
  expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(token);
  expect(spies.sink.join("\n")).not.toContain(token);
  expect(document.body.innerHTML).not.toContain(token);
  expect(globalThis.location.href).not.toContain(token);
}

describe("Forgot password page", () => {
  it("A-P1: shows the same sentence for every address", async () => {
    const user = userEvent.setup();
    const request = vi.fn(() => Promise.resolve());
    const seen: string[] = [];
    for (const address of ["ada@example.org", "nobody@example.org", "not-even-an-address"]) {
      renderAt("/forgot-password", clientWith({ request }));
      await user.type(await screen.findByLabelText("Email"), address);
      await user.click(screen.getByRole("button", { name: "Send the link" }));
      seen.push((await screen.findByTestId("forgot-sent")).textContent);
      cleanup();
    }
    expect(new Set(seen)).toEqual(new Set(["If that address has an account, a message is on its way."]));
    expect(request).toHaveBeenCalledTimes(3);
    expect(request).toHaveBeenNthCalledWith(1, "ada@example.org");
  });

  it("A-P1: the sent page never echoes the address", async () => {
    const user = userEvent.setup();
    renderAt("/forgot-password", clientWith({}));
    await user.type(await screen.findByLabelText("Email"), "ada@example.org");
    await user.click(screen.getByRole("button", { name: "Send the link" }));
    await screen.findByTestId("forgot-sent");
    expect(document.body.textContent).not.toContain("ada@example.org");
  });

  it("keeps the button off with no address and shows a rate limit plainly", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockRejectedValue(new ProblemError("too many requests", 429, "rate_limited"));
    renderAt("/forgot-password", clientWith({ request }));
    const button = await screen.findByRole("button", { name: "Send the link" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText("Email"), "ada@example.org");
    await user.click(button);
    expect(await screen.findByTestId("forgot-failure")).toBeInTheDocument();
    expect(screen.queryByTestId("forgot-sent")).not.toBeInTheDocument();
  });

  it("is reachable from the sign-in page", async () => {
    const user = userEvent.setup();
    const router = renderAt("/login", createFixtureClient({ signIn: { account: ACCOUNT, mailEnabled: true } }));
    await user.click(await screen.findByRole("link", { name: "Reset it by email" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/forgot-password");
    });
  });
});

describe("Reset password page", () => {
  async function fill(user: ReturnType<typeof userEvent.setup>, password: string, again: string) {
    await user.type(await screen.findByLabelText("New password", { selector: "#reset-password" }), password);
    await user.type(screen.getByLabelText("New password again"), again);
  }

  it("removes the fragment before any request, posts the typed password, then goes to sign-in", async () => {
    const user = userEvent.setup();
    openLink("/reset-password", `#t=${RESET_TOKEN}`);
    let hashAtRequest: string | null = null;
    const redeem = vi.fn(() => {
      hashAtRequest = globalThis.location.hash;
      return Promise.resolve();
    });
    const router = renderAt("/reset-password", clientWith({ redeem }));
    await fill(user, PASSWORD, PASSWORD);
    await user.click(screen.getByRole("button", { name: "Set the new password" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    expect(redeem).toHaveBeenCalledWith({ token: RESET_TOKEN, password: PASSWORD });
    expect(hashAtRequest).toBe("");
    expect((router.state.location.state as { resetDone?: boolean } | null)?.resetDone).toBe(true);
    expect(await screen.findByText(/Your password was changed/)).toBeInTheDocument();
  });

  it("A-P2: no fragment, a wrong shape and a 403 are all the same generic page", async () => {
    const user = userEvent.setup();
    const redeem = vi.fn().mockRejectedValue(new ProblemError("this link is not valid", 403, "invalid_reset_token"));
    const heading = { name: "This link can't be used" };
    openLink("/reset-password", "");
    renderAt("/reset-password", clientWith({ redeem }));
    expect(await screen.findByRole("heading", heading)).toBeInTheDocument();
    expect(redeem).not.toHaveBeenCalled();
    document.body.innerHTML = "";
    openLink("/reset-password", "#t=not-a-reset-token");
    renderAt("/reset-password", clientWith({ redeem }));
    expect(await screen.findByRole("heading", heading)).toBeInTheDocument();
    expect(redeem).not.toHaveBeenCalled();
    document.body.innerHTML = "";
    openLink("/reset-password", `#t=${RESET_TOKEN}`);
    renderAt("/reset-password", clientWith({ redeem }));
    await fill(user, PASSWORD, PASSWORD);
    await user.click(screen.getByRole("button", { name: "Set the new password" }));
    expect(await screen.findByRole("heading", heading)).toBeInTheDocument();
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
  });

  it("keeps the button off for a short password and when the two differ", async () => {
    const user = userEvent.setup();
    openLink("/reset-password", `#t=${RESET_TOKEN}`);
    const redeem = vi.fn(() => Promise.resolve());
    renderAt("/reset-password", clientWith({ redeem }));
    const button = await screen.findByRole("button", { name: "Set the new password" });
    await user.type(screen.getByLabelText("New password", { selector: "#reset-password" }), "short");
    expect(button).toBeDisabled();
    expect(screen.getByText(/shorter than 12/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("New password", { selector: "#reset-password" }));
    await user.type(screen.getByLabelText("New password", { selector: "#reset-password" }), PASSWORD);
    await user.type(screen.getByLabelText("New password again"), "different-password-here");
    expect(button).toBeDisabled();
    expect(redeem).not.toHaveBeenCalled();
  });

  it("A-P2: the token is never put in a URL, a storage API or a console message", async () => {
    const user = userEvent.setup();
    openLink("/reset-password", `#t=${RESET_TOKEN}`);
    const spies = spyOnLeaks();
    const router = renderAt("/reset-password", clientWith({}));
    await fill(user, PASSWORD, PASSWORD);
    await user.click(screen.getByRole("button", { name: "Set the new password" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    expectNoLeak(RESET_TOKEN, spies);
  });
});

describe("Revoke token page", () => {
  it("revokes on open, shows that the key was revoked, and clears the fragment first", async () => {
    openLink("/revoke-token", `#t=${REVOKE_TOKEN}`);
    let hashAtRequest: string | null = null;
    const revokeByLink = vi.fn(() => {
      hashAtRequest = globalThis.location.hash;
      return Promise.resolve();
    });
    renderAt("/revoke-token", clientWith({ revokeByLink }));
    expect(await screen.findByRole("heading", { name: "That key was revoked" })).toBeInTheDocument();
    expect(revokeByLink).toHaveBeenCalledWith(REVOKE_TOKEN);
    expect(revokeByLink).toHaveBeenCalledTimes(1);
    expect(hashAtRequest).toBe("");
  });

  it("A-P3: a 403 and a missing fragment are the same generic page", async () => {
    const revokeByLink = vi.fn().mockRejectedValue(new ProblemError("this link is not valid", 403, "invalid_revoke_link"));
    openLink("/revoke-token", `#t=${REVOKE_TOKEN}`);
    renderAt("/revoke-token", clientWith({ revokeByLink }));
    expect(await screen.findByRole("heading", { name: "This link can't be used" })).toBeInTheDocument();
    document.body.innerHTML = "";
    revokeByLink.mockClear();
    openLink("/revoke-token", "");
    renderAt("/revoke-token", clientWith({ revokeByLink }));
    expect(await screen.findByRole("heading", { name: "This link can't be used" })).toBeInTheDocument();
    expect(revokeByLink).not.toHaveBeenCalled();
  });

  it("A-P3: the token is never put in a URL, a storage API or a console message", async () => {
    openLink("/revoke-token", `#t=${REVOKE_TOKEN}`);
    const spies = spyOnLeaks();
    renderAt("/revoke-token", clientWith({}));
    await screen.findByRole("heading", { name: "That key was revoked" });
    expectNoLeak(REVOKE_TOKEN, spies);
  });
});
