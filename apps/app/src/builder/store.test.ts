import { describe, expect, it, vi } from "vitest";

import { blankDocument } from "./document";
import type { LayoutEngine } from "./layout-engine";
import { updateAgent } from "./ops";
import { DRAFT_KEY, TEMPLATES_KEY } from "./storage";
import { connectDraftAutosave, connectLayout, createBuilderStore, edgeId, reportOfEdge } from "./store";
import { cos, dev, sampleDoc } from "./test-data";
import { BUILT_IN_TEMPLATES } from "./templates";

describe("builder store", () => {
  it("puts a verifier next to the work, under the top agent, never under the selected agent", () => {
    const store = createBuilderStore(blankDocument());
    const s = () => store.getState();
    const boss = s().addAgentFromRole(cos);
    const worker = s().addAgentFromRole(dev); // reports to the selected boss
    s().focusAgent(worker); // a worker is selected
    const checker = s().addAgentFromRole({ ...dev, id: "reviewer", name: "Reviewer", kind: "verifier" });
    const reports = (id: string) => s().doc.org.agents.find((a) => a.id === id)?.reports_to;
    expect(reports(checker)).toBe(boss); // not under the worker, and the org stays valid
    expect(s().issues.errorCount).toBe(0);
    expect(s().announcement.text).toBe(`Added Reviewer reporting to ${boss}`);
  });

  it("never adds anyone under a selected verifier", () => {
    const store = createBuilderStore(blankDocument());
    const s = () => store.getState();
    const boss = s().addAgentFromRole(cos);
    const checker = s().addAgentFromRole({ ...dev, id: "reviewer", name: "Reviewer", kind: "verifier" });
    s().focusAgent(checker); // the verifier is selected
    const worker = s().addAgentFromRole(dev);
    expect(s().doc.org.agents.find((a) => a.id === worker)?.reports_to).toBe(boss);
    expect(s().issues.errorCount).toBe(0);
  });

  it("adds a first verifier to an empty org without a manager", () => {
    const store = createBuilderStore(blankDocument());
    const id = store.getState().addAgentFromRole({ ...dev, id: "reviewer", name: "Reviewer", kind: "verifier" });
    expect(store.getState().doc.org.agents.find((a) => a.id === id)?.reports_to).toBeNull();
  });

  it("records every edit for undo/redo and re-validates", () => {
    const store = createBuilderStore(blankDocument());
    const s = () => store.getState();
    expect(s().issues.issues.map((i) => i.code)).toEqual(["no_root"]);

    const boss = s().addAgentFromRole(cos);
    expect(s().selection.nodes).toEqual(new Set([boss]));
    const worker = s().addAgentFromRole(dev); // reports to the selected agent
    expect(s().doc.org.agents.find((a) => a.id === worker)?.reports_to).toBe(boss);
    expect(s().issues.issues).toEqual([]);

    s().edit("Rename", (d) => updateAgent(d, worker, { name: "" }));
    expect(s().issues.issues.map((i) => i.code)).toEqual(["invalid_field"]);
    expect(s().issues.byAgent.get(worker)?.errors).toBe(1);

    s().undo();
    expect(s().issues.issues).toEqual([]);
    s().undo();
    s().undo();
    expect(s().doc.org.agents).toEqual([]);
    expect(s().selection.nodes.size).toBe(0); // pruned
    s().redo();
    expect(s().doc.org.agents).toHaveLength(1);
    expect(s().announcement.text).toBe("Redid Add Chief of Staff");
  });

  it("connects, refuses loops, reconnects and deletes lines and agents", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    expect(s().connect("a1", "a")).toBe(false);
    expect(s().announcement.text).toMatch(/loop/);
    expect(s().connect("b", "a1")).toBe(true);
    expect(s().doc.org.agents.find((a) => a.id === "a1")?.reports_to).toBe("b");

    // Drag the end of b → a1 onto a: a1 loses its manager, a gets b.
    expect(s().reconnect("a1", "b", "a")).toBe(true);
    expect(s().doc.org.agents.find((a) => a.id === "a1")?.reports_to).toBeNull();
    expect(s().doc.org.agents.find((a) => a.id === "a")?.reports_to).toBe("b");
    s().undo();
    expect(s().doc.org.agents.find((a) => a.id === "a1")?.reports_to).toBe("b");

    s().setSelection(["b"], ["a"]);
    s().deleteSelection();
    expect(s().doc.org.agents.map((a) => a.id)).toEqual(["boss", "a", "a1"]);
    expect(s().doc.org.agents.find((a) => a.id === "a1")?.reports_to).toBe("boss"); // moved up
    expect(s().doc.org.agents.find((a) => a.id === "a")?.reports_to).toBeNull(); // its line was deleted
  });

  it("commits a drag once, on drop, and merges keyboard nudges", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().dragMove({ b: { x: 1, y: 1 } });
    s().dragMove({ b: { x: 2, y: 2 } });
    expect(s().history.past).toHaveLength(0);
    expect(s().doc.layout.positions.b).toBeUndefined();
    s().dragEnd({ b: { x: 3, y: 3 } });
    expect(s().dragging).toBeNull();
    expect(s().doc.layout.positions.b).toEqual({ x: 3, y: 3 });
    expect(s().history.past).toHaveLength(1);
    s().nudge({ b: { x: 8, y: 3 } });
    s().nudge({ b: { x: 13, y: 3 } });
    expect(s().history.past).toHaveLength(2);
    s().undo();
    expect(s().doc.layout.positions.b).toEqual({ x: 3, y: 3 });
  });

  it("selects all, copies, pastes with growing offsets, duplicates and cuts", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().selectAll();
    expect(s().selection.nodes.size).toBe(4);
    expect(s().selection.edges).toEqual(new Set(["a", "a1", "b"]));
    s().setSelection(["a"]);
    expect(s().copy()).toBe(true);
    s().paste();
    s().paste();
    expect(s().doc.layout.positions["a-2"]).toEqual({ x: 50, y: 60 });
    expect(s().doc.layout.positions["a-3"]).toEqual({ x: 90, y: 100 });
    expect(s().selection.nodes).toEqual(new Set(["a-3"]));
    expect(s().duplicate()).toBe(true);
    expect(s().doc.org.agents).toHaveLength(7);
    expect(s().cut()).toBe(true);
    expect(s().doc.org.agents).toHaveLength(6);
    expect(s().paste()).toBe(true);
    expect(s().doc.org.agents).toHaveLength(7);
    s().setSelection([]);
    expect(s().copy()).toBe(false);
  });

  it("loads templates as one undoable step and asks the canvas to fit", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    const template = BUILT_IN_TEMPLATES[1];
    if (template === undefined) throw new Error("no template");
    s().load("Loaded template", template.document);
    expect(s().doc).toBe(template.document);
    expect(s().fitRequest).toBe(1);
    s().undo();
    expect(s().doc.org.id).toBe("acme");
  });

  it("renames ids and keeps the selection and role panel on them", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().setSelection(["a"]);
    s().renameAgentId("a", "al", "k1");
    s().renameAgentId("al", "alpha", "k1");
    expect(s().selection.nodes).toEqual(new Set(["alpha"]));
    expect(s().history.past).toHaveLength(1); // typing merges
    s().setActiveRole("dev");
    s().renameRoleId("dev", "developer", "k2");
    expect(s().activeRoleId).toBe("developer");
    expect(s().doc.org.agents.filter((a) => a.role === "developer")).toHaveLength(3);
  });

  it("focus requests select the agent; auto-layout clears pinned positions", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().focusAgent("b");
    expect(s().selection.nodes).toEqual(new Set(["b"]));
    expect(s().focusRequest).toEqual({ id: "b", seq: 1 });
    s().focusRole("dev");
    expect(s().panel).toBe("roles");
    s().autoLayout();
    expect(s().doc.layout.positions).toEqual({});
    expect(s().fitRequest).toBe(1);
  });

  it("auto-layout only says 'Arranged' when it changed something", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().autoLayout(); // clears any pinned positions (or finds none)
    s().autoLayout(); // nothing left to clear
    expect(s().announcement.text).toBe("Already arranged automatically; fitted to view");
    expect(s().fitRequest).toBe(2); // it still fits the view
  });

  it("saves and deletes user templates, and reports blocked storage", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    expect(s().saveTemplate("Mine", "")).toBe(true);
    expect(JSON.parse(window.localStorage.getItem(TEMPLATES_KEY) ?? "[]")).toHaveLength(1);
    expect(createBuilderStore().getState().userTemplates.map((t) => t.name)).toEqual(["Mine"]);
    const id = s().userTemplates[0]?.id ?? "";
    expect(s().deleteTemplate(id)).toBe(true);
    expect(s().userTemplates).toEqual([]);

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(s().saveTemplate("Again", "")).toBe(false);
    expect(s().announcement.text).toMatch(/Couldn't save the template/);
  });

  it("edge ids map to the reporting agent", () => {
    expect(reportOfEdge(edgeId("dev-1"))).toBe("dev-1");
  });
});

describe("connectLayout", () => {
  it("runs layout on structural changes only and drops stale results", async () => {
    type Positions = Record<string, { x: number; y: number }>;
    const resolvers: ((p: Positions) => void)[] = [];
    const layout = vi.fn(
      () =>
        new Promise<Positions>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const engine: LayoutEngine = { layout, dispose: () => undefined };
    const store = createBuilderStore(sampleDoc());
    const stop = connectLayout(store, engine);
    expect(store.getState().layoutStatus).toBe("running");
    store.getState().edit("Rename", (d) => updateAgent(d, "a", { name: "Z" })); // not structural
    expect(layout).toHaveBeenCalledTimes(1);
    store.getState().addAgentFromRole(dev);
    expect(layout).toHaveBeenCalledTimes(2);

    resolvers[0]?.({ old: { x: 0, y: 0 } }); // stale
    await Promise.resolve();
    expect(store.getState().computed).toEqual({});
    resolvers[1]?.({ boss: { x: 1, y: 2 } });
    await Promise.resolve();
    await Promise.resolve();
    expect(store.getState().computed).toEqual({ boss: { x: 1, y: 2 } });
    expect(store.getState().layoutStatus).toBe("idle");
    stop();
  });

  it("reports a failed layout", async () => {
    const engine: LayoutEngine = { layout: () => Promise.reject(new Error("boom")), dispose: () => undefined };
    const store = createBuilderStore(sampleDoc());
    connectLayout(store, engine);
    await vi.waitFor(() => {
      expect(store.getState().layoutStatus).toBe("error");
    });
    expect(store.getState().announcement.text).toMatch(/layout failed/);
  });
});

describe("connectDraftAutosave", () => {
  it("saves the document shortly after edits and on stop", () => {
    vi.useFakeTimers();
    try {
      const store = createBuilderStore(sampleDoc());
      const stop = connectDraftAutosave(store, 100);
      store.getState().edit("Rename", (d) => updateAgent(d, "a", { name: "Saved" }));
      expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull();
      vi.advanceTimersByTime(100);
      expect(window.localStorage.getItem(DRAFT_KEY)).toContain("Saved");
      store.getState().edit("Rename", (d) => updateAgent(d, "a", { name: "Final" }));
      stop();
      expect(window.localStorage.getItem(DRAFT_KEY)).toContain("Final");
    } finally {
      vi.useRealTimers();
    }
  });
});
