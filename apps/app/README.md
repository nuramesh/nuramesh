# NuraMesh app

The NuraMesh web app: React + TypeScript + Vite single-page app (graph builder, later run views).

```bash
cd apps/app
corepack pnpm@10.34.7 install --frozen-lockfile    # pnpm version also pinned in package.json
corepack pnpm@10.34.7 dev                           # http://localhost:5173 (sample data; real data: see the app guide)
corepack pnpm@10.34.7 lint && corepack pnpm@10.34.7 typecheck && corepack pnpm@10.34.7 test
corepack pnpm@10.34.7 exec playwright install chromium   # once
corepack pnpm@10.34.7 e2e                           # builds, serves with `vite preview`, runs Playwright
corepack pnpm@10.34.7 audit --audit-level low
```

- **Org model:** `src/model/org.ts` mirrors `nuramesh.core.org` (same snake_case JSON). `src/model/validate.ts`
  implements the same rules as `validate_org`; `src/model/validate.test.ts` runs every fixture in
  the internal specification (shared with pytest); `src/model/org.test.ts` runs the internal specification
  (field-level parsing, which must agree with pydantic `OrgFields`).
- **Graph builder (`src/builder/`, PLAN A.1–A.5):** document `{schema_version: 1, org, layout: {positions}}`;
  `org` is exactly the Python `OrgGraph` JSON, `layout.positions` holds only positions the user chose.
  Pure edits in `ops.ts`, undo/redo in `history.ts` (every edit goes through `store.edit`), copy/paste in
  `clipboard.ts`, ELK layout mapping in `layout.ts` (ELK runs in its own Web Worker, `layout-engine.ts`),
  live validation in `issues.ts` (`validateOrg` + field rules), React Flow mapping with per-node caching in
  `flow.ts`. User templates and the current draft live in `localStorage` (guarded, re-validated on read).
- **Speed checks:** `e2e/perf.spec.ts` (Playwright project `perf`, runs after the others): 500 rendered
  agents, pan/zoom/drag for 3 s, median frame ≤ 16.7 ms and p95 ≤ 25 ms (`PERF_MEDIAN_MAX_MS` /
  `PERF_P95_MAX_MS` override for slow runners); add/edit visible in < 100 ms.
- **Theme:** `public/theme-init.js` sets the `dark` class before first paint (no flash; external file so a
  strict CSP needs no inline script); `src/lib/theme.ts` owns the preference afterwards. Same storage key.
- **shadcn/ui:** only the components we use live in `src/components/ui/`. They are generated from the
  shadcn registry (style `new-york-v4`, see `components.json`) with:

  ```bash
  corepack pnpm@10.34.7 dlx shadcn@4.21.5 view button dropdown-menu input label textarea tabs dialog native-select \
    | node scripts/shadcn-sync.mjs
  ```

  The script rewrites the registry's `cn` import (from the 0.x `cn` package) to our `@/lib/utils`
  (clsx + tailwind-merge) and registry-internal paths to `@/components/ui/`, and drops `"use client"`. Add the component's npm dependencies by hand,
  pinned exactly.
- **Audit exceptions:** `pnpm-workspace.yaml` (`auditConfig.ignoreGhsas`) **and** `audit-exceptions.json`
  (reason, review date) (`scripts/check-audit-exceptions.mjs`).
- **Supply chain:** `minimumReleaseAge: 4320` (3 days) in `pnpm-workspace.yaml`; every version is pinned exactly.
