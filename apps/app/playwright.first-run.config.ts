import { defineConfig, devices } from "@playwright/test";

/**
 * First-run end-to-end checks against a REAL `nuramesh serve --auth` on a fresh empty store (no
 * owner yet): the setup token, the app's setup page, the first sign-in. `pnpm e2e:first-run`.
 * `setup-url.spec.ts` starts its own servers (8792, 8793) on a pseudo-terminal, because `serve`
 * prints the one-time setup link to a terminal only.
 * Needs `uv sync --all-extras` at the repository root. One API server (8791) and one Vite dev
 * server (5191, forwarding /v1 to it). Specs run in order and share that one server's store, which
 * is removed by the global teardown.
 */
export default defineConfig({
  testDir: "./e2e-first-run",
  globalTeardown: "./e2e-first-run/teardown.ts",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  timeout: 60_000,
  use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, trace: "retain-on-failure" },
  webServer: [
    {
      command: "sh scripts/first-run-server.sh test-results/first-run 8791",
      url: "http://127.0.0.1:8791/v1/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "corepack pnpm@10.34.7 exec vite --port 5191 --strictPort",
      env: { NURAMESH_API_URL: "http://127.0.0.1:8791", VITE_API_SOURCE: "server" },
      url: "http://127.0.0.1:5191/",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
