import { afterEach, describe, expect, it, vi } from "vitest";

import {
  readSidebarPreference,
  resolveCollapsed,
  SIDEBAR_KEY,
  writeSidebarPreference,
} from "./sidebar-pref";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("sidebar preference", () => {
  it("reads nothing when empty", () => {
    expect(readSidebarPreference()).toBeNull();
  });

  it("round-trips both values", () => {
    expect(writeSidebarPreference("collapsed")).toBe(true);
    expect(readSidebarPreference()).toBe("collapsed");
    writeSidebarPreference("expanded");
    expect(readSidebarPreference()).toBe("expanded");
  });

  it("ignores invalid stored values", () => {
    for (const bad of ["", "true", "COLLAPSED", "{}", "null"]) {
      window.localStorage.setItem(SIDEBAR_KEY, bad);
      expect(readSidebarPreference()).toBeNull();
    }
  });

  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(readSidebarPreference()).toBeNull();
    expect(writeSidebarPreference("collapsed")).toBe(false);
  });

  it("follows the width without a stored choice and the choice otherwise", () => {
    expect(resolveCollapsed(null, true)).toBe(false);
    expect(resolveCollapsed(null, false)).toBe(true);
    expect(resolveCollapsed("expanded", false)).toBe(false);
    expect(resolveCollapsed("collapsed", true)).toBe(true);
  });
});
