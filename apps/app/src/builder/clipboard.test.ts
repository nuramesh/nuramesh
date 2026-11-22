import { describe, expect, it } from "vitest";

import { copyFragment, PASTE_OFFSET, pasteFragment } from "./clipboard";
import { blankDocument } from "./document";
import { sampleDoc } from "./test-data";

describe("copy and paste", () => {
  it("copies selected agents with their roles and pinned positions", () => {
    const fragment = copyFragment(sampleDoc(), new Set(["a", "a1"]));
    expect(fragment?.agents.map((a) => a.id)).toEqual(["a", "a1"]);
    expect(fragment?.roles.map((r) => r.id)).toEqual(["dev"]);
    expect(fragment?.positions).toEqual({ a: { x: 10, y: 20 } });
    expect(copyFragment(sampleDoc(), new Set(["nobody"]))).toBeNull();
  });

  it("remaps ids and internal lines; external managers are kept", () => {
    const doc = sampleDoc();
    const fragment = copyFragment(doc, new Set(["a", "a1"]));
    if (fragment === null) throw new Error("expected a fragment");
    const { doc: next, ids } = pasteFragment(doc, fragment);
    expect(ids).toEqual(["a-2", "a1-2"]);
    const pasted = next.org.agents.slice(-2);
    expect(pasted.map((a) => [a.id, a.reports_to, a.name])).toEqual([
      ["a-2", "boss", "A (copy)"], // boss wasn't copied but exists: keeps reporting to it
      ["a1-2", "a-2", "A1 (copy)"], // internal line remapped
    ]);
    expect(next.layout.positions["a-2"]).toEqual({ x: 10 + PASTE_OFFSET, y: 20 + PASTE_OFFSET });
    expect(next.org.roles).toHaveLength(2);
  });

  it("pastes into another document, bringing missing roles and dropping unknown managers", () => {
    const fragment = copyFragment(sampleDoc(), new Set(["a1"]));
    if (fragment === null) throw new Error("expected a fragment");
    const { doc, ids } = pasteFragment(blankDocument(), fragment, 0);
    expect(ids).toEqual(["a1"]);
    expect(doc.org.agents[0]?.reports_to).toBeNull();
    expect(doc.org.roles.map((r) => r.id)).toEqual(["dev"]);
  });

  it("keeps copied names within the 120-character limit", () => {
    const doc = sampleDoc();
    const long = { ...doc, org: { ...doc.org, agents: doc.org.agents.map((a) => ({ ...a, name: "n".repeat(120) })) } };
    const fragment = copyFragment(long, new Set(["b"]));
    if (fragment === null) throw new Error("expected a fragment");
    const name = pasteFragment(long, fragment).doc.org.agents.at(-1)?.name ?? "";
    expect(name).toHaveLength(120);
    expect(name.endsWith(" (copy)")).toBe(true);
  });
});
