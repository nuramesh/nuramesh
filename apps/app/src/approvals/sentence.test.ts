import { describe, expect, it } from "vitest";

import { usd } from "@/api/money";
import type { Approval } from "@/api/types";

import { approvalSentence, consequenceFor, costLine, escapeInert, MAX_TARGET, sentenceOf } from "./sentence";

const cp = (...points: number[]): string => String.fromCodePoint(...points);

describe("AE-5 plain sentence", () => {
  const cases: [string, string | null, string][] = [
    ["merge", "main", "dev wants to merge main."],
    ["external_message", "ann@example.test", "dev wants to send a message outside your organization: ann@example.test."],
    ["run_command", "make test", "dev wants to run a command: make test."],
    ["network", "api.example.test", "dev wants to connect to api.example.test."],
    ["use_tool", "search", "dev wants to use the tool search."],
    ["write_file", "a.txt", "dev wants to change the file a.txt."],
    ["call_model", "fake-model", "dev wants to call the model fake-model."],
    ["delegate", "tester", "dev wants to hand work to tester."],
    ["teleport", "moon", "dev wants to do something called teleport: moon."],
  ];
  for (const [kind, target, expected] of cases) {
    it(kind, () => {
      expect(approvalSentence({ agent: "dev", kind, target, amount: null })).toBe(expected);
    });
  }
  it("spend names the amount, or says money", () => {
    expect(approvalSentence({ agent: "dev", kind: "spend", target: null, amount: usd("0.50") })).toBe("dev wants to spend $0.50.");
    expect(approvalSentence({ agent: "dev", kind: "spend", target: null, amount: null })).toBe("dev wants to spend money.");
  });
  it("a missing target is said in words, never null or blank", () => {
    for (const target of [null, ""]) {
      const s = approvalSentence({ agent: "dev", kind: "merge", target, amount: null });
      expect(s).toBe("dev wants to merge (no target given).");
      expect(s).not.toContain("null");
    }
  });
  it("an unknown kind with a bidi control is shown escaped; an agent id with a newline too", () => {
    expect(approvalSentence({ agent: "dev", kind: `te${cp(0x202e)}x`, target: "t", amount: null })).toBe("dev wants to do something called te\\u202Ex: t.");
    expect(approvalSentence({ agent: `a${cp(10)}b`, kind: "merge", target: "main", amount: null })).toBe("a\\u000Ab wants to merge main.");
  });
});

describe("AE-6 inert text", () => {
  it("escapes C0, DEL, C1 and bidi controls as visible text", () => {
    expect(escapeInert(`a${cp(0x202e)}b`)).toBe("a\\u202Eb");
    expect(escapeInert(`x${cp(10)}y${cp(9)}z`)).toBe("x\\u000Ay\\u0009z");
    expect(escapeInert(cp(0x85))).toBe("\\u0085");
    expect(escapeInert(cp(0x7f))).toBe("\\u007F");
    expect(escapeInert(cp(0x2066))).toBe("\\u2066");
    for (const p of [0x200e, 0x200f, 0x061c, 0x202a, 0x2069, 0, 0x1f, 0x9f]) {
      expect(escapeInert(cp(p))).toMatch(/^\\u[0-9A-F]{4}$/);
    }
  });
  it("leaves ordinary text, backslashes and markup untouched (React renders them as text)", () => {
    expect(escapeInert("<img src=x onerror=alert(1)> [x](http://evil) \\u202E")).toBe("<img src=x onerror=alert(1)> [x](http://evil) \\u202E");
  });
  it("cuts a target over 200 characters with ... in the sentence", () => {
    const long = "a".repeat(300);
    const s = approvalSentence({ agent: "dev", kind: "merge", target: long, amount: null });
    expect(s).toBe(`dev wants to merge ${"a".repeat(MAX_TARGET)}....`);
  });
});

describe("AE-7 consequences and AE-8 cost line", () => {
  it("every kind has a fixed line, and an unknown one a generic line", () => {
    for (const kind of ["merge", "spend", "external_message", "run_command", "network", "use_tool", "write_file", "call_model", "delegate"]) {
      expect(consequenceFor(kind)).not.toBe(consequenceFor("teleport"));
    }
    expect(consequenceFor("teleport")).toBe("the agent does this one thing");
  });
  it("run_command consequence does not claim a sandbox or isolation", () => {
    expect(consequenceFor("run_command")).not.toMatch(/sandbox|isolat|contain|safe/i);
    expect(consequenceFor("run_command")).toBe("the command runs");
  });
  it("spend shows the exact amount; others the run spend; unknown is said", () => {
    expect(costLine({ kind: "spend", amount: usd("0.50") }, null)).toBe("$0.50");
    expect(costLine({ kind: "spend", amount: usd("0.10") }, null)).toBe("$0.10");
    expect(costLine({ kind: "merge", amount: null }, { spent: usd("1.20"), limit: usd("5") })).toBe("Spent so far $1.20 of $5.00");
    expect(costLine({ kind: "merge", amount: null }, { spent: usd("1.20"), limit: null })).toBe("No direct cost");
    expect(costLine({ kind: "merge", amount: null }, null)).toBe("Cost not available");
    expect(costLine({ kind: "spend", amount: null }, null)).toBe("Cost not available");
  });
});

describe("sentenceOf", () => {
  const base = { id: "x", title: `T${cp(0x202e)}`, requester: "dev", runId: "r", runLabel: "#1", requestedAt: "", state: "pending", effect: "", rule: "", runSpend: null, evidence: [], decision: null } as Approval;
  it("builds from the action, and falls back to the escaped title", () => {
    expect(sentenceOf({ ...base, action: { kind: "merge", target: "main", amount: null } })).toBe("dev wants to merge main.");
    expect(sentenceOf(base)).toBe("T\\u202E");
  });
});
