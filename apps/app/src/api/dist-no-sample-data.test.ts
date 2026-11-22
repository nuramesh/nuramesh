import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { build } from "vite";
import { afterAll, describe, expect, it } from "vitest";

// Sample-data strings that exist only in the fixture client and its fixtures (#888).
const MARKERS = ["u_sample_other", "Sample admin", "Sample viewer"];

const out = mkdtempSync(join(tmpdir(), "nuramesh-dist-"));
afterAll(() => {
  rmSync(out, { recursive: true, force: true });
});

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? files(join(dir, e.name)) : [join(dir, e.name)]));
}

describe("a build pinned to the server ships no sample data (#888)", () => {
  it("emits no fixture chunk and none of the sample-data strings", async () => {
    process.env.VITE_API_SOURCE = "server";
    try {
      await build({ logLevel: "silent", build: { outDir: out, emptyOutDir: true, sourcemap: false, write: true } });
    } finally {
      delete process.env.VITE_API_SOURCE;
    }
    const all = files(out);
    expect(all.filter((f) => /fixture/i.test(f))).toEqual([]);
    for (const f of all.filter((p) => /\.(js|css|html)$/.test(p))) {
      const text = readFileSync(f, "utf8");
      for (const m of MARKERS) expect(text, `${f} contains "${m}"`).not.toContain(m);
    }
  }, 180_000);
});
