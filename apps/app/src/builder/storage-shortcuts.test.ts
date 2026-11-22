import { describe, expect, it, vi } from "vitest";

import { handleShortcut, isEditableTarget } from "./shortcuts";
import { DRAFT_KEY, loadDraft, loadUserTemplates, saveDraft, TEMPLATES_KEY } from "./storage";
import { createBuilderStore } from "./store";
import { sampleDoc } from "./test-data";

describe("storage", () => {
  it("skips corrupted or invalid saved templates", () => {
    window.localStorage.setItem(
      TEMPLATES_KEY,
      JSON.stringify([
        { id: "ok", name: "OK", document: sampleDoc() },
        { id: "bad", name: "Bad", document: { schema_version: 9 } },
        "junk",
        { name: "no id", document: sampleDoc() },
      ]),
    );
    expect(loadUserTemplates().map((t) => t.id)).toEqual(["ok"]);
    window.localStorage.setItem(TEMPLATES_KEY, "{not json");
    expect(loadUserTemplates()).toEqual([]);
  });

  it("round-trips the draft and survives blocked storage", () => {
    expect(saveDraft(sampleDoc())).toBe(true);
    expect(loadDraft()).toEqual(sampleDoc());
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify({ schema_version: 1 }));
    expect(loadDraft()).toBeNull();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadDraft()).toBeNull();
    expect(loadUserTemplates()).toEqual([]);
  });
});

describe("shortcuts", () => {
  const press = (key: string, init: KeyboardEventInit = {}, target: EventTarget = document.body) => {
    const event = new KeyboardEvent("keydown", { key, ...init });
    Object.defineProperty(event, "target", { value: target });
    return event;
  };

  it("ignores typing in fields, menus and dialogs", () => {
    const input = document.createElement("input");
    expect(isEditableTarget(input)).toBe(true);
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const button = document.createElement("button");
    dialog.append(button);
    expect(isEditableTarget(button)).toBe(true);
    expect(isEditableTarget(document.body)).toBe(false);
    const store = createBuilderStore(sampleDoc());
    expect(handleShortcut(store, press("z", { ctrlKey: true }, input))).toBe(false);
  });

  it("maps keys to undo, redo, select all, copy, paste, cut and duplicate", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().setSelection(["b"]);
    expect(handleShortcut(store, press("d", { metaKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(5);
    expect(handleShortcut(store, press("z", { ctrlKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(4);
    expect(handleShortcut(store, press("Z", { ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(5);
    s().undo();
    expect(handleShortcut(store, press("y", { ctrlKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(5);
    expect(handleShortcut(store, press("a", { metaKey: true }))).toBe(true);
    expect(s().selection.nodes.size).toBe(5);
    s().setSelection(["b"]);
    expect(handleShortcut(store, press("c", { metaKey: true }))).toBe(true);
    expect(handleShortcut(store, press("v", { metaKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(6);
    expect(handleShortcut(store, press("x", { metaKey: true }))).toBe(true);
    expect(s().doc.org.agents).toHaveLength(5);
    expect(handleShortcut(store, press("q", { metaKey: true }))).toBe(false);
    expect(handleShortcut(store, press("z"))).toBe(false); // no modifier
  });
});

describe("shortcuts in read-only (phone) mode", () => {
  const press = (key: string, init: KeyboardEventInit = {}) => new KeyboardEvent("keydown", { key, ...init });

  it("does nothing for edit shortcuts when read-only", () => {
    const store = createBuilderStore(sampleDoc());
    const s = () => store.getState();
    s().setSelection(["b"]);
    expect(handleShortcut(store, press("d", { metaKey: true }), true)).toBe(false);
    expect(s().doc.org.agents).toHaveLength(4);
    // An edit made elsewhere is not undone by Cmd+Z either.
    s().addPersonNode();
    const before = s().doc;
    const selected = s().selection;
    for (const key of ["z", "y", "x", "v", "a", "c", "d"]) {
      expect(handleShortcut(store, press(key, { ctrlKey: true }), true), key).toBe(false);
      expect(handleShortcut(store, press(key, { ctrlKey: true, shiftKey: true }), true), key).toBe(false);
    }
    expect(s().doc).toBe(before);
    expect(s().selection).toBe(selected);
  });
});
