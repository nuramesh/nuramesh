import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { Catalog } from "@/api/models";
import { ProblemError } from "@/api/client";
import { renderAt, serverLike } from "@/test/server-like";
import { createUnavailableClient } from "@/test/unavailable-client";

const row = (ref: string): HTMLElement => {
  const found = document.querySelector(`[data-ref="${ref}"]`);
  if (found === null) throw new Error(`no row for ${ref}`);
  return found as HTMLElement;
};

const LOAD_WAIT_MS = 20_000;

async function open(path = "/models/catalog", client: ApiClient = createFixtureClient()) {
  const user = userEvent.setup();
  renderAt(path, client);
  // Lazy route chunks load slowly on a busy machine: generous explicit wait, not the 1000 ms default.
  await screen.findByRole("heading", { level: 1, name: "Models" }, { timeout: LOAD_WAIT_MS });
  return { user, client };
}

const ACTIONS = /^(Approve|Pause|Retire|Remove|Edit|Add price|Add model|Customer regions|Change)/;

describe("Models: routes and navigation", () => {
  it("/models goes to the catalog; the sections are links; the sidebar has Models", async () => {
    await open("/models");
    const sections = within(screen.getByRole("navigation", { name: "Models sections" }));
    for (const name of ["Catalog", "Assignments", "Connections", "Resolution"]) {
      expect(sections.getByRole("link", { name })).toBeInTheDocument();
    }
    expect(sections.getByRole("link", { name: "Catalog" })).toHaveAttribute("aria-current", "page");
    expect(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Models" })).toHaveAttribute("href", "/models");
    expect(await screen.findByRole("list", { name: "Approved model catalog" }, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
  }, 40_000);
});

describe("Catalog (admin)", () => {
  it("lists every entry with status in words, the data it may receive, its price and where it came from", async () => {
    await open();
    const mini = await waitFor(() => row("azure-openai/gpt-5.4-mini"));
    expect(mini).toHaveTextContent("approved");
    expect(mini).toHaveTextContent("global");
    expect(mini).toHaveTextContent(/synthetic data only/i);
    expect(mini).toHaveTextContent("$0.75 in");
    expect(mini).toHaveTextContent("$4.50 out");
    expect(mini).toHaveTextContent(/seeded from the configuration/i);
    expect(row("azure-openai/gpt-5.4-ca")).toHaveTextContent(/synthetic and customer data/i);
    const paused = row("azure-ai/Llama-4-Maverick");
    expect(paused).toHaveTextContent("paused");
    expect(paused).toHaveTextContent(/no price/i); // unknown stays unknown
    expect(screen.getByText(/Customer regions:/)).toBeInTheDocument();
  });

  it("says a missing price is blocked, with an icon and a status role, and counts them over the list (DP-19)", async () => {
    await open();
    const paused = await waitFor(() => row("azure-ai/Llama-4-Maverick"));
    const words = within(paused).getByText("No price: calls to this model are blocked");
    expect(words.closest("[role='status']")).not.toBeNull();
    expect(words.parentElement?.querySelector("[aria-hidden='true']")).not.toBeNull(); // the icon, so the cue is not colour alone
    const header = screen.getByRole("link", { name: /1 model has no price and is blocked/ });
    expect(header).toHaveAttribute("href", "#entry-azure-ai-Llama-4-Maverick");
    expect(paused).toHaveAttribute("id", "entry-azure-ai-Llama-4-Maverick");
  });

  it("offers each action only where the entry's status allows it", async () => {
    await open();
    await waitFor(() => row("fake/dev-1"));
    const approved = within(row("fake/dev-1"));
    expect(approved.getByRole("button", { name: /^Pause/ })).toBeInTheDocument();
    expect(approved.getByRole("button", { name: /^Retire/ })).toBeInTheDocument();
    expect(approved.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
    expect(approved.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument(); // must be paused or retired first
    const paused = within(row("azure-ai/Llama-4-Maverick"));
    expect(paused.getByRole("button", { name: /^Approve/ })).toBeInTheDocument();
    expect(paused.getByRole("button", { name: /^Remove/ })).toBeInTheDocument();
  });

  it("pause: the reason needs 3 characters, then it says who is blocked and the row changes", async () => {
    const { user } = await open();
    await waitFor(() => row("fake/dev-1"));
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Pause/ }));
    const dialog = await screen.findByRole("dialog", { name: /Pause fake\/dev-1/ });
    const send = within(dialog).getByRole("button", { name: "Pause" });
    expect(send).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^Reason/), "ab");
    expect(send).toBeDisabled();
    expect(within(dialog).getByText(/3 to 500 characters is required/i)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Reason/), "c");
    expect(send).toBeEnabled();
    await user.click(send);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const notice = await screen.findByRole("status", { name: "Last change" });
    expect(notice).toHaveTextContent("Paused fake/dev-1");
    expect(notice).toHaveTextContent("Their next call is blocked: agent=dana, agent=theo, agent=omar");
    expect(row("fake/dev-1")).toHaveTextContent("paused");
  });

  it("shows the server's refusal in the dialog, keeps it open and says nothing changed", async () => {
    const { user } = await open();
    await waitFor(() => row("azure-ai/Llama-4-Maverick"));
    await user.click(within(row("azure-ai/Llama-4-Maverick")).getByRole("button", { name: /^Approve/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "looks fine");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Nothing was changed");
    expect(alert).toHaveTextContent(/has no price/);
    expect(row("azure-ai/Llama-4-Maverick")).toHaveTextContent("paused");
    // add a price, then the approval goes through
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(within(row("azure-ai/Llama-4-Maverick")).getByRole("button", { name: /^Add price/ }));
    const price = await screen.findByRole("dialog", { name: /Add a price/ });
    await user.type(within(price).getByLabelText(/Input, per million tokens/), "0.25");
    await user.type(within(price).getByLabelText(/Output, per million tokens/), "1.00");
    await user.type(within(price).getByLabelText(/Effective from/), "2026-03-01T00:00:00Z");
    await user.type(within(price).getByLabelText(/Source/), "vendor price list");
    await user.type(within(price).getByLabelText(/^Reason/), "list price");
    await user.click(within(price).getByRole("button", { name: "Add price" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(row("azure-ai/Llama-4-Maverick")).toHaveTextContent("$0.25 in"));
    await user.click(within(row("azure-ai/Llama-4-Maverick")).getByRole("button", { name: /^Approve/ }));
    const again = await screen.findByRole("dialog");
    await user.type(within(again).getByLabelText(/^Reason/), "priced and reviewed");
    await user.click(within(again).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(row("azure-ai/Llama-4-Maverick")).toHaveTextContent("approved"));
  });

  it("an edit that widens what an entry accepts warns first, then shows it was paused", async () => {
    const { user } = await open();
    await waitFor(() => row("azure-openai/gpt-5.4-mini"));
    await user.click(within(row("azure-openai/gpt-5.4-mini")).getByRole("button", { name: /^Edit/ }));
    const dialog = await screen.findByRole("dialog", { name: /Edit azure-openai\/gpt-5.4-mini/ });
    expect(within(dialog).queryByText(/paused again/i)).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Processing scope"), "canada");
    expect(within(dialog).getByText(/accepts customer data it did not before/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/paused until an admin approves it again/i)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Reason/), "this deployment is in Canada");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    const notice = await screen.findByRole("status", { name: "Last change" });
    expect(notice).toHaveTextContent(/paused: approve it again with a reason/);
    expect(row("azure-openai/gpt-5.4-mini")).toHaveTextContent("paused");
  });

  it("a narrowing edit does not warn", async () => {
    const { user } = await open();
    await waitFor(() => row("azure-openai/gpt-5.4-ca"));
    await user.click(within(row("azure-openai/gpt-5.4-ca")).getByRole("button", { name: /^Edit/ }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Processing scope"), "global");
    expect(within(dialog).queryByText(/did not before/i)).not.toBeInTheDocument();
  });

  it("customer regions: a write that widens lists the entries paused again", async () => {
    const { user } = await open();
    await waitFor(() => row("azure-ai/Mistral-Large"));
    await user.click(screen.getByRole("button", { name: /^Customer regions/ }));
    const dialog = await screen.findByRole("dialog", { name: /customer regions/i });
    await user.type(within(dialog).getByLabelText(/^Regions/), "eu-west");
    expect(within(dialog).getByText(/azure-ai\/Mistral-Large/)).toBeInTheDocument(); // told before sending
    await user.type(within(dialog).getByLabelText(/^Reason/), "EU customers");
    await user.click(within(dialog).getByRole("button", { name: "Save regions" }));
    const notice = await screen.findByRole("status", { name: "Last change" });
    expect(notice).toHaveTextContent("Paused again, approve each again with a reason: azure-ai/Mistral-Large");
    expect(row("azure-ai/Mistral-Large")).toHaveTextContent("paused");
  });

  it("add model: the new entry starts paused", async () => {
    const { user } = await open();
    await user.click(await screen.findByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a model" });
    await user.type(within(dialog).getByLabelText("Provider"), "azure-openai");
    await user.type(within(dialog).getByLabelText("Model"), "gpt-5.4-nano");
    await user.type(within(dialog).getByLabelText("Family"), "openai");
    await user.selectOptions(within(dialog).getByLabelText("Processing scope"), "canada");
    expect(within(dialog).getByText(/starts paused/i)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Reason/), "cheap tier");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(row("azure-openai/gpt-5.4-nano")).toHaveTextContent("paused"));
  });

  it("remove: refused with the server's reason while an agent uses it", async () => {
    const { user } = await open();
    await waitFor(() => row("fake/dev-1"));
    // pause an assigned model, then try to remove it
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Pause/ }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "under review");
    await user.click(within(dialog).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Remove/ }));
    dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "clean up");
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/is still assigned to agent=dana/);
  });

  it("history: the ledger events with the reason a person gave", async () => {
    const { user } = await open();
    await waitFor(() => row("fake/dev-1"));
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Pause/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "under review");
    await user.click(within(dialog).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "History" }));
    const history = await screen.findByRole("dialog", { name: "Catalog history" });
    expect(await within(history).findByText("Catalog seeded from the team configuration")).toBeInTheDocument();
    expect(within(history).getByText("Paused fake/dev-1")).toBeInTheDocument();
    expect(within(history).getByText(/under review/)).toBeInTheDocument();
  });
});

describe("read-only views", () => {
  it("a viewer sees the catalog and assignments with no action buttons and no admin-only request", async () => {
    const client = createFixtureClient({ role: "viewer" });
    const events = vi.spyOn(client.models, "events");
    const connections = vi.spyOn(client.models, "connections");
    await open("/models/catalog", client);
    await waitFor(() => row("fake/dev-1"));
    expect(screen.getByText(/Read only/)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: ACTIONS })).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "History" })).not.toBeInTheDocument();
    expect(events).not.toHaveBeenCalled();
    expect(connections).not.toHaveBeenCalled();
  });

  it("a viewer's assignments are a plain table with no picker", async () => {
    await open("/models/assignments", createFixtureClient({ role: "viewer" }));
    const list = await screen.findByRole("list", { name: "Model assignments" });
    expect(within(list).getByText("agent=dana")).toBeInTheDocument();
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryAllByRole("button", { name: ACTIONS })).toHaveLength(0);
  });

  it("connections and resolution say they are for admins", async () => {
    await open("/models/connections", createFixtureClient({ role: "viewer" }));
    expect(await screen.findByText(/Only an admin can see connections/)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /Connect|Test/ })).toHaveLength(0);
  });

  it("nobody signed in is read-only too, and says how to sign in", async () => {
    const fixtures = createFixtureClient();
    const client = serverLike({ models: fixtures.models, session: { current: () => Promise.resolve(null) } });
    await open("/models/catalog", client);
    await waitFor(() => row("fake/dev-1"));
    expect(screen.getByText(/not signed in/i)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: ACTIONS })).toHaveLength(0);
  });

  it("a server that refuses a write with 403 forbidden says only an admin can", async () => {
    const fixtures = createFixtureClient();
    const client = serverLike({
      models: { ...fixtures.models, pause: () => Promise.reject(new ProblemError("your role doesn't allow this", 403, "forbidden")) },
      session: fixtures.session,
    });
    const { user } = await open("/models/catalog", client);
    await waitFor(() => row("fake/dev-1"));
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Pause/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "under review");
    await user.click(within(dialog).getByRole("button", { name: "Pause" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Only an admin can do this/);
  });
});

describe("states", () => {
  const empty: Catalog = { seeded: false, configDrift: false, ignoredEvents: 0, customerRegions: [], entries: [] };

  it("a clean install has no catalog and says what to do", async () => {
    const fixtures = createFixtureClient();
    await open("/models/catalog", serverLike({ models: { ...fixtures.models, catalog: () => Promise.resolve(empty) }, session: fixtures.session }));
    expect(await screen.findByText(/No catalog yet/)).toBeInTheDocument();
    expect(screen.getByText(/no real model can run/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add model" })).toBeInTheDocument();
  });

  it("warns when the team file's models section is ignored, and about ignored ledger events", async () => {
    const fixtures = createFixtureClient();
    const base = await fixtures.models.catalog();
    await open("/models/catalog", serverLike({ models: { ...fixtures.models, catalog: () => Promise.resolve({ ...base, configDrift: true, ignoredEvents: 2 }) }, session: fixtures.session }));
    expect(await screen.findByText(/models: section of the team configuration is ignored/)).toBeInTheDocument();
    expect(screen.getByText(/2 catalog entries in the ledger are ignored/)).toBeInTheDocument();
  });

  it("shows a loading state, then the data", async () => {
    renderAt("/models/catalog", createFixtureClient());
    expect(await screen.findByRole("status", { name: "Loading" })).toBeInTheDocument();
    await waitFor(() => row("fake/dev-1"));
  });

  it("a source that cannot answer says so and offers a retry", async () => {
    await open("/models/catalog", createUnavailableClient());
    expect(await screen.findByText(/can't give the model catalog yet/)).toBeInTheDocument();
    // the page and the organization status bar (E19) each offer their own retry
    const alert = screen.getByText(/can't give the model catalog yet/).closest("[role=alert]");
    expect(alert).not.toBeNull();
    if (alert instanceof HTMLElement) {
      expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    }
  });

  it("an error from the server is named in words", async () => {
    const fixtures = createFixtureClient();
    await open(
      "/models/catalog",
      serverLike({ models: { ...fixtures.models, catalog: () => Promise.reject(new ProblemError("boom", 500, "internal_error")) }, session: fixtures.session }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't load the model catalog/);
  });
});

describe("Assignments (admin)", () => {
  it("shows where each model comes from; the picker offers only approved entries", async () => {
    await open("/models/assignments");
    const list = await screen.findByRole("list", { name: "Model assignments" });
    const dana = within(list).getByText("agent=dana").closest("li") as HTMLElement;
    expect(dana).toHaveTextContent("fake/dev-1");
    expect(dana).toHaveTextContent("role config");
    const picker = within(dana).getByLabelText(/Model for agent=dana/);
    const options = within(picker).getAllByRole("option").map((o) => o.textContent);
    expect(options).toContain("azure-openai/gpt-5.4-mini");
    expect(options.join(" ")).not.toContain("Llama-4-Maverick"); // paused
  });

  it("choosing a model asks for a reason, then the row says 'agent ledger'", async () => {
    const { user } = await open("/models/assignments");
    const list = await screen.findByRole("list", { name: "Model assignments" });
    const dana = within(list).getByText("agent=dana").closest("li") as HTMLElement;
    await user.selectOptions(within(dana).getByLabelText(/Model for agent=dana/), "azure-openai/gpt-5.4-ca");
    const dialog = await screen.findByRole("dialog", { name: /Assign azure-openai\/gpt-5.4-ca to agent=dana/ });
    await user.type(within(dialog).getByLabelText(/^Reason/), "trial in Canada");
    await user.click(within(dialog).getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(within(list).getByText("agent=dana").closest("li")).toHaveTextContent("agent ledger"));
    expect(screen.getByRole("status", { name: "Last change" })).toHaveTextContent(/ledger #/);
  });

  it("a ledger assignment can be cleared back to the configuration", async () => {
    const { user } = await open("/models/assignments");
    const list = await screen.findByRole("list", { name: "Model assignments" });
    let dana = within(list).getByText("agent=dana").closest("li") as HTMLElement;
    await user.selectOptions(within(dana).getByLabelText(/Model for agent=dana/), "azure-openai/gpt-5.4-ca");
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "trial in Canada");
    await user.click(within(dialog).getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(within(list).getByText("agent=dana").closest("li")).toHaveTextContent("agent ledger"));
    dana = within(list).getByText("agent=dana").closest("li") as HTMLElement;
    await user.click(within(dana).getByRole("button", { name: /^Clear/ }));
    dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "back to default");
    await user.click(within(dialog).getByRole("button", { name: "Clear assignment" }));
    await waitFor(() => expect(within(list).getByText("agent=dana").closest("li")).toHaveTextContent("role config"));
  });

  it("an agent with no model is shown as none, not invented", async () => {
    await open("/models/assignments");
    const list = await screen.findByRole("list", { name: "Model assignments" });
    expect(within(list).getByText("agent=chief").closest("li")).toHaveTextContent(/no model/i);
  });
});

describe("Connections (admin)", () => {
  it("starts empty and connects a provider; the entries of that provider show as connected", async () => {
    const { user } = await open("/models/connections");
    expect(await screen.findByText(/No connections yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Connect a provider" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect a provider" });
    await user.selectOptions(within(dialog).getByLabelText("Provider"), "azure-openai");
    await user.type(within(dialog).getByLabelText("Endpoint"), "http://nope");
    expect(within(dialog).getByText(/must be https:\/\/<name>.openai.azure.com/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Connect" })).toBeDisabled();
    await user.clear(within(dialog).getByLabelText("Endpoint"));
    await user.type(within(dialog).getByLabelText("Endpoint"), "https://example-openai.openai.azure.com");
    await user.click(within(dialog).getByRole("button", { name: "Connect" })); // a first connection needs no reason
    const list = await screen.findByRole("list", { name: "Connections" });
    expect(within(list).getByText("azure-openai")).toBeInTheDocument();
    expect(list).toHaveTextContent("https://example-openai.openai.azure.com");
    expect(list).toHaveTextContent("active");
  });

  it("never offers a place to type a key", async () => {
    const { user } = await open("/models/connections");
    await user.click(await screen.findByRole("button", { name: "Connect a provider" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText(/key|secret|token|password/i)).not.toBeInTheDocument();
    expect(within(dialog).getByText(/No key is stored or typed here/)).toBeInTheDocument();
  });

  async function connected() {
    const ctx = await open("/models/connections");
    await ctx.user.click(await screen.findByRole("button", { name: "Connect a provider" }));
    const dialog = await screen.findByRole("dialog");
    await ctx.user.selectOptions(within(dialog).getByLabelText("Provider"), "azure-openai");
    await ctx.user.type(within(dialog).getByLabelText("Endpoint"), "https://example-openai.openai.azure.com");
    await ctx.user.click(within(dialog).getByRole("button", { name: "Connect" }));
    const list = await screen.findByRole("list", { name: "Connections" });
    return { ...ctx, list };
  }

  it("shows a data-location warning: where customer data can go through this endpoint, and that the region is not verified", async () => {
    const { list } = await connected();
    const item = within(list).getByText("azure-openai").closest("li") as HTMLElement;
    expect(item).toHaveTextContent(/Customer data can be sent to this endpoint/i);
    expect(item).toHaveTextContent("azure-openai/gpt-5.4-ca");
    expect(item).toHaveTextContent(/cannot be checked against the endpoint/i);
    expect(item).toHaveTextContent(/synthetic data only/i); // gpt-5.4-mini is global
  });

  it("Test connection shows the result text and latency; a failure shows its code", async () => {
    const { user, list } = await connected();
    const item = within(list).getByText("azure-openai").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: /^Test/ }));
    const ok = await within(item).findByRole("status", { name: "Test result" });
    expect(ok).toHaveTextContent("Test passed in 42 ms");
    expect(ok).toHaveTextContent("the endpoint answered and accepted the Entra login");
    // an endpoint the sample server cannot reach
    await user.click(within(item).getByRole("button", { name: /^Repoint/ }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText("Endpoint"));
    await user.type(within(dialog).getByLabelText("Endpoint"), "https://down.openai.azure.com");
    await user.type(within(dialog).getByLabelText(/^Reason/), "moved to another resource");
    await user.click(within(dialog).getByRole("button", { name: "Repoint" }));
    await screen.findByRole("status", { name: "Last change" });
    await user.click(within(item).getByRole("button", { name: /^Test/ }));
    const failed = await within(item).findByText(/Test failed \(unreachable\)/);
    expect(failed).toHaveTextContent("could not be reached");
  });

  it("a rate-limited test says how long to wait", async () => {
    const fixtures = createFixtureClient();
    const client = serverLike({
      models: { ...fixtures.models, testConnection: () => Promise.reject(new ProblemError("limit", 429, "rate_limited", 7)) },
      session: fixtures.session,
    });
    const ctx = await open("/models/connections", client);
    await ctx.user.click(await screen.findByRole("button", { name: "Connect a provider" }));
    const dialog = await screen.findByRole("dialog");
    await ctx.user.selectOptions(within(dialog).getByLabelText("Provider"), "azure-ai");
    await ctx.user.type(within(dialog).getByLabelText("Endpoint"), "https://ais-x.cognitiveservices.azure.com");
    await ctx.user.click(within(dialog).getByRole("button", { name: "Connect" }));
    const item = (await within(await screen.findByRole("list", { name: "Connections" })).findByText("azure-ai")).closest("li") as HTMLElement;
    await ctx.user.click(within(item).getByRole("button", { name: /^Test/ }));
    expect(await within(item).findByRole("alert")).toHaveTextContent("try again in 7 s");
  });

  it("a repoint needs a reason, warns which approved entries will be paused, and lists them after", async () => {
    const { user, list } = await connected();
    const item = within(list).getByText("azure-openai").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: /^Repoint/ }));
    const dialog = await screen.findByRole("dialog", { name: /Repoint azure-openai/ });
    await user.clear(within(dialog).getByLabelText("Endpoint"));
    await user.type(within(dialog).getByLabelText("Endpoint"), "https://aoai-other.openai.azure.com");
    expect(within(dialog).getByText(/will be paused and need a fresh approval/)).toBeInTheDocument();
    expect(within(dialog).getByText(/azure-openai\/gpt-5.4-mini, azure-openai\/gpt-5.4-ca/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Repoint" })).toBeDisabled(); // no reason yet
    await user.type(within(dialog).getByLabelText(/^Reason/), "moved to the other resource");
    await user.click(within(dialog).getByRole("button", { name: "Repoint" }));
    const notice = await screen.findByRole("status", { name: "Last change" });
    expect(notice).toHaveTextContent("Paused again, approve each again with a reason: azure-openai/gpt-5.4-mini, azure-openai/gpt-5.4-ca");
    expect(notice).toHaveTextContent(/new endpoint/);
  });

  it("disable keeps the endpoint; disconnect removes the connection", async () => {
    const { user, list } = await connected();
    let item = within(list).getByText("azure-openai").closest("li") as HTMLElement;
    await user.click(within(item).getByRole("button", { name: /^Disable/ }));
    let dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(within(list).getByText("azure-openai").closest("li")).toHaveTextContent("disabled"));
    item = within(list).getByText("azure-openai").closest("li") as HTMLElement;
    expect(within(item).getByRole("button", { name: /^Enable/ })).toBeInTheDocument();
    await user.click(within(item).getByRole("button", { name: /^Disconnect/ }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    expect(await screen.findByText(/No connections yet/)).toBeInTheDocument();
  });
});

describe("Resolution (admin)", () => {
  it("shows a pass for each agent, with its family, processing, endpoint and price basis", async () => {
    await open("/models/resolution");
    expect(await screen.findByText(/Every agent and role resolves for synthetic data/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Resolved models" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(list).toHaveTextContent("agent=dana");
    expect(list).toHaveTextContent("fake/dev-1");
  });

  it("shows the blocks with the server's texts once a model is paused, and no list of passes", async () => {
    const { user } = await open("/models/catalog");
    await waitFor(() => row("fake/dev-1"));
    await user.click(within(row("fake/dev-1")).getByRole("button", { name: /^Pause/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "under review");
    await user.click(within(dialog).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(within(screen.getByRole("navigation", { name: "Models sections" })).getByRole("link", { name: "Resolution" }));
    const blocked = await screen.findByRole("list", { name: "Blocks" });
    expect(blocked).toHaveTextContent("model blocked [model_paused] agent=dana");
    expect(screen.queryByRole("list", { name: "Resolved models" })).not.toBeInTheDocument();
    expect(screen.getByText(/A run would be refused/)).toBeInTheDocument();
  });

  it("can check for customer data", async () => {
    const { user } = await open("/models/resolution");
    await screen.findByRole("list", { name: "Resolved models" });
    await user.selectOptions(screen.getByLabelText("Data class"), "customer");
    expect(await screen.findByText(/Every agent and role resolves for customer data/)).toBeInTheDocument();
  });

  it("says that gate pairings are checked by `models check` and at run start", async () => {
    await open("/models/resolution");
    expect(await screen.findByText(/gate pairings/i)).toBeInTheDocument();
  });
});
