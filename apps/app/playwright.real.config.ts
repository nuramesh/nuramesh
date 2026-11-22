import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end checks against REAL `nuramesh` API servers (not the fixture client): the recorded first
 * real run, tampered copies of it, window budgets, approvals and a live run. `pnpm e2e:real`.
 * Needs `uv sync --all-extras` at the repository root (it starts `scripts/app/serve_scenarios.py`).
 *
 * Six API servers (ports 8781 to 8786) and one Vite dev server per API server (5181 to 5186; the
 * dev server forwards /v1 to its API, which is how the app talks to the server). The sixth has an
 * empty models page, a FAKE OpenAI-compatible model server and a driver on port 8790
 * (`scripts/app/connect_scenario.py`): no real model provider is ever called. Nothing is reused:
 * stop any server still running on those ports first. The scenario server starts the fifth API last,
 * so waiting for its health check means all six are up; its stores are under test-results/real-e2e-stores and are removed by the global teardown.
 */
const API_BASE = 8781;
const APP_BASE = 5181;

export default defineConfig({
  testDir: "./e2e-real",
  globalTeardown: "./e2e-real/teardown.ts",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  timeout: 60_000,
  use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, trace: "retain-on-failure" },
  webServer: [
    {
      command: "uv run python scripts/app/serve_scenarios.py --dir apps/app/test-results/real-e2e-stores",
      cwd: "../..",
      url: `http://127.0.0.1:${String(API_BASE + 4)}/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    ...[0, 1, 2, 3, 4, 5].map((i) => ({
      command: `corepack pnpm@10.34.7 exec vite --port ${String(APP_BASE + i)} --strictPort`,
      env: { NURAMESH_API_URL: `http://127.0.0.1:${String(API_BASE + i)}`, VITE_API_SOURCE: "server" },
      url: `http://127.0.0.1:${String(APP_BASE + i)}/`,
      reuseExistingServer: false,
      timeout: 60_000,
    })),
  ],
});
