import { describe, expect, it } from "vitest";

import { eventCategory, eventText, ledgerCategory, runEvent } from "./map";
import type { RawEvent } from "./raw";

const raw = (type: string, payload: Record<string, unknown> = {}, over: Partial<RawEvent> = {}): RawEvent => ({
  stream: "run:r1",
  seq: 13,
  type,
  actor: "system",
  at: "2026-10-25T10:00:13Z",
  run_id: "r1",
  task_id: "r1-t1",
  payload,
  cost: null,
  cost_basis: null,
  hash: "1".repeat(64),
  prev_hash: "0".repeat(64),
  ...over,
});

describe("G1 eventText for the types that showed their raw name", () => {
  it("M1 model.blocked", () => {
    expect(eventText(raw("model.blocked", { agent: "writer", reason: "budget" }))).toBe("writer's model call was blocked: budget");
  });

  it("M2 six more types each get a sentence that is not the raw type", () => {
    for (const [type, payload] of [
      ["tool.called", { agent: "writer", tool: "search" }],
      ["eval.scored", { agent: "writer", eval: "quality" }],
      ["gate.started", { gate: "g1", attempt: 1 }],
      ["task.stop_requested", {}],
      ["content.captured", { kind: "task_output" }],
      ["breaker.tripped", { rule: "spend-cap" }],
    ] as const) {
      const text = eventText(raw(type, payload));
      expect(text).not.toBe(type);
      expect(text.length).toBeGreaterThan(10);
    }
    expect(eventText(raw("tool.called", { agent: "writer", tool: "search" }))).toContain("writer");
    expect(eventText(raw("breaker.tripped", { rule: "spend-cap" }))).toContain("spend-cap");
  });

  it("M2a adapter tool calls (S4 N5): the outcome of tool.called and the frozen tool set", () => {
    const called = { agent: "dev", tool: "deploy", adapter: "langgraph", call_id: "c-1", outcome: "ok" };
    expect(eventText(raw("tool.called", called))).toBe("dev used the tool deploy");
    expect(eventText(raw("tool.called", { ...called, outcome: "error" }))).toBe("dev used the tool deploy (the tool failed)");
    expect(eventText(raw("tool.called", { ...called, outcome: "cancelled" }))).toBe("dev used the tool deploy (cancelled)");
    const frozen = { kind: "tool_set_frozen", agent: "dev", adapter: "langgraph", tool_count: 4, attempt: 1 };
    expect(eventText(raw("decision.recorded", frozen, { actor: "agent:dev" }))).toBe("Tool set frozen for dev: 4 tools (langgraph)");
  });

  it("M3 an unknown type keeps its raw name", () => {
    expect(eventText(raw("x.unknown"))).toBe("x.unknown");
  });

  it("M11 each kind of decision has its own sentence, and an incident never carries its note", () => {
    const kinds: [Record<string, unknown>, string][] = [
      [{ kind: "human_decision_refused", attempt: 1, why: "input_expired" }, "input expired"],
      [{ kind: "engine_incident", task_status: "needs_human", human_required: true, note: "NOTE-TEXT-FIXTURE do not show" }, "Engine incident"],
      [{ kind: "step_restarted", step: "verify", attempt: 1, epoch: 2 }, "verify"],
      [{ kind: "scripted_approval", gate: "g1", approver: "human:u3", human_review: false }, "human:u3"],
      [{}, "Decision recorded"],
      [{ kind: "human_wait_warning", attempt: 1, days_left: 7 }, "7 days"],
      [{ kind: "human_wait_expired", attempt: 1, days_left: 0 }, "ended"],
      [{ kind: "dependency_waiting", waiting_on: "t-wait", attempt: 1 }, "t-wait"],
    ];
    const texts = kinds.map(([payload, word]) => {
      const text = eventText(raw("decision.recorded", payload));
      expect(text).toContain(word);
      return text;
    });
    expect(new Set(texts).size).toBe(8);
    expect(texts[1]).not.toContain("NOTE-TEXT");
  });
});

describe("G2 categories", () => {
  it("M4 model.blocked is spend in both functions", () => {
    expect(eventCategory("model.blocked")).toBe("spend");
    expect(ledgerCategory("model.blocked")).toBe("spend");
  });

  it("M5 stop requests, breakers and org changes are tasks on the timeline and decisions in the ledger", () => {
    for (const type of ["run.stop_requested", "task.stop_requested", "breaker.tripped", "org.halted"]) {
      expect(eventCategory(type)).toBe("tasks");
      expect(ledgerCategory(type)).toBe("decisions");
    }
  });
});

describe("G3 a timeline event links to its entry and task", () => {
  it("M6 carries the entry id (as entryHref takes it) and the task id", () => {
    const e = runEvent(raw("gate.completed", { gate: "g", outcome: "passed", attempt: 1 }));
    expect(e.entry).toBe("run:r1@13");
    expect(e.taskId).toBe("r1-t1");
    expect(runEvent(raw("run.ended", {}, { task_id: null })).taskId).toBeNull();
  });
});
