import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chooseSource, createApiClient } from "./index";

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllEnvs();
});

describe("a production build never shows sample data from a stored choice (#888)", () => {
  it("ignores a stored 'fixtures' when not dev", () => {
    expect(chooseSource(undefined, false, "fixtures")).toBe("server");
    expect(chooseSource("", false, "fixtures")).toBe("server");
  });

  it("still honours the stored choice under dev", () => {
    expect(chooseSource(undefined, true, "fixtures")).toBe("fixtures");
    expect(chooseSource(undefined, true, "server")).toBe("server");
  });

  it("clears a stale stored 'fixtures' key in a production build", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_API_SOURCE", "");
    localStorage.setItem("nuramesh.source", "fixtures");
    const client = await createApiClient();
    expect(client.source).toBe("server");
    expect(localStorage.getItem("nuramesh.source")).toBeNull();
  });
});
