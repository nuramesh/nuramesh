import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_PREFS, firstRunKey, readPrefs, writePrefs } from "./storage";

afterEach(() => {
  localStorage.clear();
});

describe("first-run preference (FR-11)", () => {
  it("C41 a length-safe key: colliding pairs get different keys", () => {
    expect(firstRunKey("a:b", "c")).not.toBe(firstRunKey("a", "b:c"));
    expect(firstRunKey("o", "u")).toBe(
      `nuramesh.firstRun.v1:${JSON.stringify(["o", "u"])}`,
    );
    writePrefs("a:b", "c", { dismissed: true, invite_skipped: false });
    expect(readPrefs("a", "b:c")).toEqual(DEFAULT_PREFS);
    expect(readPrefs("a:b", "c").dismissed).toBe(true);
  });
  it("round-trips", () => {
    writePrefs("o", "u", { dismissed: true, invite_skipped: true });
    expect(readPrefs("o", "u")).toEqual({
      dismissed: true,
      invite_skipped: true,
    });
  });
  it.each([
    '{"v":2,"dismissed":true,"invite_skipped":false}',
    "not json",
    '{"v":1,"dismissed":"yes","invite_skipped":false}',
    '{"v":1,"dismissed":true,"invite_skipped":false,"extra":1}',
    JSON.stringify({
      v: 1,
      dismissed: true,
      invite_skipped: false,
      pad: "x".repeat(300),
    }),
    "null",
    "[]",
  ])("C24 %s reads as the default and the next write replaces it", (raw) => {
    localStorage.setItem(firstRunKey("o", "u"), raw);
    expect(readPrefs("o", "u")).toEqual(DEFAULT_PREFS);
    writePrefs("o", "u", { dismissed: true, invite_skipped: false });
    expect(readPrefs("o", "u").dismissed).toBe(true);
  });
  it("C25 blocked storage never throws and the value lasts until reload", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readPrefs("o", "u")).toEqual(DEFAULT_PREFS);
    expect(() => {
      writePrefs("o", "u", { dismissed: true, invite_skipped: false });
    }).not.toThrow();
  });
  it("C26 and C27 another person or organization has its own value", () => {
    writePrefs("o", "A", { dismissed: true, invite_skipped: false });
    expect(readPrefs("o", "B")).toEqual(DEFAULT_PREFS);
    expect(readPrefs("o2", "A")).toEqual(DEFAULT_PREFS);
    expect(readPrefs("o", "A").dismissed).toBe(true);
  });
  it("C28 the stored value holds no personal data and is at most 256 bytes", () => {
    writePrefs("org-secret-id", "user-secret-id", {
      dismissed: true,
      invite_skipped: true,
    });
    const raw =
      localStorage.getItem(firstRunKey("org-secret-id", "user-secret-id")) ??
      "";
    expect(raw).toBe('{"v":1,"dismissed":true,"invite_skipped":true}');
    expect(raw).not.toContain("secret");
    expect(raw.length).toBeLessThanOrEqual(256);
  });
});
