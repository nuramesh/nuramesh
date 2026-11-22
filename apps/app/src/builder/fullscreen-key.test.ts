import { describe, expect, it } from "vitest";

import { isFullscreenKey } from "./shortcuts";

const plain = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };

describe("isFullscreenKey", () => {
  it("accepts a plain f or F outside a text field", () => {
    const target = document.createElement("div");
    expect(isFullscreenKey({ key: "f", target, ...plain })).toBe(true);
    expect(isFullscreenKey({ key: "F", target, ...plain })).toBe(true);
  });

  it("ignores other keys and any held modifier", () => {
    const target = document.createElement("div");
    expect(isFullscreenKey({ key: "g", target, ...plain })).toBe(false);
    for (const held of ["ctrlKey", "metaKey", "altKey", "shiftKey"] as const) {
      expect(isFullscreenKey({ key: "f", target, ...plain, [held]: true })).toBe(false);
    }
  });

  it("ignores typing in inputs, textareas, selects and contenteditable", () => {
    for (const tag of ["input", "textarea", "select"]) {
      expect(isFullscreenKey({ key: "f", target: document.createElement(tag), ...plain })).toBe(false);
    }
    const editable = document.createElement("div");
    editable.contentEditable = "true";
    Object.defineProperty(editable, "isContentEditable", { value: true });
    expect(isFullscreenKey({ key: "f", target: editable, ...plain })).toBe(false);
  });
});
