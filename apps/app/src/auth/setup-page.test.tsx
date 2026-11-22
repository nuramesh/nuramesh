import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { renderAt } from "@/test/server-like";

const ACCOUNT = { email: "ada@example.org", password: "obviously-fake-test-password", displayName: "Ada Lovelace" };
const TOKEN = "obviously-fake-setup-token";

function fixture() {
  return createFixtureClient({ signIn: { account: ACCOUNT, setupToken: TOKEN } });
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

// Case S0-44 of spec/rbac/solo-approval.md.
describe("the setup page and the one-time link (S0-44)", () => {
  it("reads the token from the fragment, removes the fragment and fills the token field", async () => {
    window.history.replaceState(null, "", `/setup#token=${TOKEN}`);
    renderAt("/setup", fixture());
    const field = await screen.findByLabelText("Setup token");
    expect(field).toHaveValue(TOKEN);
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain(TOKEN);
  });

  it("shows the typed-token field, empty, when there is no fragment", async () => {
    renderAt("/setup", fixture());
    expect(await screen.findByLabelText("Setup token")).toHaveValue("");
  });

  it("ignores a fragment that holds no token", async () => {
    window.history.replaceState(null, "", "/setup#other=1");
    renderAt("/setup", fixture());
    expect(await screen.findByLabelText("Setup token")).toHaveValue("");
  });

  it("tells the person how long a token lasts, for both ways of getting one", async () => {
    renderAt("/setup", fixture());
    await screen.findByLabelText("Setup token");
    expect(
      screen.getByText("It works once: 15 minutes (from serve), or 24 hours (from new-setup-token)."),
    ).toBeInTheDocument();
  });

  it("makes the owner from a token that came in the link", async () => {
    window.history.replaceState(null, "", `/setup#token=${TOKEN}`);
    const user = userEvent.setup();
    const router = renderAt("/setup", fixture());
    await user.type(await screen.findByLabelText("Email"), ACCOUNT.email);
    await user.type(screen.getByLabelText("Password"), ACCOUNT.password);
    await user.type(screen.getByLabelText("Password again"), ACCOUNT.password);
    await user.click(screen.getByRole("button", { name: "Create owner account" }));
    expect(await screen.findByText("The owner account is ready. Sign in with it.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  const REFUSED =
    "That setup token can't be used. It may be wrong, already used or expired (15 minutes from serve, 24 hours from new-setup-token), or an owner already exists. Restart serve, or make a new one on the server with: nuramesh auth new-setup-token <team>";

  async function submit(user: ReturnType<typeof userEvent.setup>, token: string | null) {
    if (token !== null) await user.type(await screen.findByLabelText("Setup token"), token);
    await user.type(await screen.findByLabelText("Email"), ACCOUNT.email);
    await user.type(screen.getByLabelText("Password"), ACCOUNT.password);
    await user.type(screen.getByLabelText("Password again"), ACCOUNT.password);
    await user.click(screen.getByRole("button", { name: "Create owner account" }));
  }

  it("says the same plain thing, with both lifetimes and the fix, for a wrong token", async () => {
    const user = userEvent.setup();
    const router = renderAt("/setup", fixture());
    await submit(user, "obviously-fake-wrong-token");
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/setup");
  });

  it("says it for a used token (the second redeem of the same token)", async () => {
    const user = userEvent.setup();
    const client = fixture();
    await client.auth?.redeemSetup({ token: TOKEN, email: ACCOUNT.email, password: ACCOUNT.password, displayName: ACCOUNT.displayName });
    renderAt("/setup", client);
    await submit(user, TOKEN);
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
  });

  it("says it for an expired token (the server holds none any more)", async () => {
    const user = userEvent.setup();
    renderAt("/setup", createFixtureClient({ signIn: { account: ACCOUNT } }));
    await submit(user, TOKEN);
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
  });

  it("never renders or logs the token outside the input", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m));
    window.history.replaceState(null, "", `/setup#token=${TOKEN}`);
    const user = userEvent.setup();
    renderAt("/setup", fixture());
    expect(await screen.findByLabelText("Setup token")).toHaveValue(TOKEN);
    expect(document.body.textContent).not.toContain(TOKEN);
    await submit(user, null);
    expect(await screen.findByText("The owner account is ready. Sign in with it.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(TOKEN);
    for (const spy of spies) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(TOKEN);
      spy.mockRestore();
    }
  });
});
