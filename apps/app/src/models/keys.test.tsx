import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { InvalidRequestError, NotFoundError, ProblemError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { Connection } from "@/api/models";
import { renderAt, serverLike } from "@/test/server-like";

const LOAD_WAIT_MS = 20_000;
// Generated when the test runs, never a real or key-shaped value.
const FAKE_KEY = "obviously-fake-test-key-not-a-secret";

const ANTHROPIC: Connection = { provider: "anthropic", auth: "stored", endpoint: "https://api.anthropic.com", status: "active", setBy: "human:u_sample", setAt: "2026-10-24T12:00:00Z", seq: 3 };

function clientWith(role: "owner" | "viewer" = "owner", over: Partial<ApiClient["models"]> = {}): ApiClient {
  const base = createFixtureClient(role === "owner" ? {} : { role });
  return serverLike({ models: { ...base.models, connections: () => Promise.resolve([ANTHROPIC]), ...over }, session: base.session });
}

async function open(client: ApiClient = clientWith()) {
  const user = userEvent.setup();
  renderAt("/models/keys", client);
  await screen.findByRole("heading", { level: 1, name: "Models" }, { timeout: LOAD_WAIT_MS });
  return { user, client };
}

const row = async (): Promise<HTMLElement> => (await screen.findByRole("list", { name: "Stored keys" }, { timeout: LOAD_WAIT_MS })).querySelector("li") as HTMLElement;

describe("Keys tab", () => {
  it("is a section of Models, lists only connections that take a stored key, and starts with no key", async () => {
    await open();
    expect(within(screen.getByRole("navigation", { name: "Models sections" })).getByRole("link", { name: "Keys" })).toHaveAttribute("aria-current", "page");
    const item = await row();
    expect(item).toHaveTextContent("anthropic");
    await waitFor(() => { expect(item).toHaveTextContent("no key"); });
    expect(within(item).getByRole("button", { name: "Set key anthropic" })).toBeEnabled();
    expect(within(item).queryByRole("button", { name: /Revoke/ })).not.toBeInTheDocument();
  });

  it("says so when no connection takes a stored key", async () => {
    await open(clientWith("owner", { connections: () => Promise.resolve([{ ...ANTHROPIC, provider: "azure-openai" }]) }));
    expect(await screen.findByText(/No connection takes a stored key yet/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
  });

  it("offers anthropic only: an openai-compatible connection gets no key row (#807 makes it a 422)", async () => {
    await open(clientWith("owner", { connections: () => Promise.resolve([ANTHROPIC, { ...ANTHROPIC, provider: "openai-compatible", endpointId: "ep_x" }]) }));
    const list = await screen.findByRole("list", { name: "Stored keys" }, { timeout: LOAD_WAIT_MS });
    expect(list.querySelectorAll("li[data-ref]")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /openai-compatible/ })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/OpenAI-compatible provider first/);
  });

  it("renders a 422 invalid_request from the server and keeps the key out of the page", async () => {
    const refusal = new ProblemError("this provider takes no stored key", 422, "invalid_request");
    const { user } = await open(clientWith("owner", { setKey: () => Promise.reject(refusal), keyState: () => Promise.reject(new NotFoundError("key")) }));
    await user.click(await within(await row()).findByRole("button", { name: "Set key anthropic" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/takes no stored key/);
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
  });

  it("a non-admin sees no keys and no buttons, and makes no key request", async () => {
    const keyState = vi.fn();
    await open(clientWith("viewer", { keyState }));
    expect(await screen.findByText(/Only an admin can see stored keys/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /key/i })).not.toBeInTheDocument();
    expect(keyState).not.toHaveBeenCalled();
  });

  it("sets a first key: masked field, reason required, the key is gone from the page after submit and never shown", async () => {
    const setKey = vi.fn<ApiClient["models"]["setKey"]>();
    const base = createFixtureClient();
    setKey.mockImplementation((i) => base.models.setKey(i));
    const { user } = await open(clientWith("owner", { setKey, keyState: (t) => base.models.keyState(t), revokeKey: (i) => base.models.revokeKey(i) }));
    const item = await row();
    await user.click(await within(item).findByRole("button", { name: "Set key anthropic" }));
    const dialog = await screen.findByRole("dialog", { name: /Set the key of anthropic/ });
    const field = within(dialog).getByLabelText("Key");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "new-password");
    expect(field).toHaveValue("");
    const send = within(dialog).getByRole("button", { name: "Set key" });
    await user.type(field, FAKE_KEY);
    expect(send).toBeDisabled(); // a first key needs a reason
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    expect(send).toBeEnabled();
    await user.click(send);
    const notice = await screen.findByRole("status", { name: "Last change" });
    expect(notice).toHaveTextContent("Set the key of anthropic: version 1 is current");
    expect(setKey).toHaveBeenCalledWith({ provider: "anthropic", value: FAKE_KEY, reason: "first key for the team" });
    expect(document.body.textContent).not.toContain(FAKE_KEY);
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a refused set empties the field, keeps the dialog and quotes nothing", async () => {
    const refusal = new InvalidRequestError("the connection test of the new key failed; the connection is unchanged", []);
    const { user } = await open(clientWith("owner", { setKey: () => Promise.reject(refusal), keyState: () => Promise.reject(new NotFoundError("key")) }));
    const item = await row();
    await user.click(await within(item).findByRole("button", { name: "Set key anthropic" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/connection test of the new key failed/);
    expect(within(dialog).getByLabelText("Key")).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
  });

  it("a forbidden set says only the owner can, and a failed key test says nothing was stored", async () => {
    let refusal = new ProblemError("your role doesn't allow this", 403, "forbidden");
    const { user } = await open(clientWith("owner", { setKey: () => Promise.reject(refusal), keyState: () => Promise.reject(new NotFoundError("key")) }));
    await user.click(await within(await row()).findByRole("button", { name: "Set key anthropic" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Only the owner can set, rotate or revoke keys.");
    refusal = new ProblemError("the connection test of the new key failed; the connection is unchanged", 422, "key_test_failed");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    await waitFor(() => { expect(within(dialog).getByRole("alert")).toHaveTextContent(/did not pass its test request/); });
  });

  it("shows a notice when the secret store is unavailable, and none when it is ok", async () => {
    await open(clientWith("owner", { keysHealth: () => Promise.resolve("unavailable" as const), keyState: () => Promise.reject(new NotFoundError("key")) }));
    expect(await screen.findByText(/Secret store unavailable/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
  });

  it("shows no secret-store notice while the store is ok", async () => {
    const keysHealth = vi.fn<ApiClient["models"]["keysHealth"]>(() => Promise.resolve("ok"));
    await open(clientWith("owner", { keysHealth, keyState: () => Promise.reject(new NotFoundError("key")) }));
    await row();
    await waitFor(() => { expect(keysHealth).toHaveBeenCalled(); });
    expect(screen.queryByText(/Secret store unavailable/)).not.toBeInTheDocument();
  });

  it("a step-up refusal says what to do", async () => {
    const { user } = await open(clientWith("owner", { setKey: () => Promise.reject(new ProblemError("this needs a fresh multi-factor step-up", 403, "mfa_required")), keyState: () => Promise.reject(new NotFoundError("key")) }));
    await user.click(await within(await row()).findByRole("button", { name: "Set key anthropic" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Sign out, sign in again with your code, then send the key again\. Nothing was changed\./);
  });

  it("rotates (reason optional, grace in minutes sent as seconds), then revokes with a required reason", async () => {
    const base = createFixtureClient();
    await base.models.setKey({ provider: "anthropic", value: FAKE_KEY, reason: "first key" });
    const setKey = vi.fn<ApiClient["models"]["setKey"]>((i) => base.models.setKey(i));
    const { user } = await open(clientWith("owner", { setKey, keyState: (t) => base.models.keyState(t), revokeKey: (i) => base.models.revokeKey(i) }));
    const item = await row();
    await waitFor(() => { expect(item).toHaveTextContent("key stored"); });
    expect(item).toHaveTextContent("Version 1");
    await user.click(within(item).getByRole("button", { name: "Rotate key anthropic" }));
    let dialog = await screen.findByRole("dialog", { name: /Rotate the key of anthropic/ });
    await user.type(within(dialog).getByLabelText("Key"), `${FAKE_KEY}-2`);
    await user.type(within(dialog).getByLabelText(/^Grace period/), "30");
    await user.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    await screen.findByRole("status", { name: "Last change" });
    expect(setKey).toHaveBeenCalledWith({ provider: "anthropic", value: `${FAKE_KEY}-2`, graceSeconds: 1800 });
    await waitFor(() => { expect(row_text(item)).toContain("Version 2"); });

    await user.click(within(item).getByRole("button", { name: "Revoke key anthropic" }));
    dialog = await screen.findByRole("dialog", { name: /Revoke the key of anthropic/ });
    const revoke = within(dialog).getByRole("button", { name: "Revoke key" });
    expect(revoke).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^Reason/), "leaked in a ticket");
    await user.click(revoke);
    expect(await screen.findByRole("status", { name: "Last change" })).toHaveTextContent("Revoked versions 1, 2");
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
  });

  it("VK48: the key field is type=password, is emptied on close, and the value is in no URL, storage, console output or error page", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
    const base = createFixtureClient();
    const { user } = await open(clientWith("owner", { setKey: (i) => base.models.setKey(i), keyState: (t) => base.models.keyState(t) }));
    await user.click(await within(await row()).findByRole("button", { name: "Set key anthropic" }));
    let dialog = await screen.findByRole("dialog");
    let field = within(dialog).getByLabelText("Key");
    expect(field).toHaveAttribute("type", "password");
    await user.type(field, FAKE_KEY);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" })); // unmount with a typed value
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(await within(await row()).findByRole("button", { name: "Set key anthropic" }));
    dialog = await screen.findByRole("dialog");
    field = within(dialog).getByLabelText("Key");
    expect(field).toHaveValue(""); // nothing carried over from the closed dialog
    await user.type(field, FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Reason/), "first key for the team");
    await user.click(within(dialog).getByRole("button", { name: "Set key" }));
    await screen.findByRole("status", { name: "Last change" });
    expect(document.querySelector('input[type="password"]')).toBeNull(); // the field is gone with its value
    const seen = [window.location.href, JSON.stringify(Object.entries(window.localStorage)), JSON.stringify(Object.entries(window.sessionStorage)), document.documentElement.outerHTML, ...spies.flatMap((s) => s.mock.calls.map((c) => JSON.stringify(c)))];
    for (const text of seen) expect(text).not.toContain(FAKE_KEY);
    spies.forEach((s) => { s.mockRestore(); });
  });

  it("a grace period outside 0 to 10080 minutes blocks the send", async () => {
    const base = createFixtureClient();
    await base.models.setKey({ provider: "anthropic", value: FAKE_KEY, reason: "first key" });
    const { user } = await open(clientWith("owner", { keyState: (t) => base.models.keyState(t) }));
    const item = await row();
    await user.click(await within(item).findByRole("button", { name: "Rotate key anthropic" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Key"), FAKE_KEY);
    await user.type(within(dialog).getByLabelText(/^Grace period/), "20000");
    expect(within(dialog).getByRole("button", { name: "Rotate key" })).toBeDisabled();
    expect(within(dialog).getByText(/whole number of minutes/)).toBeInTheDocument();
  });
});

const row_text = (el: HTMLElement): string => el.textContent;
