import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { InvalidRequestError, type ProblemError } from "@/api/client";
import { createFixtureModels } from "@/api/fixtures/models";
import { renderAt, serverLike } from "@/test/server-like";

/**
 * #1485: the catalog forms can target a model on an `openai-compatible` connection. The sample
 * API follows the server's rules for the endpoint id (the server's own answers are in
 * `src/api/server/models.test.ts`, recorded from the real server).
 */
const LOAD_WAIT_MS = 20_000;
const OC = "openai-compatible";
const LOCAL = "http://127.0.0.1:11434/v1";
const REF = `${OC}/ollama/llama3.1`;

/** A sample server with the given connections already made, and a spy on every catalog write. */
async function world(connections: { id: string; endpoint: string; key?: string; status?: "active" | "disabled" }[] = [{ id: "ollama", endpoint: LOCAL }]) {
  const base = createFixtureModels();
  for (const c of connections) {
    await base.models.putConnection(OC, { endpointId: c.id, endpoint: c.endpoint, auth: c.key === undefined ? "none" : "key", ...(c.key === undefined ? {} : { key: c.key }), ...(c.status === undefined ? {} : { status: c.status }) });
  }
  const m = base.models;
  const spies = {
    addEntry: vi.fn((i: Parameters<typeof m.addEntry>[0]) => m.addEntry(i)),
    setPrice: vi.fn((i: Parameters<typeof m.setPrice>[0]) => m.setPrice(i)),
    approve: vi.fn((i: Parameters<typeof m.approve>[0]) => m.approve(i)),
    editEntry: vi.fn((i: Parameters<typeof m.editEntry>[0]) => m.editEntry(i)),
    pause: vi.fn((i: Parameters<typeof m.pause>[0]) => m.pause(i)),
    retire: vi.fn((i: Parameters<typeof m.retire>[0]) => m.retire(i)),
    remove: vi.fn((i: Parameters<typeof m.remove>[0]) => m.remove(i)),
    assign: vi.fn((i: Parameters<typeof m.assign>[0]) => m.assign(i)),
  };
  const client: ApiClient = serverLike({ models: { ...base.models, ...spies }, session: base.session });
  return { client, spies, models: base.models };
}

async function open(client: ApiClient, path = "/models/catalog") {
  const user = userEvent.setup();
  renderAt(path, client);
  await screen.findByRole("heading", { level: 1, name: "Models" }, { timeout: LOAD_WAIT_MS });
  return user;
}

const row = (ref: string): HTMLElement => {
  const found = document.querySelector(`[data-ref="${ref}"]`);
  if (found === null) throw new Error(`no row for ${ref}`);
  return found as HTMLElement;
};

async function addDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Add model" }, { timeout: LOAD_WAIT_MS }));
  return screen.findByRole("dialog", { name: "Add a model" });
}

/** Adds `llama3.1` on the `ollama` connection through the form. */
async function addOnConnection(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await addDialog(user);
  await user.selectOptions(await within(dialog).findByLabelText("Connection"), "ollama");
  await user.type(within(dialog).getByLabelText("Model"), "llama3.1");
  await user.type(within(dialog).getByLabelText("Family"), "meta");
  await user.type(within(dialog).getByLabelText(/^Reason/), "our local model");
  await user.click(within(dialog).getByRole("button", { name: "Add model" }));
  await waitFor(() => row(REF));
}

async function addPrice(user: ReturnType<typeof userEvent.setup>, input: string, output: string, source: string, from = "2026-03-01T00:00:00Z") {
  await user.click(within(row(REF)).getByRole("button", { name: /^Add price/ }));
  const dialog = await screen.findByRole("dialog", { name: `Add a price for ${REF}` });
  await user.type(within(dialog).getByLabelText(/^Input, per million/), input);
  await user.type(within(dialog).getByLabelText(/^Output, per million/), output);
  await user.type(within(dialog).getByLabelText(/^Effective from/), from);
  await user.type(within(dialog).getByLabelText("Source"), source);
  await user.type(within(dialog).getByLabelText(/^Reason/), "cost of our own server");
  return dialog;
}

describe("Add model on a connection", () => {
  it("offers each openai-compatible connection, sends its id, and the new entry is paused with no price", async () => {
    const { client, spies } = await world([{ id: "ollama", endpoint: LOCAL }, { id: "mistral", endpoint: "https://api.mistral.ai/v1", key: "mistral" }]);
    const user = await open(client);
    const dialog = await addDialog(user);
    const picker = await within(dialog).findByLabelText("Connection");
    await waitFor(() => {
      expect(within(picker).getAllByRole("option")).toHaveLength(3);
    });
    expect(within(picker).getAllByRole("option").map((o) => o.textContent)).toEqual(["A keyless provider (azure-openai, azure-ai)", `${OC} (mistral) · https://api.mistral.ai/v1`, `${OC} (ollama) · ${LOCAL}`]);
    expect(within(dialog).getByLabelText("Provider")).toBeInTheDocument(); // nothing picked: the provider is typed
    await user.selectOptions(picker, "ollama");
    expect(within(dialog).queryByLabelText("Provider")).not.toBeInTheDocument(); // the connection names the provider
    expect(dialog).toHaveTextContent(`The entry will be ${OC}/ollama/<model>`);
    await user.type(within(dialog).getByLabelText("Model"), "llama3.1");
    await user.type(within(dialog).getByLabelText("Family"), "meta");
    await user.type(within(dialog).getByLabelText(/^Reason/), "our local model");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(row(REF)).toHaveTextContent("paused"));
    expect(spies.addEntry).toHaveBeenCalledExactlyOnceWith({ provider: OC, endpointId: "ollama", model: "llama3.1", family: "meta", processing: "canada", reason: "our local model" });
    expect(row(REF)).toHaveTextContent("No price: calls to this model are blocked"); // unknown stays unknown
    expect(row(REF)).toHaveTextContent("connected");
    expect(screen.getByRole("status", { name: "Last change" })).toHaveTextContent(`Added ${REF} (paused until approved)`);
  }, 40_000);

  it("a typed provider still sends no endpoint id", async () => {
    const { client, spies } = await world();
    const user = await open(client);
    const dialog = await addDialog(user);
    await user.type(within(dialog).getByLabelText("Provider"), "azure-openai");
    await user.type(within(dialog).getByLabelText("Model"), "gpt-5.4-nano");
    await user.type(within(dialog).getByLabelText("Family"), "openai");
    await user.type(within(dialog).getByLabelText(/^Reason/), "cheap tier");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(row("azure-openai/gpt-5.4-nano")).toHaveTextContent("paused"));
    expect(spies.addEntry).toHaveBeenCalledExactlyOnceWith({ provider: "azure-openai", model: "gpt-5.4-nano", family: "openai", processing: "canada", reason: "cheap tier" });
  }, 40_000);

  it("typing openai-compatible as the provider is stopped: the connection must be picked", async () => {
    const { client, spies } = await world();
    const user = await open(client);
    const dialog = await addDialog(user);
    await user.type(within(dialog).getByLabelText("Provider"), OC);
    await user.type(within(dialog).getByLabelText("Model"), "llama3.1");
    await user.type(within(dialog).getByLabelText("Family"), "meta");
    await user.type(within(dialog).getByLabelText(/^Reason/), "our local model");
    expect(within(dialog).getByText(/pick its connection above/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Add model" })).toBeDisabled();
    expect(spies.addEntry).not.toHaveBeenCalled();
  }, 40_000);

  it("with no openai-compatible connection it says to add one first, and a disabled one is marked", async () => {
    const none = await world([]);
    const user = await open(none.client);
    const dialog = await addDialog(user);
    expect(await within(dialog).findByText(/add its connection first on the Connections tab/i)).toBeInTheDocument();
    expect(within(within(dialog).getByLabelText("Connection")).getAllByRole("option")).toHaveLength(1);
  }, 40_000);

  it("a disabled connection is offered and marked as disabled", async () => {
    const { client } = await world([{ id: "ollama", endpoint: LOCAL, status: "disabled" }]);
    const user = await open(client);
    const dialog = await addDialog(user);
    const picker = await within(dialog).findByLabelText("Connection");
    await waitFor(() => {
      expect(within(picker).getAllByRole("option")).toHaveLength(2);
    });
    expect(within(picker).getAllByRole("option")[1]).toHaveTextContent(`${OC} (ollama) · ${LOCAL} (disabled)`);
  }, 40_000);

  it("a hosted connection starts on global processing and says why; the server's refusal of another scope is shown", async () => {
    const { client } = await world([{ id: "mistral", endpoint: "https://api.mistral.ai/v1", key: "mistral" }]);
    const user = await open(client);
    const dialog = await addDialog(user);
    await user.selectOptions(await within(dialog).findByLabelText("Connection"), "mistral");
    expect(within(dialog).getByLabelText("Processing scope")).toHaveValue("global");
    expect(within(dialog).getByText(/A hosted endpoint takes global processing only/)).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Processing scope"), "canada");
    await user.type(within(dialog).getByLabelText("Model"), "mistral-large-latest");
    await user.type(within(dialog).getByLabelText("Family"), "mistral");
    await user.type(within(dialog).getByLabelText(/^Reason/), "hosted model");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("a remote endpoint can only declare global processing");
    expect(document.querySelector(`[data-ref="${OC}/mistral/mistral-large-latest"]`)).toBeNull();
  }, 40_000);
});

describe("Price and approve on a connection", () => {
  it("approve is refused without a price (never guessed); the price and the approval name the connection", async () => {
    const { client, spies } = await world();
    const user = await open(client);
    await addOnConnection(user);

    await user.click(within(row(REF)).getByRole("button", { name: /^Approve/ }));
    const refuse = await screen.findByRole("dialog", { name: `Approve ${REF}` });
    expect(refuse).toHaveTextContent(`on the connection ${OC} (ollama)`);
    await user.type(within(refuse).getByLabelText(/^Reason/), "looks good");
    await user.click(within(refuse).getByRole("button", { name: "Approve" }));
    expect(await within(refuse).findByRole("alert")).toHaveTextContent("a price is never guessed");
    expect(spies.approve).toHaveBeenLastCalledWith({ provider: OC, endpointId: "ollama", model: "llama3.1", reason: "looks good" });
    await user.click(within(refuse).getByRole("button", { name: "Cancel" }));
    expect(row(REF)).toHaveTextContent("paused");

    const price = await addPrice(user, "0.20", "0.60", "our hosting cost sheet");
    expect(price).toHaveTextContent(`on the connection ${OC} (ollama)`);
    await user.click(within(price).getByRole("button", { name: "Add price" }));
    await waitFor(() => expect(row(REF)).toHaveTextContent("$0.20 in · $0.60 out per 1M tokens"));
    expect(spies.setPrice).toHaveBeenCalledExactlyOnceWith({ provider: OC, endpointId: "ollama", model: "llama3.1", inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" });

    await user.click(within(row(REF)).getByRole("button", { name: /^Approve/ }));
    const approve = await screen.findByRole("dialog", { name: `Approve ${REF}` });
    await user.type(within(approve).getByLabelText(/^Reason/), "priced now");
    await user.click(within(approve).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(row(REF)).toHaveTextContent("approved"));
    expect(spies.approve).toHaveBeenLastCalledWith({ provider: OC, endpointId: "ollama", model: "llama3.1", reason: "priced now" });
  }, 60_000);

  it("a local connection can be declared free: the form fills zero rates and the source the server requires", async () => {
    const { client, spies } = await world();
    const user = await open(client);
    await addOnConnection(user);
    await user.click(within(row(REF)).getByRole("button", { name: /^Add price/ }));
    const dialog = await screen.findByRole("dialog", { name: `Add a price for ${REF}` });
    await user.click(await within(dialog).findByRole("button", { name: "Declare free (local server)" }));
    expect(within(dialog).getByLabelText(/^Input, per million/)).toHaveValue("0");
    expect(within(dialog).getByLabelText(/^Output, per million/)).toHaveValue("0");
    expect(within(dialog).getByLabelText("Source")).toHaveValue("declared free (local)");
    await user.type(within(dialog).getByLabelText(/^Effective from/), "2026-03-01T00:00:00Z");
    await user.type(within(dialog).getByLabelText(/^Reason/), "runs on our own machine");
    await user.click(within(dialog).getByRole("button", { name: "Add price" }));
    await waitFor(() => expect(row(REF)).toHaveTextContent("$0 in · $0 out per 1M tokens (declared free, local)"));
    expect(spies.setPrice).toHaveBeenCalledExactlyOnceWith({ provider: OC, endpointId: "ollama", model: "llama3.1", inputPerMillion: "0", outputPerMillion: "0", effectiveFrom: "2026-03-01T00:00:00Z", source: "declared free (local)", reason: "runs on our own machine" });
  }, 60_000);

  it("a zero price typed with another source is refused with the server's words, and nothing is priced", async () => {
    const { client } = await world();
    const user = await open(client);
    await addOnConnection(user);
    const dialog = await addPrice(user, "0", "0", "it is free");
    await user.click(within(dialog).getByRole("button", { name: "Add price" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("a zero price must be declared free for a local connection");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(row(REF)).toHaveTextContent("No price: calls to this model are blocked");
  }, 60_000);

  it("a hosted connection, or an entry of another provider, is not offered the free declaration", async () => {
    const { client, models } = await world([{ id: "mistral", endpoint: "https://api.mistral.ai/v1", key: "mistral" }]);
    await models.addEntry({ provider: OC, endpointId: "mistral", model: "mistral-large-latest", family: "mistral", processing: "global", reason: "hosted model" });
    const user = await open(client);
    for (const ref of [`${OC}/mistral/mistral-large-latest`, "azure-ai/Llama-4-Maverick"]) {
      await user.click(within(await waitFor(() => row(ref))).getByRole("button", { name: /^Add price/ }));
      const dialog = await screen.findByRole("dialog", { name: `Add a price for ${ref}` });
      expect(within(dialog).queryByRole("button", { name: /Declare free/ })).not.toBeInTheDocument();
      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    }
  }, 60_000);

  it("edit, pause, retire and remove send the connection too", async () => {
    const { client, spies, models } = await world();
    const named = { provider: OC, endpointId: "ollama", model: "llama3.1" };
    await models.addEntry({ ...named, family: "meta", processing: "canada", reason: "our local model" });
    await models.setPrice({ ...named, inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" });
    await models.approve({ ...named, reason: "priced now" });
    const user = await open(client);
    const act = async (button: RegExp, title: string, submit: string, fill?: (dialog: HTMLElement) => Promise<void>) => {
      await user.click(within(await waitFor(() => row(REF))).getByRole("button", { name: button }));
      const dialog = await screen.findByRole("dialog", { name: title });
      if (fill !== undefined) await fill(dialog);
      await user.type(within(dialog).getByLabelText(/^Reason/), "as planned");
      await user.click(within(dialog).getByRole("button", { name: submit }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    };
    await act(/^Edit/, `Edit ${REF}`, "Save", async (dialog) => {
      await user.clear(within(dialog).getByLabelText("Family"));
      await user.type(within(dialog).getByLabelText("Family"), "llama");
    });
    expect(spies.editEntry).toHaveBeenCalledExactlyOnceWith({ ...named, family: "llama", reason: "as planned" });
    await act(/^Pause/, `Pause ${REF}`, "Pause");
    expect(spies.pause).toHaveBeenCalledExactlyOnceWith({ ...named, reason: "as planned" });
    await act(/^Retire/, `Retire ${REF}`, "Retire");
    expect(spies.retire).toHaveBeenCalledExactlyOnceWith({ ...named, reason: "as planned" });
    await act(/^Remove/, `Remove ${REF}`, "Remove");
    expect(spies.remove).toHaveBeenCalledExactlyOnceWith({ ...named, reason: "as planned" });
    expect(document.querySelector(`[data-ref="${REF}"]`)).toBeNull();
  }, 90_000);
});

describe("An approved model on a connection", () => {
  it("is offered in the assignment picker under its full name and can be assigned", async () => {
    const { client, spies, models } = await world();
    const named = { provider: OC, endpointId: "ollama", model: "llama3.1" };
    await models.addEntry({ ...named, family: "meta", processing: "canada", reason: "our local model" });
    await models.setPrice({ ...named, inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" });
    await models.approve({ ...named, reason: "priced now" });
    const user = await open(client, "/models/assignments");
    const list = await screen.findByRole("list", { name: "Model assignments" });
    const dana = within(list).getByText("agent=dana").closest("li") as HTMLElement;
    await user.selectOptions(within(dana).getByLabelText(/Model for agent=dana/), REF);
    const dialog = await screen.findByRole("dialog", { name: `Assign ${REF} to agent=dana` });
    await user.type(within(dialog).getByLabelText(/^Reason/), "try the local model");
    await user.click(within(dialog).getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(within(list).getByText("agent=dana").closest("li")).toHaveTextContent("agent ledger"));
    expect(spies.assign).toHaveBeenCalledExactlyOnceWith({ scope: "agent", id: "dana", model: REF, reason: "try the local model" });
    expect(within(within(list).getByText("agent=dana").closest("li") as HTMLElement).getByLabelText(/Model for agent=dana/)).toHaveValue(REF);
    const resolved = await models.resolution("customer");
    expect(resolved.resolutions.find((r) => r.subject === "agent=dana")).toMatchObject({ ref: REF, endpoint: LOCAL, priceBasis: "book" });
  }, 60_000);

  it("the connection's data-location note and repoint warning list only that connection's models", async () => {
    const { client, models } = await world([{ id: "ollama", endpoint: LOCAL }, { id: "vllm", endpoint: "http://127.0.0.1:8000/v1" }]);
    for (const [id, model] of [["ollama", "llama3.1"], ["vllm", "qwen"]] as const) {
      const named = { provider: OC, endpointId: id, model };
      await models.addEntry({ ...named, family: "meta", processing: "canada", reason: "our local model" });
      await models.setPrice({ ...named, inputPerMillion: "0.20", outputPerMillion: "0.60", effectiveFrom: "2026-03-01T00:00:00Z", source: "our hosting cost sheet", reason: "cost of our own server" });
      await models.approve({ ...named, reason: "priced now" });
    }
    await open(client, "/models/connections");
    const list = await screen.findByRole("list", { name: "Connections" });
    const items = within(list).getAllByRole("listitem");
    const ollama = items.find((i) => i.textContent.includes(`${OC} (ollama)`));
    expect(ollama).toHaveTextContent(`${OC}/ollama/llama3.1 (canada)`);
    expect(ollama).not.toHaveTextContent("qwen");
  }, 60_000);
});

describe("the sample API follows the server's endpoint-id rules", () => {
  const entry = { model: "llama3.1", family: "meta", processing: "canada", reason: "our local model" };
  /** The sample API refuses by throwing, before or inside its promise: both are caught here. */
  const refusal = async (call: () => Promise<unknown>): Promise<unknown> => {
    try {
      await call();
    } catch (error) {
      return error;
    }
    return undefined;
  };
  const problem = async (call: () => Promise<unknown>) => {
    const error = await refusal(call);
    expect(error).toBeInstanceOf(InvalidRequestError);
    return (error as InvalidRequestError).fields[0];
  };

  it("needs an endpoint id for openai-compatible, a valid one, and refuses one for any other provider (the server's texts)", async () => {
    const { models } = createFixtureModels();
    expect(await problem(() => models.addEntry({ provider: OC, ...entry }))).toEqual({ path: "$.endpoint_id", message: "openai-compatible: this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)" });
    expect(await problem(() => models.addEntry({ provider: OC, endpointId: "Not Valid", ...entry }))).toEqual({ path: "$.endpoint_id", message: "openai-compatible: the endpoint id is not valid" });
    expect(await problem(() => models.addEntry({ provider: "azure-openai", endpointId: "ollama", ...entry }))).toEqual({ path: "$.endpoint_id", message: "endpoint_id_not_allowed" });
    expect(await problem(() => models.approve({ provider: OC, model: "llama3.1", reason: "looks good" }))).toEqual({ path: "$.endpoint_id", message: "openai-compatible: this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)" });
  });

  it("keeps the same model name on two connections apart", async () => {
    const { models } = createFixtureModels();
    await models.addEntry({ provider: OC, endpointId: "one", ...entry });
    await models.addEntry({ provider: OC, endpointId: "two", ...entry });
    const again = await refusal(() => models.addEntry({ provider: OC, endpointId: "one", ...entry }));
    expect((again as ProblemError).title).toBe("already_exists");
    const refs = (await models.catalog()).entries.filter((e) => e.provider === OC).map((e) => [e.ref, e.endpointId, e.connected]);
    expect(refs).toEqual([[`${OC}/one/llama3.1`, "one", false], [`${OC}/two/llama3.1`, "two", false]]);
    const missing = await refusal(() => models.pause({ provider: OC, endpointId: "three", model: "llama3.1", reason: "under review" }));
    expect((missing as ProblemError).message).toBe(`'${OC}/three/llama3.1' is not in the model catalog`);
  });

  it("a free price needs the declared source and an active local connection; a repoint pauses only that connection's entries", async () => {
    const { models } = createFixtureModels();
    await models.putConnection(OC, { endpointId: "one", endpoint: LOCAL, auth: "none" });
    await models.putConnection(OC, { endpointId: "two", endpoint: "https://models.example.com/v1", auth: "key", key: "two" });
    const price = { inputPerMillion: "0", outputPerMillion: "0", effectiveFrom: "2026-03-01T00:00:00Z", reason: "no charge" };
    await models.addEntry({ provider: OC, endpointId: "one", ...entry });
    expect(await problem(() => models.addEntry({ provider: OC, endpointId: "two", ...entry }))).toEqual({ path: "$.processing", message: "openai-compatible: a remote endpoint can only declare global processing" });
    await models.addEntry({ provider: OC, endpointId: "two", ...entry, processing: "global" });
    expect((await problem(() => models.setPrice({ provider: OC, endpointId: "one", model: "llama3.1", ...price, source: "it is free" })))?.message).toContain("a zero price must be declared free for a local connection");
    expect(await problem(() => models.setPrice({ provider: OC, endpointId: "two", model: "llama3.1", ...price, source: "declared free (local)" }))).toEqual({ path: "$", message: "openai-compatible: a free price is only allowed for an active local (loopback) connection" });
    await models.setPrice({ provider: OC, endpointId: "one", model: "llama3.1", ...price, source: "declared free (local)" });
    await models.setPrice({ provider: OC, endpointId: "two", model: "llama3.1", ...price, inputPerMillion: "2", outputPerMillion: "6", source: "vendor price list" });
    await models.approve({ provider: OC, endpointId: "one", model: "llama3.1", reason: "looks good" });
    await models.approve({ provider: OC, endpointId: "two", model: "llama3.1", reason: "looks good" });
    const moved = await models.putConnection(OC, { endpointId: "two", endpoint: "https://other.example.com/v1", auth: "key", key: "two", reason: "moved hosts" });
    expect(moved.repaused).toEqual([`${OC}/two/llama3.1`]);
    const status = Object.fromEntries((await models.catalog()).entries.filter((e) => e.provider === OC).map((e) => [e.ref, e.status]));
    expect(status).toEqual({ [`${OC}/one/llama3.1`]: "approved", [`${OC}/two/llama3.1`]: "paused" });
  });
});
