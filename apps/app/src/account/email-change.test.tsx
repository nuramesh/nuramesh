import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { renderAt } from "@/test/server-like";

import { forgetEmailToken } from "./email-token";

// Obviously fake: a repeated character, never a real token.
const TOKEN = `nme_${"A".repeat(43)}`;
const ACCOUNT = { email: "owner@example.org", password: "obviously-fake-test-password", role: "owner" as const };
const PASSWORD = "obviously-fake-test-password";

function signedIn() {
  return createFixtureClient({ signIn: { account: ACCOUNT, signedIn: true } });
}

function openLink(hash: string): void {
  globalThis.history.replaceState(null, "", `/verify-email${hash}`);
}

beforeEach(() => {
  forgetEmailToken();
});
afterEach(() => {
  vi.restoreAllMocks();
  globalThis.history.replaceState(null, "", "/");
});

describe("A-V1: account settings, change email", () => {
  function withStart(start: (input: { newEmail: string; password: string }) => Promise<void>) {
    const base = signedIn();
    return { ...base, emailChange: { ...base.emailChange, start } };
  }

  async function fill(user: ReturnType<typeof userEvent.setup>, email: string): Promise<void> {
    await user.type(await screen.findByLabelText("New email address"), email);
    await user.type(screen.getByLabelText("Current password"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Send confirmation" }));
  }

  it("sends the typed address and password, then always ends on 'check your new inbox' and clears the form", async () => {
    const user = userEvent.setup();
    const start = vi.fn(() => Promise.resolve());
    renderAt("/settings", withStart(start));
    await fill(user, " ada.new@example.org ");
    expect(await screen.findByText(/Check your new inbox/)).toBeInTheDocument();
    expect(start).toHaveBeenCalledWith({ newEmail: "ada.new@example.org", password: PASSWORD });
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New email address")).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(PASSWORD);
  });

  it("keeps the button off until both fields are filled", async () => {
    const user = userEvent.setup();
    const start = vi.fn(() => Promise.resolve());
    renderAt("/settings", withStart(start));
    const button = await screen.findByRole("button", { name: "Send confirmation" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText("New email address"), "ada.new@example.org");
    expect(button).toBeDisabled();
    expect(start).not.toHaveBeenCalled();
  });

  it("a wrong password (401) and the same address (409) show a plain reason and no 'check your inbox'", async () => {
    const user = userEvent.setup();
    const start = vi
      .fn()
      .mockRejectedValueOnce(new ProblemError("the email or password is not right", 401, "invalid_credentials"))
      .mockRejectedValueOnce(new ProblemError("that is already your address", 409, "same_email"));
    renderAt("/settings", withStart(start));
    await fill(user, "ada.new@example.org");
    expect(await screen.findByTestId("email-change-failure")).toHaveTextContent("password is not right");
    await user.click(screen.getByRole("button", { name: "Send confirmation" }));
    await waitFor(() => { expect(screen.getByTestId("email-change-failure")).toHaveTextContent("already your email address"); });
    expect(screen.queryByText(/Check your new inbox/)).not.toBeInTheDocument();
  });

  it("a rate limit shows the wait; a mail-off 503 shows the server's text", async () => {
    const user = userEvent.setup();
    const start = vi
      .fn()
      .mockRejectedValueOnce(new ProblemError("too many requests", 429, "rate_limited", 90))
      .mockRejectedValueOnce(new ProblemError("email is not configured", 503, "email_not_configured"));
    renderAt("/settings", withStart(start));
    await fill(user, "ada.new@example.org");
    expect(await screen.findByTestId("email-change-failure")).toHaveTextContent("1 minute 30 seconds");
    await user.click(screen.getByRole("button", { name: "Send confirmation" }));
    await waitFor(() => { expect(screen.getByTestId("email-change-failure")).toHaveTextContent("email is not configured"); });
  });
});

describe("A-V2: /verify-email", () => {
  function clientWith(verify: (token: string) => Promise<void>) {
    const base = signedIn();
    return { ...base, emailChange: { ...base.emailChange, verify } };
  }

  it("reads and clears #t= before any request, posts it once, and shows success", async () => {
    openLink(`#t=${TOKEN}`);
    let hashAtRequest: string | null = null;
    const verify = vi.fn((token: string) => {
      hashAtRequest = globalThis.location.hash;
      expect(token).toBe(TOKEN);
      return Promise.resolve();
    });
    renderAt("/verify-email", clientWith(verify));
    expect(await screen.findByRole("heading", { name: "Your email address is changed" })).toBeInTheDocument();
    expect(hashAtRequest).toBe("");
    expect(verify).toHaveBeenCalledTimes(1);
    expect(globalThis.location.href).not.toContain("nme_");
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("a 403 shows one generic page", async () => {
    openLink(`#t=${TOKEN}`);
    renderAt("/verify-email", clientWith(() => Promise.reject(new ProblemError("this link is not valid", 403, "invalid_verification_token"))));
    expect(await screen.findByRole("heading", { name: "This link can't be used" })).toBeInTheDocument();
  });

  it("a link with no fragment, or the wrong prefix, is the same page and asks the server nothing", async () => {
    for (const hash of ["", "#t=nmi_" + "A".repeat(43), "#x=1"]) {
      forgetEmailToken();
      openLink(hash);
      const verify = vi.fn(() => Promise.resolve());
      const view = renderAt("/verify-email", clientWith(verify));
      expect(await screen.findByRole("heading", { name: "This link can't be used" })).toBeInTheDocument();
      expect(verify).not.toHaveBeenCalled();
      expect(globalThis.location.hash).toBe("");
      view.dispose();
      document.body.innerHTML = "";
    }
  });

  it("a 409 shows 'that address is no longer available'", async () => {
    openLink(`#t=${TOKEN}`);
    renderAt("/verify-email", clientWith(() => Promise.reject(new ProblemError("that address is no longer available", 409, "already_exists"))));
    expect(await screen.findByRole("heading", { name: "That address is no longer available" })).toBeInTheDocument();
  });

  it("the token is never put in a URL, a storage API or a console message", async () => {
    openLink(`#t=${TOKEN}`);
    const sink: string[] = [];
    for (const level of ["log", "info", "warn", "error", "debug"] as const) {
      vi.spyOn(console, level).mockImplementation((...args: unknown[]) => { sink.push(args.map(String).join(" ")); });
    }
    const pushes: string[] = [];
    for (const method of ["pushState", "replaceState"] as const) {
      const original = globalThis.history[method].bind(globalThis.history);
      vi.spyOn(globalThis.history, method).mockImplementation((data: unknown, unused: string, url?: string | URL | null) => {
        pushes.push(String(url ?? ""));
        original(data, unused, url);
      });
    }
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    renderAt("/verify-email", clientWith(() => Promise.resolve()));
    await screen.findByRole("heading", { name: "Your email address is changed" });
    expect(pushes.join("\n")).not.toContain(TOKEN);
    expect(setItem.mock.calls.flat().join("\n")).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(TOKEN);
    expect(sink.join("\n")).not.toContain(TOKEN);
    expect(globalThis.location.href).not.toContain(TOKEN);
  });
});
