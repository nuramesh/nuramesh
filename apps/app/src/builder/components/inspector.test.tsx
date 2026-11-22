import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { createBuilderStore } from "../store";
import { BuilderStoreContext } from "../store-context";
import { BUILT_IN_TEMPLATES } from "../templates";
import { Inspector } from "./inspector";

function mount(select: string[]) {
  const doc = BUILT_IN_TEMPLATES.find((t) => t.id === "dev-team")?.document;
  if (doc === undefined) throw new Error("no template");
  const store = createBuilderStore(doc);
  store.getState().setSelection(select);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BuilderStoreContext value={store}>
        <Inspector />
      </BuilderStoreContext>
    </QueryClientProvider>,
  );
  return store;
}

describe("Inspector", () => {
  it("renders an agent's settings (every section) without looping", () => {
    mount(["dana"]);
    for (const heading of ["Identity", "Model", "Budget", "Permissions", "Gates", "Evals"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Dana (developer)");
    expect(screen.getByText("Default: deny")).toBeInTheDocument();
  });

  it("shows the verification line for a verifier", () => {
    mount(["rhea"]);
    expect(screen.getByRole("heading", { name: "Verifies" })).toBeInTheDocument();
    expect(screen.getByText("Dana (developer)")).toBeInTheDocument();
  });

  it("switches to the YAML tab and follows edits made elsewhere", async () => {
    const store = mount(["dana"]);
    await userEvent.click(screen.getByRole("tab", { name: "YAML" }));
    const yaml = screen.getByRole<HTMLTextAreaElement>("textbox", { name: /YAML for/ });
    expect(yaml.value).toContain("name: Dana (developer)");
    act(() => {
      store.getState().edit("Rename", (d) => ({ ...d, org: { ...d.org, agents: d.org.agents.map((a) => (a.id === "dana" ? { ...a, name: "Ada" } : a)) } }));
    });
    expect(yaml.value).toContain("name: Ada");
  });

  it("renders a person and the organization when nothing is selected", () => {
    const store = mount([]);
    expect(screen.getByRole("heading", { name: "Organization" })).toBeInTheDocument();
    let id = "";
    act(() => {
      id = store.getState().addPersonNode();
    });
    expect(screen.getByRole("heading", { name: /Human approver/ })).toBeInTheDocument();
    expect(screen.getByText(id)).toBeInTheDocument();
  });
});
