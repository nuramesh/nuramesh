// Writes shadcn/ui registry components into src/components/ui (only the ones we use).
// The registry imports `cn` from the 0.x `cn` package; we keep our own `@/lib/utils` helper
// (clsx + tailwind-merge) instead, so the import is rewritten, as are registry-internal paths
// (@/registry/new-york-v4/ui/*). "use client" is dropped (no RSC here).
// Usage: corepack pnpm@10.34.7 dlx shadcn@4.21.5 view <names...> | node scripts/shadcn-sync.mjs
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

const items = JSON.parse(readFileSync(0, "utf8"));
const target = join(import.meta.dirname, "..", "src", "components", "ui");
mkdirSync(target, { recursive: true });

for (const item of items) {
  for (const file of item.files) {
    if (file.type !== "registry:ui") throw new Error(`${item.name}: unexpected file type ${file.type}`);
    const content = file.content
      .replace(/^"use client"\n\n/, "")
      .replace(/from "cn"/g, 'from "@/lib/utils"')
      .replace(/@\/registry\/new-york-v4\/ui\//g, "@/components/ui/");
    const out = join(target, basename(file.path));
    writeFileSync(out, content);
    console.log(`wrote ${out} (npm deps: ${(item.dependencies ?? []).join(", ") || "none"})`);
  }
}
