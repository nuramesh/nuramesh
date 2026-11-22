import { describe, expect, it } from "vitest";

import { emptyHistory, HISTORY_LIMIT, MERGE_WINDOW_MS, record, redo, undo } from "./history";
import { updateOrg } from "./ops";
import { sampleDoc } from "./test-data";

describe("history", () => {
  it("undoes and redoes in order; a new edit clears redo", () => {
    const d0 = sampleDoc();
    const d1 = updateOrg(d0, { name: "One" });
    const d2 = updateOrg(d1, { name: "Two" });
    let h = record(emptyHistory(), d0, d1, "one");
    h = record(h, d1, d2, "two");
    const u1 = undo(h);
    expect(u1?.doc).toBe(d1);
    expect(u1?.label).toBe("two");
    const u2 = u1 && undo(u1.history);
    expect(u2?.doc).toBe(d0);
    expect(u2 && undo(u2.history)).toBeNull();
    const r1 = u2 && redo(u2.history);
    expect(r1?.doc).toBe(d1);
    const branched = r1 && record(r1.history, d1, updateOrg(d1, { name: "Other" }), "other");
    expect(branched?.future).toEqual([]);
    expect(branched && redo(branched)).toBeNull();
  });

  it("records nothing for a no-op edit", () => {
    const d0 = sampleDoc();
    const h = emptyHistory();
    expect(record(h, d0, d0, "noop")).toBe(h);
  });

  it("merges rapid edits with the same key into one step", () => {
    const d0 = sampleDoc();
    const d1 = updateOrg(d0, { name: "A" });
    const d2 = updateOrg(d1, { name: "Ab" });
    const d3 = updateOrg(d2, { name: "Abc" });
    let h = record(emptyHistory(), d0, d1, "type", "org:name", 1_000);
    h = record(h, d1, d2, "type", "org:name", 1_500);
    h = record(h, d2, d3, "type", "org:name", 1_500 + MERGE_WINDOW_MS); // too late: new step
    expect(h.past).toHaveLength(2);
    expect(h.past[0]?.before).toBe(d0);
    expect(h.past[0]?.after).toBe(d2);
    // A different key never merges.
    h = record(h, d3, updateOrg(d3, { id: "x" }), "id", "org:id", 3_000);
    expect(h.past).toHaveLength(3);
  });

  it("keeps at most HISTORY_LIMIT steps", () => {
    let doc = sampleDoc();
    let h = emptyHistory();
    for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) {
      const next = updateOrg(doc, { name: `n${String(i)}` });
      h = record(h, doc, next, "edit");
      doc = next;
    }
    expect(h.past).toHaveLength(HISTORY_LIMIT);
  });
});
