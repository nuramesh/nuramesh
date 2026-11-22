import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";
import { exportYaml } from "@/builder/config-yaml";
import { BUILT_IN_TEMPLATES } from "@/builder/templates";

function renderAt(client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: ["/templates"] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

function requireDoc(id: string) {
  const found = BUILT_IN_TEMPLATES.find((t) => t.id === id);
  if (found === undefined) throw new Error(id);
  return found.document;
}

function card(heading: HTMLElement): HTMLElement {
  const article = heading.closest("article");
  if (article === null) throw new Error("no card");
  return article;
}

describe("Templates", () => {
  it("lists the built-in templates, with roles", async () => {
    renderAt();
    const cards = await screen.findByRole("region", { name: "Templates" });
    const names = within(cards).getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(names).toEqual(expect.arrayContaining(["Dev team", "Code review", "Support team", "QA and testing team", "DevOps team", "Sales team", "Blank organization"]));
    const support = card(within(cards).getByRole("heading", { name: "Support team" }));
    expect(within(support).getByRole("button", { name: "Use template" })).toBeInTheDocument();
    const dev = card(within(cards).getByRole("heading", { name: "Dev team" }));
    expect(within(dev).getByRole("button", { name: "Use template" })).toBeInTheDocument();
    expect(within(dev).getByRole("button", { name: "Preview YAML" })).toBeInTheDocument();
    expect(within(within(dev).getByRole("list", { name: "Roles" })).getAllByRole("listitem").length).toBeGreaterThan(2);
  });

  it("hides the Planned filter when no template is planned", async () => {
    const user = userEvent.setup();
    renderAt();
    await screen.findByRole("heading", { name: "Dev team" });
    expect(screen.queryByRole("button", { name: "Planned" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Available now" }));
    expect(screen.getByRole("heading", { name: "Support team" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Dev team" })).toBeInTheDocument();
  });

  it("hides the Planned filter on the local catalog a server without templates falls back to", async () => {
    renderAt(createUnavailableClient());
    await screen.findByRole("heading", { name: "Dev team" });
    expect(screen.queryByRole("button", { name: "Planned" })).not.toBeInTheDocument();
  });

  it("shows the Planned filter when a template is planned, and it lists only that one", async () => {
    const user = userEvent.setup();
    const base = createFixtureClient();
    const list = await base.templates.list();
    const client: ApiClient = {
      ...base,
      templates: { ...base.templates, list: () => Promise.resolve([...list, { id: "later", name: "Later team", description: "Soon.", availability: "planned", origin: "built-in", roles: [], meta: "", latestVersion: null, updatedAt: null }]) },
    };
    renderAt(client);
    await screen.findByRole("heading", { name: "Dev team" });
    await user.click(screen.getByRole("button", { name: "Planned" }));
    expect(screen.getByRole("heading", { name: "Later team" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Support team" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "My templates" })).not.toBeInTheDocument();
  });

  it("previews a template's YAML, exactly what Export YAML writes", async () => {
    const user = userEvent.setup();
    renderAt();
    const dev = card(await screen.findByRole("heading", { name: "Dev team" }));
    await user.click(within(dev).getByRole("button", { name: "Preview YAML" }));
    const dialog = await screen.findByRole("dialog", { name: "Dev team (YAML)" });
    expect(within(dialog).getByRole("region", { name: "YAML" }).textContent).toBe(exportYaml(requireDoc("dev-team")));
  });

  it("Use template opens the builder with that organization", async () => {
    const user = userEvent.setup();
    const router = renderAt();
    const dev = card(await screen.findByRole("heading", { name: "Dev team" }));
    await user.click(within(dev).getByRole("button", { name: "Use template" }));
    expect(await screen.findByRole("region", { name: "Organization canvas" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/builder");
    expect(await screen.findByText("Loaded template Dev team. Undo to go back.")).toBeInTheDocument();
    expect(await screen.findByTestId("builder-summary")).toHaveTextContent("5 agents");
  });

  it("shows My templates with version and date, lets you list versions and use one", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    const dev = BUILT_IN_TEMPLATES[0]?.document;
    if (dev === undefined) throw new Error("no template");
    await client.orgs.update(dev.org, (await client.orgs.get()).versionHash);
    await client.templates.saveAs({ id: "my-team", name: "My team", description: "first" });
    await client.templates.saveAs({ id: "my-team", name: "My team v2", description: "second" });
    renderAt(client);
    const mycard = card(await screen.findByRole("heading", { name: "My team v2" }));
    expect(within(mycard).getByText(/v2 · updated/)).toBeInTheDocument();
    await user.click(within(mycard).getByRole("button", { name: "Show versions" }));
    const versions = await within(mycard).findByRole("list", { name: "Versions of My team v2" });
    expect(within(versions).getAllByRole("listitem")).toHaveLength(2);
    await user.click(within(versions).getByRole("button", { name: "Preview v1" }));
    expect(await screen.findByRole("dialog", { name: "My team v2 v1 (YAML)" })).toBeInTheDocument();
  });

  it("says plainly when nothing is saved, and when the source can't list templates", async () => {
    renderAt();
    expect(await screen.findByText(/Nothing saved yet/)).toBeInTheDocument();
  });

  it("a source that can't list templates still shows the built-in ones, and says so", async () => {
    renderAt(createUnavailableClient());
    expect(await screen.findByRole("alert")).toHaveTextContent("can't list templates yet");
    expect(await screen.findByRole("heading", { name: "Dev team" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Code review" })).toBeInTheDocument();
  });

  it("using a saved version opens that version in the builder", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    await client.templates.saveAs({ id: "my-team", name: "My team", description: "first" });
    renderAt(client);
    const mine = card(await screen.findByRole("heading", { name: "My team" }));
    await user.click(within(mine).getByRole("button", { name: "Show versions" }));
    await user.click(await within(mine).findByRole("button", { name: "Use v1" }));
    expect(await screen.findByText("Loaded template My team v1. Undo to go back.")).toBeInTheDocument();
  });
});

describe("Import YAML", () => {
  const open = async () => {
    const user = userEvent.setup();
    const router = renderAt();
    await screen.findByRole("heading", { name: "Dev team" });
    await user.click(screen.getByRole("button", { name: "Import YAML" }));
    return { user, router, dialog: await screen.findByRole("dialog", { name: "Import YAML" }) };
  };

  it("can't be submitted empty, and explains bad YAML without opening the builder", async () => {
    const { user, router, dialog } = await open();
    expect(within(dialog).getByRole("button", { name: "Open in the builder" })).toBeDisabled();
    await user.click(within(dialog).getByLabelText("YAML"));
    await user.paste("prices:\n  prices: []\norg: 5");
    await user.click(within(dialog).getByRole("button", { name: "Open in the builder" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/prices: The builder doesn't edit/);
    expect(router.state.location.pathname).toBe("/templates");
  });

  it("opens valid YAML in the builder", async () => {
    const { user, router, dialog } = await open();
    await user.click(within(dialog).getByLabelText("YAML"));
    await user.paste(exportYaml(requireDoc("code-review")));
    await user.click(within(dialog).getByRole("button", { name: "Open in the builder" }));
    expect(await screen.findByRole("region", { name: "Organization canvas" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/builder");
    expect(await screen.findByText("Imported YAML. Undo to go back.")).toBeInTheDocument();
  });
});

describe("Handing a document to the builder", () => {
  it("loads over the saved draft as one undo step, clears its state, and survives a reload", async () => {
    const { saveDraft } = await import("@/builder/storage");
    const { sampleDoc } = await import("@/builder/test-data");
    saveDraft(sampleDoc()); // the person's earlier draft: 4 agents
    const user = userEvent.setup();
    const router = renderAt();
    const dev = card(await screen.findByRole("heading", { name: "Dev team" }));
    await user.click(within(dev).getByRole("button", { name: "Use template" }));
    expect(await screen.findByText("Loaded template Dev team. Undo to go back.")).toBeInTheDocument();
    expect(await screen.findByTestId("builder-summary")).toHaveTextContent("5 agents");
    await vi.waitFor(() => {
      expect(router.state.location.state).toBeNull();
    });
    // a reload: the template was saved as the draft, and nothing is applied a second time
    const { loadDraft } = await import("@/builder/storage");
    expect(loadDraft()?.org.agents).toHaveLength(5);
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByTestId("builder-summary")).toHaveTextContent("4 agents");
  });
});
