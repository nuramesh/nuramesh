import { describe, expect, it } from "vitest";

import { eventText, ledgerEntry, runTask } from "./map";
import type { RawEvent, RawRunDetail } from "./raw";

const WITHHELD = "withheld (a person's text is not shown)";

function event(over: Partial<RawEvent> & { payload: Record<string, unknown> }): RawEvent {
  return {
    stream: "run:r5",
    seq: 6,
    type: "task.transitioned",
    actor: "system",
    at: "2026-10-24T10:00:00Z",
    run_id: "r5",
    task_id: "t5",
    cost: null,
    cost_basis: null,
    hash: "h".repeat(8),
    prev_hash: "p".repeat(8),
    ...over,
  };
}

const move = (actor: string, reason: string): RawEvent =>
  event({ actor, payload: { from: "verifying", to: "in_progress", reason } });

const field = (e: RawEvent, label: string): string | undefined => ledgerEntry(e).fields.find((f) => f.label === label)?.value;

describe("M10 eventText hides a reason unless the actor is exactly system", () => {
  it("leaves out the reason part for every other actor and keeps it for system", () => {
    for (const actor of ["human:u1", "service:cli", "agent:x", "system:model-governance"]) {
      const text = eventText(move(actor, "secret words"));
      expect(text).not.toContain("secret words");
      expect(text).not.toContain("(");
    }
    expect(eventText(move("system", "gate failed: g-ver"))).toContain("(gate failed: g-ver)");
  });
});

describe("M13 incident notes", () => {
  const incident = (actor: string, payload: Record<string, unknown>): RawEvent =>
    event({ type: "decision.recorded", actor, payload: { kind: "engine_incident", ...payload } });

  it("never puts the note in a sentence, a summary or a field", () => {
    const [fresh, oldSystem, oldHuman] = [
      incident("system", { note_present: true }),
      incident("system", { note: "NOTE-TEXT-FIXTURE do not show" }),
      incident("human:u1", { note: "NOTE-TEXT-FIXTURE do not show" }),
    ] as [RawEvent, RawEvent, RawEvent];
    for (const e of [fresh, oldSystem, oldHuman]) {
      const entry = ledgerEntry(e);
      expect(eventText(e)).not.toContain("NOTE-TEXT");
      expect(entry.summary).not.toContain("NOTE-TEXT");
      expect(JSON.stringify(entry.fields)).not.toContain("NOTE-TEXT");
    }
    expect(field(fresh, "note_present")).toBe("yes");
    expect(field(incident("system", { note_present: false }), "note_present")).toBe("no");
    expect(field(oldSystem, "note")).toBe(WITHHELD);
    expect(field(oldHuman, "note")).toBe(WITHHELD);
  });
});

describe("M14 hidden fields", () => {
  it("shows withheld for a redacted key, nested too, and never the word null", () => {
    const top = event({ actor: "human:u1", payload: { from: "a", reason: null } });
    const withList = { ...top, redacted: ["reason"] };
    expect(field(withList, "reason")).toBe(WITHHELD);
    const nested = { ...event({ actor: "human:u1", payload: { details: { reason: null } } }), redacted: ["reason"] } as RawEvent;
    expect(field(nested, "details.reason")).toBe(WITHHELD);
    expect(ledgerEntry(nested).fields.map((f) => f.value)).not.toContain("null");
    expect(ledgerEntry(withList).fields.map((f) => f.value)).not.toContain("null");
  });
});

describe("M15 run page note", () => {
  const task = (note: string | null): RawRunDetail["tasks"][number] =>
    ({ id: "t", title: "T", assignee: null, status: "assigned", attempt: 1, max_attempts: 2, note, attempts: [], latest_gates: [], spend: { total: { amount: "0", currency: "USD" }, calls: 0 } });

  it("maps null and empty to no note and keeps a real one", () => {
    expect(runTask(task(null), "run:r").note).toBe("");
    expect(runTask(task(""), "run:r").note).toBe("");
    expect(runTask(task("x"), "run:r").note).toBe("x");
  });
});

describe("M16 stale server", () => {
  it("hides an unredacted reason of a person but shows one of system", () => {
    const person = event({ actor: "human:u1", payload: { reason: "x" } });
    expect(field(person, "reason")).toBe(WITHHELD);
    expect(field(event({ actor: "system", payload: { reason: "x" } }), "reason")).toBe("x");
  });
});
