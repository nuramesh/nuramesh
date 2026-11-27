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
    mount(["developer"]);
    for (const heading of ["Identity", "Model", "Budget", "Permissions", "Gates", "Evals"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Developer");
    expect(screen.getByText("Default: deny")).toBeInTheDocument();
  });

  it("shows the verification line for a verifier", () => {
    mount(["reviewer"]);
    expect(screen.getByRole("heading", { name: "Verifies" })).toBeInTheDocument();
    expect(screen.getAllByText("Developer").length).toBeGreaterThan(0);
  });

  it("switches to the YAML tab and follows edits made elsewhere", async () => {
    const store = mount(["developer"]);
    await userEvent.click(screen.getByRole("tab", { name: "YAML" }));
    const yaml = screen.getByRole<HTMLTextAreaElement>("textbox", { name: /YAML for/ });
    expect(yaml.value).toContain("name: Developer");
    act(() => {
      store.getState().edit("Rename", (d) => ({ ...d, org: { ...d.org, agents: d.org.agents.map((a) => (a.id === "developer" ? { ...a, name: "Ada" } : a)) } }));
    });
    expect(yaml.value).toContain("name: Ada");
  });

  it("adds a gate only once its settings are given (issue #1407)", async () => {
    const store = mount(["developer"]);
    expect(screen.getByText(/No gates: this role's work is not checked/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Add a gate" }), "verifier");
    // choosing a kind writes nothing: the settings come first
    expect(store.getState().doc.config.presets.gate).toEqual([]);
    const add = screen.getByRole("button", { name: "Add gate" });
    expect(add).toBeDisabled();
    const box = screen.getByRole("textbox", { name: "Settings for the Independent verifier (YAML)" });
    await userEvent.type(box, "- not a map");
    expect(add).toBeDisabled();
    expect(screen.getByText("Must be an object")).toBeInTheDocument();
    await userEvent.clear(box);
    await userEvent.type(box, "verifier: reviewer");
    await userEvent.click(add);
    expect(store.getState().doc.config.presets.gate).toEqual([
      { id: "developer-gates", name: "Developer gates", description: "", gates: [{ id: "verifier", kind: "verifier", required: true, config: { verifier: "reviewer" } }], evals: [] },
    ]);
    expect(screen.getByRole("button", { name: "Remove Independent verifier" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add gate" })).not.toBeInTheDocument();
  });

  it("does not carry a half-filled add-gate form over to another role (issue #1407)", async () => {
    const store = mount(["developer"]);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Add a gate" }), "verifier");
    await userEvent.type(screen.getByRole("textbox", { name: "Settings for the Independent verifier (YAML)" }), "verifier: reviewer");
    expect(screen.getByRole("button", { name: "Add gate" })).toBeEnabled();
    act(() => {
      store.getState().setSelection(["reviewer"]);
    });
    expect(screen.queryByRole("button", { name: "Add gate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Settings for the/ })).not.toBeInTheDocument();
    expect(store.getState().doc.config.presets.gate).toEqual([]);
  });

  it("marks a gate that has no settings (from an older saved file)", () => {
    const store = mount(["developer"]);
    act(() => {
      store.getState().edit("Old file", (d) => ({
        ...d,
        config: {
          ...d.config,
          presets: {
            ...d.config.presets,
            gate: [{ id: "developer-gates", name: "Developer gates", description: "", gates: [{ id: "rule", kind: "rule", required: true }], evals: [] }],
            assignments: [{ role: "developer", policy_presets: [], gate_preset: "developer-gates" }],
          },
        },
      }));
    });
    expect(screen.getByText(/No settings: no task can carry this gate/)).toBeInTheDocument();
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
