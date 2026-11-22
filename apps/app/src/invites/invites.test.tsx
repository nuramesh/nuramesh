import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConflictError, ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { InviteInspection, InvitesApi } from "@/api/invites";
import { createFixtureAuth } from "@/auth/fixture-auth";
import { renderAt, serverLike } from "@/test/server-like";

import { resetCapturedInviteToken } from "./invite-token";

// Obviously fake: a repeated character, never a real token.
const TOKEN = `nmi_${"A".repeat(43)}`;
const ACCOUNT = { email: "owner@example.org", password: "obviously-fake-test-password", role: "owner" as const };
const PASSWORD = "obviously-fake-test-password";
const INSPECTION: InviteInspection = { email: "ada@example.org", role: "approver", organization: "Demo team", expiresAt: "2026-11-05T12:00:00Z" };

function ownerFixture() {
  return createFixtureClient({ signIn: { account: ACCOUNT, signedIn: true } });
}

function openLink(hash: string): void {
  globalThis.history.replaceState(null, "", `/accept-invite${hash}`);
}

beforeEach(() => {
  resetCapturedInviteToken();
});
afterEach(() => {
  vi.restoreAllMocks();
  globalThis.history.replaceState(null, "", "/");
});

describe("People: invite list (owner)", () => {
  it("creates an invitation and shows it pending with no token or link anywhere", async () => {
    const user = userEvent.setup();
    renderAt("/settings", ownerFixture());
    const panel = await screen.findByRole("region", { name: "People" });
    await user.type(within(panel).getByLabelText("Email"), "Ada@Example.org");
    await user.selectOptions(within(panel).getByLabelText("Role"), "viewer");
    await user.click(within(panel).getByRole("button", { name: "Send invitation" }));
    const row = await within(panel).findByRole("row", { name: /ada@example\.org/ });
    expect(row).toHaveTextContent("viewer");
    expect(row).toHaveTextContent("pending");
    expect(panel.textContent).not.toMatch(/nmi_|#t=|token/i);
    expect(within(panel).getByLabelText("Email")).toHaveValue("");
  });

  it("offers only approver and viewer, never owner", async () => {
    renderAt("/settings", ownerFixture());
    const panel = await screen.findByRole("region", { name: "People" });
    const options = within(within(panel).getByLabelText("Role")).getAllByRole("option").map((o) => o.getAttribute("value"));
    expect(options).toEqual(["approver", "viewer"]);
  });

  it("revokes and resends a pending invitation; an ended one has no buttons", async () => {
    const user = userEvent.setup();
    renderAt("/settings", ownerFixture());
    const panel = await screen.findByRole("region", { name: "People" });
    for (const email of ["a@example.org", "b@example.org"]) {
      await user.type(within(panel).getByLabelText("Email"), email);
      await user.click(within(panel).getByRole("button", { name: "Send invitation" }));
      await within(panel).findByRole("row", { name: new RegExp(email.replace(".", "\\.")) });
    }
    const first = within(panel).getByRole("row", { name: /a@example\.org/ });
    await user.click(within(first).getByRole("button", { name: "Send again" }));
    await waitFor(() => { expect(within(panel).getByRole("row", { name: /a@example\.org/ })).toHaveTextContent("sent again 1"); });
    await user.click(within(within(panel).getByRole("row", { name: /a@example\.org/ })).getByRole("button", { name: "Revoke" }));
    await waitFor(() => { expect(within(panel).getByRole("row", { name: /a@example\.org/ })).toHaveTextContent("revoked"); });
    const ended = within(panel).getByRole("row", { name: /a@example\.org/ });
    expect(within(ended).queryByRole("button")).not.toBeInTheDocument();
    expect(within(within(panel).getByRole("row", { name: /b@example\.org/ })).getAllByRole("button")).toHaveLength(2);
  });

  it("shows the server's text in a banner when email is off (503), and keeps the typed address", async () => {
    const user = userEvent.setup();
    const invites: InvitesApi = {
      list: () => Promise.resolve([]),
      create: () => Promise.reject(new ProblemError("email is not set up on this server: set NURAMESH_SMTP_HOST", 503, "email_not_configured")),
      revoke: () => Promise.reject(new Error("unused")),
      resend: () => Promise.reject(new Error("unused")),
      inspect: () => Promise.reject(new Error("unused")),
      accept: () => Promise.reject(new Error("unused")),
    };
    renderAt("/settings", { ...ownerFixture(), invites });
    const panel = await screen.findByRole("region", { name: "People" });
    await user.type(within(panel).getByLabelText("Email"), "ada@example.org");
    await user.click(within(panel).getByRole("button", { name: "Send invitation" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("email is not set up on this server: set NURAMESH_SMTP_HOST");
    expect(within(panel).getByLabelText("Email")).toHaveValue("ada@example.org");
  });

  it("explains a conflict in the server's words", async () => {
    const user = userEvent.setup();
    const invites: InvitesApi = {
      list: () => Promise.resolve([]),
      create: () => Promise.reject(new ConflictError("a user with this address already exists", "already_exists")),
      revoke: () => Promise.reject(new Error("unused")),
      resend: () => Promise.reject(new Error("unused")),
      inspect: () => Promise.reject(new Error("unused")),
      accept: () => Promise.reject(new Error("unused")),
    };
    renderAt("/settings", { ...ownerFixture(), invites });
    const panel = await screen.findByRole("region", { name: "People" });
    await user.type(within(panel).getByLabelText("Email"), "ada@example.org");
    await user.click(within(panel).getByRole("button", { name: "Send invitation" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("a user with this address already exists");
  });

  it("is not shown to an approver or on sample data without sign-in", async () => {
    renderAt("/settings", createFixtureClient({ signIn: { account: { ...ACCOUNT, role: "approver" }, signedIn: true } }));
    await screen.findByRole("heading", { level: 1, name: "Settings" });
    expect(screen.queryByRole("region", { name: "People" })).not.toBeInTheDocument();
  });
});

describe("Accept invitation page", () => {
  function clientWith(parts: Partial<InvitesApi>) {
    return serverLike({
      auth: createFixtureAuth({ account: ACCOUNT }),
      invites: {
        inspect: () => Promise.resolve(INSPECTION),
        accept: () => Promise.resolve({ id: "u_1", role: "approver", email: "ada@example.org" }),
        ...parts,
      },
    });
  }

  it("A-I1: removes the fragment from the address before any request, then shows the email and role", async () => {
    openLink(`#t=${TOKEN}`);
    let hashAtRequest: string | null = null;
    const inspect = vi.fn((token: string) => {
      hashAtRequest = globalThis.location.hash;
      expect(token).toBe(TOKEN);
      return Promise.resolve(INSPECTION);
    });
    renderAt("/accept-invite", clientWith({ inspect }));
    expect(await screen.findByText("ada@example.org")).toBeInTheDocument();
    expect(hashAtRequest).toBe("");
    expect(globalThis.location.href).not.toContain("nmi_");
    expect(screen.getByText(/approver/)).toBeInTheDocument();
    expect(screen.getByText(/Demo team/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Password/, { selector: "#invite-password" })).toBeInTheDocument();
    expect(screen.getByText(/At least 12 characters/)).toBeInTheDocument();
  });

  it("A-I2: a 403 invalid_invite shows one generic page with no form", async () => {
    openLink(`#t=${TOKEN}`);
    const inspect = () => Promise.reject(new ProblemError("this invitation is not valid", 403, "invalid_invite"));
    renderAt("/accept-invite", clientWith({ inspect }));
    expect(await screen.findByRole("heading", { name: "This invitation can't be used" })).toBeInTheDocument();
    expect(screen.getByText(/ask the person who invited you for a new one/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Password/)).not.toBeInTheDocument();
  });

  it("A-I2: a link with no fragment is the same generic page and asks the server nothing", async () => {
    openLink("");
    const inspect = vi.fn(() => Promise.resolve(INSPECTION));
    renderAt("/accept-invite", clientWith({ inspect }));
    expect(await screen.findByRole("heading", { name: "This invitation can't be used" })).toBeInTheDocument();
    expect(inspect).not.toHaveBeenCalled();
  });

  it("accepts with the typed password and name, then goes to sign-in", async () => {
    const user = userEvent.setup();
    openLink(`#t=${TOKEN}`);
    const accept = vi.fn(() => Promise.resolve({ id: "u_1", role: "approver" as const, email: "ada@example.org" }));
    const router = renderAt("/accept-invite", clientWith({ accept }));
    await screen.findByText("ada@example.org");
    await user.type(screen.getByLabelText("Name (optional)"), "Ada Lovelace");
    await user.type(screen.getByLabelText("Password", { selector: "#invite-password" }), PASSWORD);
    await user.type(screen.getByLabelText("Password again"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create my account" }));
    await waitFor(() => { expect(router.state.location.pathname).toBe("/login"); });
    expect(accept).toHaveBeenCalledWith({ token: TOKEN, password: PASSWORD, displayName: "Ada Lovelace" });
    expect(router.state.location.search + router.state.location.hash).toBe("");
    expect((router.state.location.state as { inviteDone?: boolean } | null)?.inviteDone).toBe(true);
  });

  it("keeps the button off for a short password and when the two differ", async () => {
    const user = userEvent.setup();
    openLink(`#t=${TOKEN}`);
    const accept = vi.fn(() => Promise.resolve({ id: "u_1", role: "approver" as const, email: "ada@example.org" }));
    renderAt("/accept-invite", clientWith({ accept }));
    await screen.findByText("ada@example.org");
    const button = screen.getByRole("button", { name: "Create my account" });
    await user.type(screen.getByLabelText("Password", { selector: "#invite-password" }), "short");
    expect(button).toBeDisabled();
    expect(screen.getByText(/shorter than 12/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Password", { selector: "#invite-password" }));
    await user.type(screen.getByLabelText("Password", { selector: "#invite-password" }), PASSWORD);
    await user.type(screen.getByLabelText("Password again"), "different-password-here");
    expect(button).toBeDisabled();
    expect(accept).not.toHaveBeenCalled();
  });

  it("a failed accept shows the reason and keeps the form; a 403 at accept turns into the generic page", async () => {
    const user = userEvent.setup();
    openLink(`#t=${TOKEN}`);
    const accept = vi
      .fn()
      .mockRejectedValueOnce(new ConflictError("a user with this address already exists", "already_exists"))
      .mockRejectedValueOnce(new ProblemError("this invitation is not valid", 403, "invalid_invite"));
    renderAt("/accept-invite", clientWith({ accept }));
    await screen.findByText("ada@example.org");
    await user.type(screen.getByLabelText("Password", { selector: "#invite-password" }), PASSWORD);
    await user.type(screen.getByLabelText("Password again"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create my account" }));
    expect(await screen.findByTestId("invite-failure")).toHaveTextContent("already exists");
    await user.click(screen.getByRole("button", { name: "Create my account" }));
    expect(await screen.findByRole("heading", { name: "This invitation can't be used" })).toBeInTheDocument();
  });

  it("A-I3: the token is never put in a URL, a storage API or a console message", async () => {
    const user = userEvent.setup();
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
    const router = renderAt("/accept-invite", clientWith({}));
    await screen.findByText("ada@example.org");
    await user.type(screen.getByLabelText("Password", { selector: "#invite-password" }), PASSWORD);
    await user.type(screen.getByLabelText("Password again"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create my account" }));
    await waitFor(() => { expect(router.state.location.pathname).toBe("/login"); });
    expect(pushes.join("\n")).not.toContain(TOKEN);
    expect(setItem.mock.calls.flat().join("\n")).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain(TOKEN);
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(TOKEN);
    expect(sink.join("\n")).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(globalThis.location.href).not.toContain(TOKEN);
  });
});

describe("sign-in after accepting", () => {
  it("tells the new person their account is ready", async () => {
    const router = renderAt("/login", createFixtureClient({ signIn: { account: ACCOUNT } }));
    await screen.findByRole("heading", { name: "Sign in to NuraMesh" });
    expect(screen.queryByText(/Your account is ready/)).not.toBeInTheDocument();
    void router.navigate("/login", { state: { inviteDone: true }, replace: true });
    expect(await screen.findByText(/Your account is ready. Sign in with it./)).toBeInTheDocument();
  });
});
