import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chooseSource } from "@/api";
import { crossOriginNote, normalizeServerUrl, saveServerUrl, saveSource, storedServerUrl, storedSource } from "@/api/settings";
import { createFixtureClient } from "@/api/fixture-client";
import { renderAt } from "@/test/server-like";

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("settings storage", () => {
  it("keeps only the source and the address, and cleans the address", () => {
    expect(normalizeServerUrl(" http://127.0.0.1:8000/ ")).toBe("http://127.0.0.1:8000");
    expect(normalizeServerUrl("http://localhost:8000/v1/health?x=1")).toBe("http://localhost:8000");
    expect(normalizeServerUrl("ftp://x")).toBe("");
    expect(normalizeServerUrl("not a url")).toBe("");
    expect(normalizeServerUrl("")).toBe("");
    saveSource("server");
    saveServerUrl("http://127.0.0.1:8000/");
    expect(storedSource()).toBe("server");
    expect(storedServerUrl()).toBe("http://127.0.0.1:8000");
    expect(Object.keys(localStorage).sort()).toEqual(["nuramesh.serverUrl", "nuramesh.source"]);
    saveSource(null);
    saveServerUrl("");
    expect(storedSource()).toBeNull();
    expect(storedServerUrl()).toBe("");
  });

  it("ignores a stored value it doesn't know", () => {
    localStorage.setItem("nuramesh.source", "cloud");
    expect(storedSource()).toBeNull();
  });

  it("an explicit build setting beats the stored choice, which beats the default", () => {
    expect(chooseSource("fixtures", false, "server")).toBe("fixtures");
    expect(chooseSource(undefined, false, "fixtures")).toBe("server");
    expect(chooseSource(undefined, true, "fixtures")).toBe("fixtures");
    expect(chooseSource(undefined, true, "server")).toBe("server");
    expect(chooseSource(undefined, true, null)).toBe("fixtures");
    expect(chooseSource(undefined, false, null)).toBe("server");
  });

  it("warns that another origin will usually be refused", () => {
    expect(crossOriginNote("", "http://127.0.0.1:5173")).toBeNull();
    expect(crossOriginNote("http://127.0.0.1:5173", "http://127.0.0.1:5173")).toBeNull();
    expect(crossOriginNote("http://127.0.0.1:8000", "http://127.0.0.1:5173")).toMatch(/refuses cross-site/);
  });
});

describe("Settings page", () => {
  it("explains where the data comes from and that no secret is stored", async () => {
    renderAt("/settings", createFixtureClient());
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.getByText(/No password, token or key is ever stored here/)).toBeInTheDocument();
    expect(screen.getByTestId("current-source")).toHaveTextContent("sample data");
    expect(screen.getByLabelText(/Sample data/)).toBeChecked();
  });

  it("names the one default port of `nuramesh serve` (8765)", async () => {
    renderAt("/settings", createFixtureClient());
    await screen.findByRole("heading", { level: 1, name: "Settings" });
    expect(document.getElementById("server-url-help")).toHaveTextContent("http://127.0.0.1:8765");
  });

  it("offers no sample data outside dev (#888)", async () => {
    vi.stubEnv("DEV", false);
    renderAt("/settings", createFixtureClient());
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Sample data/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/NuraMesh server/)).toBeInTheDocument();
    vi.unstubAllEnvs();
  });

  it("tests the connection against the address and reports the organization or the failure", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: string) =>
      input.startsWith("http://127.0.0.1:8000")
        ? Promise.resolve(new Response(JSON.stringify({ status: "ok", streams: 2, org: "demo", humans_enabled: true }), { status: 200 }))
        : Promise.reject(new TypeError("Failed to fetch")),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderAt("/settings", createFixtureClient());
    const input = await screen.findByLabelText(/Server address/);
    await user.type(input, "http://127.0.0.1:8000");
    expect(screen.getByText(/different address than this page/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => { expect(screen.getByTestId("connection-test")).toHaveTextContent("Connected to demo (127.0.0.1:8000 · people can act)."); });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://127.0.0.1:8000/v1/health");
    await user.clear(input);
    await user.type(input, "http://127.0.0.1:9999");
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => { expect(screen.getByTestId("connection-test")).toHaveTextContent(/Couldn't reach the NuraMesh server at http:\/\/127\.0\.0\.1:9999/); });
  });

  it("refuses a malformed address", async () => {
    const user = userEvent.setup();
    renderAt("/settings", createFixtureClient());
    await user.type(await screen.findByLabelText(/Server address/), "nonsense");
    expect(screen.getByRole("alert")).toHaveTextContent(/Use an address such as/);
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save and reload" })).toBeDisabled();
  });
});
