import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { parseDocument, serializeDocument } from "../document";
import { createBuilderStore } from "../store";
import { BuilderStoreContext } from "../store-context";
import { BUILT_IN_TEMPLATES } from "../templates";
import { Inspector } from "./inspector";

function mount(select: string[], edit?: (d: ReturnType<typeof template>) => ReturnType<typeof template>) {
  const base = template();
  const store = createBuilderStore(edit === undefined ? base : edit(base));
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

function template() {
  const doc = BUILT_IN_TEMPLATES.find((t) => t.id === "dev-team")?.document;
  if (doc === undefined) throw new Error("no template");
  return doc;
}

describe("Run plan section", () => {
  it("asks for criteria per gate of the role's preset and shows what is missing", () => {
    mount(["dana"]);
    expect(screen.getByRole("heading", { name: "Run plan" })).toBeInTheDocument();
    for (const id of ["rule", "test-command", "verifier"]) {
      expect(screen.getByRole("textbox", { name: `Criteria for gate ${id}` })).toHaveValue("");
    }
    expect(screen.getByText(/say which criteria these gates check: \['rule', 'test-command', 'verifier'\]/)).toBeInTheDocument();
  });

  it("stores checks in the document, shows the instantiated gates and survives save and load", async () => {
    const store = mount(["dana"]);
    await userEvent.type(screen.getByRole("textbox", { name: "Criteria for gate rule" }), "style, lint");
    await userEvent.type(screen.getByRole("textbox", { name: "Criteria for gate test-command" }), "green");
    await userEvent.type(screen.getByRole("textbox", { name: "Criteria for gate verifier" }), "done");
    expect(store.getState().doc.config.checks).toEqual([
      { role: "developer", checks: { rule: ["style", "lint"], "test-command": ["green"], verifier: ["done"] } },
    ]);
    expect(screen.queryByText(/say which criteria/)).not.toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Instantiated gates" });
    expect(list).toHaveTextContent("rule");
    expect(list).toHaveTextContent("style, lint");
    const loaded = parseDocument(JSON.parse(serializeDocument(store.getState().doc)));
    expect(loaded.ok && loaded.value.config.checks).toEqual(store.getState().doc.config.checks);
  });

  it("lists the eval suites the role needs, its own then the preset's", () => {
    mount(["dana"], (d) => ({
      ...d,
      config: {
        ...d.config,
        evals: [{ role: "developer", suites: ["own-suite"] }],
        presets: { ...d.config.presets, gate: d.config.presets.gate.map((g) => (g.id === "developer-gates" ? { ...g, evals: ["own-suite", "preset-suite"] } : g)) },
      },
    }));
    const list = screen.getByRole("list", { name: "Required eval suites" });
    expect(list.textContent).toBe("own-suitepreset-suite");
  });

  it("is absent for a role without a gate preset", () => {
    mount(["chief"]);
    expect(screen.queryByRole("heading", { name: "Run plan" })).not.toBeInTheDocument();
  });

  it("says it is a planning view and does not claim the run carries the checks", () => {
    mount(["dana"]);
    const text = document.body.textContent;
    expect(text).not.toMatch(/carries these gates/);
    expect(text).toMatch(/planning view/i);
    expect(text).toMatch(/kept in this organization draft/i);
    expect(text).toMatch(/not yet sent when a run starts/i);
  });

  it("keeps an invalid criterion draft per agent when another agent is selected and reselected", async () => {
    const store = mount(["dana"]);
    const box = () => screen.getByRole("textbox", { name: "Criteria for gate rule" });
    await userEvent.type(box(), "Bad Id");
    expect(screen.getByText(/'Bad Id' is not a valid criterion id/)).toBeInTheDocument();
    act(() => { store.getState().setSelection([]); });
    act(() => { store.getState().setSelection(["dana"]); });
    expect(box()).toHaveValue("Bad Id");
    expect(screen.getByText(/'Bad Id' is not a valid criterion id/)).toBeInTheDocument();
    await userEvent.clear(box());
    await userEvent.type(box(), "fine");
    act(() => { store.getState().setSelection([]); });
    act(() => { store.getState().setSelection(["dana"]); });
    expect(box()).toHaveValue("fine");
  });
});
