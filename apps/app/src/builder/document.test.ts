import { describe, expect, it } from "vitest";

import { blankDocument, formatErrors, parseDocument, readDocumentFile, serializeDocument } from "./document";
import { sampleDoc } from "./test-data";
import { BUILT_IN_TEMPLATES } from "./templates";

describe("parseDocument", () => {
  it("round-trips a document through JSON", () => {
    const doc = sampleDoc();
    expect(parseDocument(JSON.parse(serializeDocument(doc)))).toEqual({ ok: true, value: doc });
    expect(parseDocument(blankDocument())).toEqual({ ok: true, value: blankDocument() });
  });

  it("accepts a bare organization (Python config shape)", () => {
    const result = parseDocument(sampleDoc().org);
    expect(result.ok && result.value.layout.positions).toEqual({});
  });

  it("rejects a hostile '__proto__' position instead of changing a prototype", () => {
    const hostile = JSON.parse(
      `{"schema_version":1,"org":${JSON.stringify(sampleDoc().org)},"layout":{"positions":{"__proto__":{"x":1,"y":2}}}}`,
    ) as unknown;
    const result = parseDocument(hostile);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.errors.some((e) => e.path === "layout.positions.__proto__")).toBe(true);
    }
    expect(({} as Record<string, unknown>).x).toBeUndefined(); // nothing global changed
  });

  it("explains every problem", () => {
    const result = parseDocument({
      schema_version: 3,
      org: { id: "x", name: "X", roles: [], agents: [{ id: "a", name: "A", role: "r" }, { id: "a", name: "B", role: "r" }] },
      layout: { positions: { a: { x: "1", y: 0 }, ghost: { x: 0, y: 0 } }, zoom: 2 },
      extra: true,
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(formatErrors(result.errors)).toEqual([
      "extra: Unknown field",
      "schema_version: Unsupported schema version 3; expected 1",
      "layout.zoom: Unknown field",
      "layout.positions.a: Must be {x, y} with finite numbers",
      "org.agents: Agent id 'a' is used more than once",
      "layout.positions.ghost: No agent or person has this id",
    ]);
    expect(parseDocument([])).toEqual({ ok: false, errors: [{ path: "(file)", message: "Must be a JSON object" }] });
    const missing = parseDocument({ org: sampleDoc().org, layout: { positions: {} } });
    expect(!missing.ok && formatErrors(missing.errors)).toEqual(["schema_version: Missing schema_version; expected 1"]);
  });

  it("built-in templates are valid documents", () => {
    for (const template of BUILT_IN_TEMPLATES) {
      expect(parseDocument(JSON.parse(serializeDocument(template.document))).ok).toBe(true);
    }
  });
});

describe("readDocumentFile", () => {
  it("reads valid files and reports bad JSON and oversize files", async () => {
    const ok = await readDocumentFile(new Blob([serializeDocument(sampleDoc())]));
    expect(ok.ok).toBe(true);
    const bad = await readDocumentFile(new Blob(["{nope"]));
    expect(!bad.ok && bad.errors[0]).toMatch(/^Not valid JSON/);
    const huge = await readDocumentFile({ size: 11 * 1024 * 1024 } as Blob);
    expect(!huge.ok && huge.errors[0]).toMatch(/limit is 10 MB/);
  });
});
