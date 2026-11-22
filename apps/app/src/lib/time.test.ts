import { describe, expect, it } from "vitest";

import { formatLocalWithRelative, formatRelative } from "./time";

const NOW = new Date("2026-10-27T07:37:00Z");
const OPTS = { locale: "en-AU", timeZone: "UTC" };

describe("formatLocalWithRelative", () => {
  it("shows the local date and time, then the relative time", () => {
    expect(formatLocalWithRelative("2026-10-27T07:34:00Z", NOW, OPTS)).toBe("27 Oct 2026, 7:34 am · 3 mins ago");
  });

  it("handles the future and large gaps", () => {
    expect(formatRelative("2099-01-01T00:00:00Z", NOW, "en-AU")).toMatch(/^in \d+ yrs?\.?$/);
    expect(formatRelative("2026-10-26T07:37:00Z", NOW, "en-AU")).toBe("yesterday");
    expect(formatRelative("2026-10-27T07:37:00Z", NOW, "en-AU")).toBe("now");
  });

  it("returns the raw text for an unreadable date", () => {
    expect(formatLocalWithRelative("not-a-date", NOW, OPTS)).toBe("not-a-date");
  });
});
