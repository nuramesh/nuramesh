import { describe, expect, it, vi } from "vitest";

import themeInitSource from "../../public/theme-init.js?raw";
import { readPreference, resolveTheme, THEME_STORAGE_KEY, useTheme, watchSystemTheme } from "./theme";

function mockSystemDark(dark: boolean): { fire: (dark: boolean) => void } {
  const listeners = new Set<() => void>();
  const state = { dark };
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        get matches() {
          return state.dark;
        },
        media: query,
        addEventListener: (_: string, fn: () => void) => listeners.add(fn),
        removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
      }) as unknown as MediaQueryList,
  );
  return {
    fire(next) {
      state.dark = next;
      for (const fn of listeners) fn();
    },
  };
}

function runInitScript(): void {
  // Same code the browser runs from <head> before first paint (our own file, test-only eval).
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  const run = new Function(themeInitSource) as () => void;
  run();
}

describe("resolveTheme", () => {
  it("follows the system only for the system preference", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });
});

describe("readPreference", () => {
  it("ignores unknown values and blocked storage", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "purple");
    expect(readPreference()).toBe("system");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readPreference()).toBe("system");
  });
});

describe("theme-init.js (no flash before React loads)", () => {
  it.each([
    ["dark", false, true],
    ["light", true, false],
    ["system", true, true],
    [null, false, false],
  ] as const)("stored %s, system dark %s → dark class %s", (stored, systemDark, expected) => {
    mockSystemDark(systemDark);
    if (stored !== null) window.localStorage.setItem(THEME_STORAGE_KEY, stored);
    runInitScript();
    expect(document.documentElement.classList.contains("dark")).toBe(expected);
  });

  it("survives blocked storage", () => {
    mockSystemDark(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    runInitScript();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});

describe("useTheme", () => {
  it("applies, saves and follows system changes", () => {
    const media = mockSystemDark(false);
    const stop = watchSystemTheme();

    useTheme.getState().setPreference("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    media.fire(false); // ignored: the user chose dark explicitly
    expect(useTheme.getState().resolved).toBe("dark");

    useTheme.getState().setPreference("system");
    expect(useTheme.getState().resolved).toBe("light");
    media.fire(true);
    expect(useTheme.getState().resolved).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    stop();
    media.fire(false);
    expect(useTheme.getState().resolved).toBe("dark");
  });

  it("still applies the choice when storage is blocked", () => {
    mockSystemDark(false);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    useTheme.getState().setPreference("dark");
    expect(useTheme.getState().preference).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
