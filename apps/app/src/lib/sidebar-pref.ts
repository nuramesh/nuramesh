/**
 * The left sidebar's collapsed preference, remembered per viewer in localStorage. Storage can be
 * missing or throw (privacy modes), so every access is guarded and the app works without it.
 * With no stored choice the sidebar follows the width: expanded from 1440 px, icons only below.
 */
export const SIDEBAR_KEY = "nuramesh-sidebar";
/** The width from which the sidebar is expanded by default. */
export const WIDE_QUERY = "(min-width: 1440px)";

/** Fired on `window` when the sidebar changes width, so the builder canvas can fit again. */
export const LAYOUT_EVENT = "nuramesh:layout";

export type SidebarPreference = "expanded" | "collapsed";

/** The stored choice, or null when there is none, it is invalid or storage is unavailable. */
export function readSidebarPreference(): SidebarPreference | null {
  try {
    const raw = window.localStorage.getItem(SIDEBAR_KEY);
    return raw === "expanded" || raw === "collapsed" ? raw : null;
  } catch {
    return null;
  }
}

/** Stores the choice; returns false when storage is unavailable. */
export function writeSidebarPreference(value: SidebarPreference): boolean {
  try {
    window.localStorage.setItem(SIDEBAR_KEY, value);
    return true;
  } catch {
    return false;
  }
}

/** True when the sidebar should be icons only: the stored choice wins, otherwise the width. */
export function resolveCollapsed(stored: SidebarPreference | null, wide: boolean): boolean {
  return stored === null ? !wide : stored === "collapsed";
}
