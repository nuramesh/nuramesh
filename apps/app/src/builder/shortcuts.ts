import { useEffect } from "react";

import type { BuilderStore } from "./store";

/** True when a key press belongs to a text field, menu or dialog rather than the canvas. */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return true;
  return target.closest('[role="dialog"], [role="menu"], [role="listbox"]') !== null;
}

/** True for a plain F (no Ctrl, Cmd, Alt or Shift) pressed outside a text field, menu or dialog. */
export function isFullscreenKey(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey" | "target">): boolean {
  if (event.key.toLowerCase() !== "f") return false;
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return false;
  return !isEditableTarget(event.target);
}

/**
 * Handles a builder shortcut. Returns true if it was one (the caller prevents the default).
 * Mod = Cmd on macOS, Ctrl elsewhere; Ctrl+Y also redoes. Delete/Backspace are handled by
 * React Flow (onDelete), so they work only while the canvas has focus. A read-only view (the
 * phone) handles none of them, so the keyboard cannot change the document.
 */
export function handleShortcut(store: BuilderStore, event: KeyboardEvent, readOnly = false): boolean {
  if (readOnly) return false;
  if (isEditableTarget(event.target)) return false;
  if (!(event.metaKey || event.ctrlKey) || event.altKey) return false;
  const s = store.getState();
  switch (event.key.toLowerCase()) {
    case "z":
      if (event.shiftKey) s.redo();
      else s.undo();
      return true;
    case "y":
      s.redo();
      return true;
    case "a":
      s.selectAll();
      return true;
    case "c":
      // Let the browser copy selected text (for example from the issues panel).
      if ((window.getSelection()?.toString() ?? "") !== "") return false;
      return s.copy();
    case "x":
      return s.cut();
    case "v":
      return s.paste();
    case "d":
      s.duplicate();
      return true; // never open the browser's bookmark dialog from the builder
    default:
      return false;
  }
}

/** Installs the builder's keyboard shortcuts on the document while mounted. */
export function useBuilderShortcuts(store: BuilderStore, readOnly = false): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (handleShortcut(store, event, readOnly)) event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [store, readOnly]);
}
