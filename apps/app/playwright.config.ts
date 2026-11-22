import { defineConfig, devices } from "@playwright/test";

// E2E_PORT lets a second checkout run its tests beside another preview server.
const port = Number(process.env.E2E_PORT ?? 4173);
/** The spec's reference width (SPEC-app-v1 §6); phone-size tests set their own viewport. */
const DESKTOP = { width: 1440, height: 900 };

/** End-to-end tests run against the production build (`vite preview`), never the dev server. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  // CI runners have 2 vCPUs, where Playwright's default (half the cores) is one worker, so
  // `fullyParallel` bought nothing. Two workers share the one read-only `vite preview` server; every
  // test uses its own browser context, so nothing is shared between specs (checked for ND138).
  workers: process.env.CI ? 2 : undefined,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // A broken spec set must fail in minutes, not run to the 30-minute job limit and jam the merge queue.
  maxFailures: process.env.CI ? 10 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${String(port)}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: DESKTOP }, testIgnore: /perf\.spec\.ts/ },
    // Speed checks run alone, after everything else, so other tests don't steal CPU from them.
    {
      name: "perf",
      use: { ...devices["Desktop Chrome"], viewport: DESKTOP },
      testMatch: /perf\.spec\.ts/,
      dependencies: ["chromium"],
      fullyParallel: false,
    },
  ],
  webServer: {
    // CI builds in an earlier step; locally pnpm runs through corepack (not installed globally).
    command: process.env.CI
      ? `pnpm preview --port ${String(port)}`
      : `VITE_API_SOURCE=fixtures corepack pnpm@10.34.7 build && corepack pnpm@10.34.7 preview --port ${String(port)}`,
    url: `http://127.0.0.1:${String(port)}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
