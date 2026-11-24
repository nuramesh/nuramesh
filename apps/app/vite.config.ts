import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import type { Plugin } from "vite";

import { CSP_POLICY, injectCspMeta } from "./csp";

const src = fileURLToPath(new URL("./src", import.meta.url));
const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
// The brand folder at the repository root (shared with apps/web):.
const brand = fileURLToPath(new URL("../../brand", import.meta.url));

// `nuramesh serve` answers only requests that name its own loopback address (no CORS, no
// cross-site calls), so the app talks to it through this same-origin forward: the browser asks the
// dev server for /v1/..., the dev server passes the request on with the Host and Origin untouched
// (changeOrigin stays false on purpose; WebSockets are forwarded too). `NURAMESH_API_URL` says where
// the server listens.
const apiTarget = process.env.NURAMESH_API_URL ?? "http://127.0.0.1:8765";
const forward = { "/v1": { target: apiTarget, ws: true, changeOrigin: false } };

// Build only: the dev server's HMR client and React refresh preamble are inline scripts a strict policy would break.
const contentSecurityPolicy: Plugin = {
  name: "nuramesh-csp-meta",
  apply: "build",
  transformIndexHtml: { order: "post", handler: injectCspMeta },
};

export default defineConfig({
  plugins: [react(), tailwindcss(), contentSecurityPolicy],
  resolve: {
    alias: { "@": src, "@brand": brand },
    // brand/Logo.tsx lives outside this package: resolve React from here, so there is one copy.
    dedupe: ["react", "react-dom"],
  },
  // Tests read shared fixtures (spec/), the design doc and the shared logo from the repository root.
  // Bound to loopback on purpose: with the forward in place, any other host would hand the server's
  // loopback-only API to the network (see the app guide on the docs site). Do not pass --host.
  server: { fs: { allow: [repoRoot] }, host: "127.0.0.1", proxy: forward },
  preview: { host: "127.0.0.1", proxy: forward, headers: { "Content-Security-Policy": CSP_POLICY } },
  build: {
    target: "es2023",
    // Maps are generated for error reporting but not referenced from the shipped bundle.
    sourcemap: "hidden",
    // ELK (compiled from Java) is ~1.4 MB: the worker script, plus a bundled copy used only where
    // workers don't exist. Neither is on the page's critical path.
    chunkSizeWarningLimit: 1_500,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    // The 5 s default per test (and 10 s per hook) is shorter than a heavy render takes on a loaded machine;
    // the testing-library wait limit (src/test/setup.ts) is kept below this so a missing element fails with its own message.
    testTimeout: 60_000,
    hookTimeout: 60_000,
    // The default (cores minus one) is a single worker on 2-vCPU CI runners but 31 on a 32-core host, where
    // the jsdom workers (about 1 GB each) hit the test-run guard's memory limit: so 2 under CI, else at most 4;
    // NURAMESH_VITEST_WORKERS overrides. Isolation stays on.
    maxWorkers: Number(process.env.NURAMESH_VITEST_WORKERS) || (process.env.CI ? 2 : 4),
    // CSS is not processed in unit tests, except that the contrast test reads the real tokens as text
    css: { include: [/src\/index\.css/] },
  },
});
