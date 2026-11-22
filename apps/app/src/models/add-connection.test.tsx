import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { InvalidRequestError } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createFixtureModels } from "@/api/fixtures/models";
import { renderAt, serverLike } from "@/test/server-like";

import { PRESETS } from "./presets";

const LOAD_WAIT_MS = 20_000;

function clientWith(over: Parameters<typeof createFixtureModels>[0] = {}, models: Partial<ApiClient["models"]> = {}): ApiClient {
  const base = createFixtureModels(over);
  return serverLike({ models: { ...base.models, ...models }, session: base.session });
}

async function openDialog(client: ApiClient = clientWith()) {
  const user = userEvent.setup();
  renderAt("/models/connections", client);
  await user.click(await screen.findByRole("button", { name: "Add a connection" }, { timeout: LOAD_WAIT_MS }));
  const dialog = await screen.findByRole("dialog", { name: "Add a connection" });
  return { user, dialog, client };
}

const field = (dialog: HTMLElement, name: string | RegExp): HTMLInputElement => within(dialog).getByLabelText(name);

describe("Add a connection: presets", () => {
  it("offers every preset and prefills the URL, the id, the model hint and the key name", async () => {
    const { user, dialog } = await openDialog();
    const select = within(dialog).getByLabelText("Preset");
    expect(within(select).getAllByRole("option")).toHaveLength(PRESETS.length + 1); // plus "another endpoint"
    expect(field(dialog, "Base URL")).toHaveValue("https://api.mistral.ai/v1");
    expect(field(dialog, "Connection id")).toHaveValue("mistral");
    expect(field(dialog, /Key binding name/)).toHaveValue("mistral");
    expect(within(dialog).getByText(/Model name, for example: mistral-large-latest/)).toBeInTheDocument();
    await user.selectOptions(select, "ollama");
    expect(field(dialog, "Base URL")).toHaveValue("http://127.0.0.1:11434/v1");
    expect(field(dialog, "Connection id")).toHaveValue("ollama");
    expect(field(dialog, /Key binding name/)).toHaveValue("");
    expect(within(dialog).getByText(/Model name, for example: llama3.1/)).toBeInTheDocument();
    expect(within(dialog).getByText(/127\.0\.0\.1 is the machine the NuraMesh server runs on, not the one your browser is on/)).toBeInTheDocument();
    expect(within(dialog).getByText(/connection test is refused unless its operator sets NURAMESH_ALLOW_PRIVATE_NETWORK=1/)).toBeInTheDocument();
  });

  it.each(PRESETS.map((p) => [p.id, p.baseUrl] as const))("the %s preset prefills %s", async (id, url) => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), id);
    expect(field(dialog, "Base URL")).toHaveValue(url);
  });

  it("ships no price: the saved view says an unknown price stays unknown", async () => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "ollama");
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    expect(await screen.findByText(/an unknown price stays recorded as unknown/)).toBeInTheDocument();
  });
});

describe("Add a connection: save", () => {
  it("saves a keyless local server with auth none and no key name, then lists it", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: put }));
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "vllm");
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    expect(put).toHaveBeenCalledWith("openai-compatible", { endpointId: "vllm", endpoint: "http://127.0.0.1:8000/v1", auth: "none" });
  });

  it("saves a hosted preset with the key NAME (auth key) and never a key value", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: put }));
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "groq");
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    expect(put).toHaveBeenCalledWith("openai-compatible", { endpointId: "groq", endpoint: "https://api.groq.com/openai/v1", auth: "key", key: "groq" });
    expect(document.body.textContent).toMatch(/key binding named groq/);
    expect(document.body.textContent).toMatch(/No key is shown or kept in this page/);
    expect(screen.queryByLabelText(/^Key$/)).not.toBeInTheDocument();
  });

  it("the key-name field is a plain name: it says never to paste a key, and a key-shaped value is refused", async () => {
    const { user, dialog } = await openDialog();
    expect(within(dialog).getByText(/never the key itself/)).toBeInTheDocument();
    const name = field(dialog, /Key binding name/);
    expect(name).not.toHaveAttribute("type", "password");
    await user.clear(name);
    await user.type(name, "NOT A VALID NAME/with-slash");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/lowercase letters, digits, hyphens or underscores/);
    expect(within(dialog).getByRole("button", { name: "Save connection" })).toBeDisabled();
  });

  it("a hosted preset cannot be saved without a key name; a local one can", async () => {
    const { user, dialog } = await openDialog();
    const save = within(dialog).getByRole("button", { name: "Save connection" });
    expect(save).toBeEnabled();
    await user.clear(field(dialog, /Key binding name/));
    expect(save).toBeDisabled();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "lmstudio");
    expect(save).toBeEnabled();
  });

  it("validates the URL and the id in place before any request", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: put }));
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "ollama");
    const url = field(dialog, "Base URL");
    await user.clear(url);
    await user.type(url, "http://example.com/v1");
    expect(await within(dialog).findByText(/Plain http is only allowed to 127.0.0.1/)).toBeInTheDocument();
    await user.clear(url);
    await user.type(url, "https://user:pw@host.example/v1");
    expect(await within(dialog).findByText(/query, a fragment or a login/)).toBeInTheDocument();
    await user.clear(url);
    await user.type(url, "ftp://x");
    expect(await within(dialog).findByText(/starts with https/)).toBeInTheDocument();
    const id = field(dialog, "Connection id");
    await user.clear(id);
    await user.type(id, "Bad Id");
    expect(await within(dialog).findByText(/1 to 40 lowercase letters/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save connection" })).toBeDisabled();
    expect(put).not.toHaveBeenCalled();
  });

  it("plain http with a key name is refused in the form", async () => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "ollama");
    await user.type(field(dialog, /Key binding name/), "mine");
    expect(await within(dialog).findByText(/never sent over plain http/)).toBeInTheDocument();
  });

  it("shows the server's refusal in place and keeps the dialog open (a hosted server refuses this provider)", async () => {
    const refusal = new InvalidRequestError("hosted", [{ path: "$.provider", message: "openai-compatible: refused on a hosted server" }]);
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: () => Promise.reject(refusal) }));
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Nothing was changed.");
    expect(alert).toHaveTextContent(/refused on a hosted server/);
    expect(screen.getByRole("dialog", { name: "Add a connection" })).toBeInTheDocument();
  });

  it("a non-admin cannot reach the form", async () => {
    const user = userEvent.setup();
    renderAt("/models/connections", createFixtureClient({ role: "viewer" }));
    expect(await screen.findByText(/Only an admin can see connections/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
    expect(user).toBeDefined();
    expect(screen.queryByRole("button", { name: "Add a connection" })).not.toBeInTheDocument();
  });
});

describe("Add a connection: test", () => {
  async function saved(over: Parameters<typeof createFixtureModels>[0], preset: string) {
    const ctx = await openDialog(clientWith(over));
    await ctx.user.selectOptions(within(ctx.dialog).getByLabelText("Preset"), preset);
    await ctx.user.click(within(ctx.dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    return ctx;
  }

  it("Test connection succeeds for a reachable hosted endpoint", async () => {
    const { user } = await saved({}, "mistral");
    await user.click(screen.getByRole("button", { name: "Test connection openai-compatible (mistral)" }));
    expect(await screen.findByRole("status", { name: "Test result" })).toHaveTextContent(/Test passed in 42 ms: the endpoint answered/);
  });

  it("a local endpoint is refused unless the server allows the private network, and the form says how", async () => {
    const { user } = await saved({}, "ollama");
    await user.click(screen.getByRole("button", { name: "Test connection openai-compatible (ollama)" }));
    const result = await screen.findByRole("status", { name: "Test result" });
    expect(result).toHaveTextContent(/Test failed \(refused\)/);
    expect(result).not.toHaveTextContent(/FAILED \[/);
    expect(result).toHaveTextContent(/NURAMESH_ALLOW_PRIVATE_NETWORK=1 \(a self-hosted server/);
    expect(result).toHaveTextContent(/The connection is kept/);
  });

  it("with the private network allowed the local test passes", async () => {
    const { user } = await saved({ allowPrivateNetwork: true }, "ollama");
    await user.click(screen.getByRole("button", { name: "Test connection openai-compatible (ollama)" }));
    expect(await screen.findByRole("status", { name: "Test result" })).toHaveTextContent(/Test passed in 42 ms: the endpoint answered/);
  });

  it("a rate-limited test is shown as an alert", async () => {
    const { ProblemError } = await import("@/api/client");
    const ctx = await openDialog(clientWith({}, { testConnection: () => Promise.reject(new ProblemError("limit", 429, "rate_limited", 7)) }));
    await ctx.user.click(within(ctx.dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    await ctx.user.click(screen.getByRole("button", { name: /^Test connection openai-compatible/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/try again in 7 s/);
  });

  it("the saved connection appears in the list with its sign-in kind and its own actions, and Done closes the form", async () => {
    const { user } = await saved({}, "together");
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => { expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); });
    const list = await screen.findByRole("list", { name: "Connections" });
    const item = within(list).getByText("openai-compatible (together)").closest("li") as HTMLElement;
    expect(item).toHaveTextContent("https://api.together.xyz/v1");
    expect(item).toHaveTextContent(/Key named in the operator's key binding file/);
    expect(within(item).getByRole("button", { name: "Disable openai-compatible (together)" })).toBeInTheDocument();
  });
});

describe("Add a connection: keyboard and aria", () => {
  it("is a labelled dialog whose every field has a label, reachable by keyboard, and Escape closes it", async () => {
    const { user, dialog } = await openDialog();
    for (const label of ["Preset", "Connection id", "Base URL", /Key binding name/, /^Reason/]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
    expect(field(dialog, /Key binding name/)).toHaveAccessibleDescription(/never the key itself/);
    within(dialog).getByLabelText("Preset").focus();
    await user.tab();
    expect(field(dialog, "Connection id")).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => { expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); });
  });

  it("links each error to its field with aria-describedby (the key field keeps its help text too)", async () => {
    const { user, dialog } = await openDialog();
    const id = field(dialog, "Connection id");
    await user.clear(id);
    await user.type(id, "Bad Id");
    expect(id).toHaveAccessibleDescription(/lowercase letters/);
    const url = field(dialog, "Base URL");
    await user.clear(url);
    await user.type(url, "nope");
    expect(url).toHaveAccessibleDescription(/\S/);
    const name = field(dialog, /Key binding name/);
    await user.clear(name);
    await user.type(name, "Bad Name");
    expect(name).toHaveAccessibleDescription(/never the key itself/);
    expect(name).toHaveAccessibleDescription(/lowercase letters/);
  });

  it("warns and refuses to save when the key binding name looks like a pasted key, and never echoes it", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: put }));
    const pasted = "obviously0fake0test0key0not0a0secret";
    const name = field(dialog, /Key binding name/);
    await user.clear(name);
    await user.type(name, pasted.slice(0, 36).toLowerCase());
    const alert = within(dialog).getByText(/looks like a key, not a name/);
    expect(alert).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(`${pasted.slice(0, 36)} is`);
    expect(alert.textContent).not.toContain(pasted.slice(0, 10));
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    expect(put).not.toHaveBeenCalled();
  });

  it("Enter in a field submits the form", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { putConnection: put }));
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "tgi");
    await user.type(field(dialog, "Connection id"), "{Enter}");
    await screen.findByRole("heading", { name: "Connection saved" });
    expect(put).toHaveBeenCalledTimes(1);
  });
});

describe("Existing connections keep their identity", () => {
  it("Disable and Repoint of an openai-compatible connection carry its id, auth and key name", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const remove = vi.fn<ApiClient["models"]["removeConnection"]>(() => Promise.resolve());
    const conn = { provider: "openai-compatible", endpointId: "grp", auth: "key", keyName: "groq", endpoint: "https://api.groq.com/openai/v1", status: "active" as const, setBy: "human:u_sample", setAt: "2026-10-24T12:00:00Z", seq: 4 };
    const base = createFixtureModels();
    const client = serverLike({ models: { ...base.models, connections: () => Promise.resolve([conn]), putConnection: put, removeConnection: remove }, session: base.session });
    const user = userEvent.setup();
    renderAt("/models/connections", client);
    await user.click(await screen.findByRole("button", { name: "Disable openai-compatible (grp)" }, { timeout: LOAD_WAIT_MS }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disable" }));
    await waitFor(() => { expect(put).toHaveBeenCalledWith("openai-compatible", { endpointId: "grp", auth: "key", key: "groq", endpoint: conn.endpoint, status: "disabled" }); });
    await user.click(await screen.findByRole("button", { name: "Disconnect openai-compatible (grp)" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => { expect(remove).toHaveBeenCalledWith("openai-compatible", undefined, "grp"); });
  });
});

describe("Add a connection: design review (#1449)", () => {
  const existing = { provider: "openai-compatible", endpointId: "mistral", auth: "key", keyName: "mistral", endpoint: "https://api.mistral.ai/v1", status: "active" as const, setBy: "human:u_sample", setAt: "2026-10-24T12:00:00Z", seq: 4 };

  it("groups the presets: hosted services, servers you run, and another endpoint", async () => {
    const { dialog } = await openDialog();
    const groups = [...within(dialog).getByLabelText("Preset").querySelectorAll("optgroup")];
    expect(groups.map((g) => g.label)).toEqual(["Hosted services", "Servers you run", "Other"]);
    expect(groups.map((g) => g.querySelectorAll("option").length)).toEqual([PRESETS.filter((p) => !p.local).length, PRESETS.filter((p) => p.local).length, 1]);
  });

  it("another endpoint starts empty: the preset's URL, id and key name are not carried over", async () => {
    const { user, dialog } = await openDialog();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "custom");
    expect(field(dialog, "Base URL")).toHaveValue("");
    expect(field(dialog, "Connection id")).toHaveValue("");
    expect(field(dialog, /Key binding name/)).toHaveValue("");
    expect(within(dialog).getByText(/Enter the base URL of your endpoint/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument(); // empty and untouched is not an error yet
    expect(within(dialog).getByRole("button", { name: "Save connection" })).toBeDisabled();
  });

  it("another endpoint keeps what the person typed", async () => {
    const { user, dialog } = await openDialog();
    const url = field(dialog, "Base URL");
    await user.clear(url);
    await user.type(url, "https://models.example.com/v1");
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "custom");
    expect(url).toHaveValue("https://models.example.com/v1");
    expect(field(dialog, "Connection id")).toHaveValue("");
  });

  it("says why a hosted preset cannot be saved without a key binding name", async () => {
    const { user, dialog } = await openDialog();
    const name = field(dialog, /Key binding name/);
    expect(name).toBeRequired();
    await user.clear(name);
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Mistral needs a key: enter the name of its key binding");
    expect(name).toHaveAccessibleDescription(/Mistral needs a key/);
    expect(name).toBeInvalid();
    await user.selectOptions(within(dialog).getByLabelText("Preset"), "ollama");
    expect(field(dialog, /Key binding name/)).not.toBeRequired();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("the id and the URL are required fields, and an emptied one says so", async () => {
    const { user, dialog } = await openDialog();
    expect(field(dialog, "Connection id")).toBeRequired();
    expect(field(dialog, "Base URL")).toBeRequired();
    await user.clear(field(dialog, "Base URL"));
    expect(field(dialog, "Base URL")).toHaveAccessibleDescription("Enter the base URL of the endpoint");
    await user.clear(field(dialog, "Connection id"));
    expect(field(dialog, "Connection id")).toHaveAccessibleDescription("Enter an id for this connection");
  });

  it("refuses an id that is already in use instead of silently changing that connection", async () => {
    const put = vi.fn<ApiClient["models"]["putConnection"]>(() => Promise.resolve({ repaused: [] }));
    const { user, dialog } = await openDialog(clientWith({}, { connections: () => Promise.resolve([existing]), putConnection: put }));
    const id = field(dialog, "Connection id");
    expect(id).toHaveAccessibleDescription("A connection with the id mistral already exists. Choose another id, or close this form and use Repoint to change that connection");
    expect(within(dialog).getByRole("button", { name: "Save connection" })).toBeDisabled();
    await user.type(id, "-eu");
    expect(id).toHaveAccessibleDescription("");
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    expect(put).toHaveBeenCalledWith("openai-compatible", expect.objectContaining({ endpointId: "mistral-eu" }));
  });

  it("the test button is named for what it does and is busy while the test runs", async () => {
    let finish: (r: Awaited<ReturnType<ApiClient["models"]["testConnection"]>>) => void = () => undefined;
    const pending = new Promise<Awaited<ReturnType<ApiClient["models"]["testConnection"]>>>((resolve) => { finish = resolve; });
    const { user, dialog } = await openDialog(clientWith({}, { testConnection: () => pending }));
    await user.click(within(dialog).getByRole("button", { name: "Save connection" }));
    await screen.findByRole("heading", { name: "Connection saved" });
    const button = screen.getByRole("button", { name: "Test connection openai-compatible (mistral)" });
    expect(button).toHaveTextContent("Test connection");
    await user.click(button);
    await waitFor(() => { expect(button).toHaveAttribute("aria-busy", "true"); });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Testing…");
    finish({ provider: "openai-compatible", ok: false, code: "unauthorized", text: "openai-compatible: the endpoint refused the credentials", latencyMs: 120, ledgerSeq: 9 });
    const result = await screen.findByRole("status", { name: "Test result" });
    expect(result).toHaveTextContent("Test failed (unauthorized) in 120 ms: openai-compatible: the endpoint refused the credentials");
    expect(result).toHaveTextContent(/ask your operator to check the key file behind the key binding name/);
    expect(button).not.toHaveAttribute("aria-busy");
  });

  it("the tab says how an OpenAI-compatible endpoint gets its key (a named binding, not a stored key)", async () => {
    renderAt("/models/connections", clientWith());
    expect(await screen.findByText(/an OpenAI-compatible endpoint, with a key named in the operator's key binding file or without a key/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/with or without a stored key/);
  });

  it("the empty list says what to do next", async () => {
    renderAt("/models/connections", clientWith({}, { connections: () => Promise.resolve([]) }));
    expect(await screen.findByText(/Add a connection for an OpenAI-compatible endpoint, or connect a keyless Azure provider/, undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
  });

  it("Connect a provider no longer says stored keys are not built (the Keys tab stores them)", async () => {
    const user = userEvent.setup();
    renderAt("/models/connections", clientWith({}, { connections: () => Promise.resolve([]) }));
    await user.click(await screen.findByRole("button", { name: "Connect a provider" }, { timeout: LOAD_WAIT_MS }));
    const dialog = await screen.findByRole("dialog", { name: "Connect a provider" });
    expect(dialog).toHaveTextContent(/No key is stored or typed here/);
    expect(dialog).not.toHaveTextContent(/not built/);
  });
});
