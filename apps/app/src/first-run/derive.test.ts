import { describe, expect, it } from "vitest";

import type { AuthState } from "@/auth/types";

import {
  checklistVisible,
  deriveSteps,
  doneCount,
  isComplete,
  parseApprovals,
  parseInvites,
  parseRuns,
  type Answers,
} from "./derive";

const owner = (
  role: "owner" | "approver" | "viewer" | "admin" | "builder" = "owner",
): AuthState =>
  ({
    kind: "signed-in",
    user: {
      id: "u1",
      orgId: "o1",
      role,
      email: "a@example.invalid",
      displayName: "A",
    },
  }) as AuthState;

const unknownAll: Answers = {
  runs: { ok: false },
  approvals: { ok: false },
  invites: { ok: false },
};
const none: Answers = {
  runs: { ok: true, count: 0 },
  approvals: { ok: true, statuses: [], hasMore: false },
  invites: { ok: true, count: 0 },
};
const prefs = { dismissed: false, invite_skipped: false };
const status = (s: ReturnType<typeof deriveSteps>, id: string) =>
  s.find((x) => x.id === id)?.status;

describe("who sees the checklist (FR-1, C3 to C6, C46)", () => {
  it("shows to a signed-in owner on the server source", () => {
    expect(checklistVisible(owner(), "server")).toBe(true);
  });
  it.each(["approver", "viewer", "admin", "builder"] as const)(
    "hides from %s",
    (role) => {
      expect(checklistVisible(owner(role), "server")).toBe(false);
    },
  );
  it("hides for the sample source and every non-signed-in state", () => {
    expect(checklistVisible(owner(), "sample")).toBe(false);
    for (const s of [
      { kind: "open" },
      { kind: "checking" },
      { kind: "signed-out" },
    ] as AuthState[]) {
      expect(checklistVisible(s, "server")).toBe(false);
    }
    expect(
      checklistVisible({ kind: "unavailable", message: "x" }, "server"),
    ).toBe(false);
  });
});

describe("deriveSteps (FR-3, FR-4)", () => {
  it("C1 lists four steps in order, owner done and the rest open", () => {
    const steps = deriveSteps(none, prefs);
    expect(steps.map((s) => s.id)).toEqual([
      "owner",
      "run",
      "approve",
      "invite",
    ]);
    expect(steps.map((s) => s.status)).toEqual([
      "done",
      "open",
      "open",
      "open",
    ]);
    expect(doneCount(steps)).toBe(1);
  });
  it("C7 and C8 a run marks run done", () => {
    expect(
      status(
        deriveSteps({ ...none, runs: { ok: true, count: 1 } }, prefs),
        "run",
      ),
    ).toBe("done");
    expect(status(deriveSteps(none, prefs), "run")).toBe("open");
  });
  it("C9 only pending rows and more pages: not done", () => {
    const a: Answers = {
      ...none,
      approvals: { ok: true, statuses: ["pending"], hasMore: true },
    };
    expect(status(deriveSteps(a, prefs), "approve")).toBe("open");
  });
  it("C10 and C11 any decided status counts", () => {
    for (const s of ["denied", "approved", "consumed"]) {
      const a: Answers = {
        ...none,
        approvals: { ok: true, statuses: ["pending", s], hasMore: false },
      };
      expect(status(deriveSteps(a, prefs), "approve")).toBe("done");
    }
  });
  it("C12 only pending rows: not done", () => {
    const a: Answers = {
      ...none,
      approvals: { ok: true, statuses: ["pending"], hasMore: false },
    };
    expect(status(deriveSteps(a, prefs), "approve")).toBe("open");
  });
  it("C13 and C14 any invite counts", () => {
    expect(
      status(
        deriveSteps({ ...none, invites: { ok: true, count: 1 } }, prefs),
        "invite",
      ),
    ).toBe("done");
    expect(status(deriveSteps(none, prefs), "invite")).toBe("open");
  });
  it("C15 an unknown read is unknown, not done, others unaffected", () => {
    const steps = deriveSteps(
      { ...none, runs: { ok: false }, invites: { ok: true, count: 1 } },
      prefs,
    );
    expect(status(steps, "run")).toBe("unknown");
    expect(status(steps, "invite")).toBe("done");
    expect(status(steps, "approve")).toBe("open");
    expect(doneCount(steps)).toBe(2);
  });
  it("C17 state follows the server (a vanished proof reverts)", () => {
    const a = deriveSteps({ ...none, runs: { ok: true, count: 1 } }, prefs);
    const b = deriveSteps(none, prefs);
    expect([status(a, "run"), status(b, "run")]).toEqual(["done", "open"]);
  });
  it("C42 run done with no approvals says no approval has been asked", () => {
    const steps = deriveSteps({ ...none, runs: { ok: true, count: 1 } }, prefs);
    expect(steps.find((s) => s.id === "approve")?.note).toBe(
      "no-approval-asked",
    );
  });
});

describe("answer parsing (C16)", () => {
  it("accepts the documented shapes", () => {
    expect(parseRuns({ runs: [{ id: "r" }], truncated: false })).toEqual({
      ok: true,
      count: 1,
    });
    expect(
      parseApprovals({ items: [{ status: "denied" }], next_cursor: null }),
    ).toEqual({
      ok: true,
      statuses: ["denied"],
      hasMore: false,
    });
    expect(
      parseApprovals({ items: [{ status: "pending" }], next_cursor: "c" }),
    ).toMatchObject({ hasMore: true });
    expect(parseInvites({ items: [{}, {}] })).toEqual({ ok: true, count: 2 });
  });
  it.each([{}, null, "x", 3, { runs: "no" }, { runs: {} }])(
    "runs body %j is unknown",
    (body) => {
      expect(parseRuns(body)).toEqual({ ok: false });
    },
  );
  it("unexpected approval and invite bodies are unknown", () => {
    for (const body of [
      {},
      null,
      { items: "x" },
      { items: [{ status: 3 }] },
      { items: [null] },
    ]) {
      expect(parseApprovals(body)).toEqual({ ok: false });
    }
    for (const body of [{}, null, { items: 4 }])
      expect(parseInvites(body)).toEqual({ ok: false });
  });
});

describe("real server shapes (invite_routes.py old_key=\"invites\", paginate)", () => {
  it("invites: the old-shape envelope `invites` counts", () => {
    expect(
      parseInvites({ invites: [{ id: "i1", email: "a@example.invalid" }], next_cursor: null }),
    ).toEqual({ ok: true, count: 1 });
    expect(parseInvites({ invites: [], next_cursor: null })).toEqual({ ok: true, count: 0 });
  });
  it("invites: a page with a cursor still counts what it holds", () => {
    expect(parseInvites({ invites: [{ id: "i1" }], next_cursor: "abc" })).toEqual({
      ok: true,
      count: 1,
    });
  });
  it("invites: `invites` that is not an array is unknown", () => {
    expect(parseInvites({ invites: "x", next_cursor: null })).toEqual({ ok: false });
  });
  it("runs: the old-shape envelope with truncated and next_cursor", () => {
    expect(parseRuns({ runs: [], truncated: false, next_cursor: null })).toEqual({
      ok: true,
      count: 0,
    });
  });
  it("approvals: paged envelope `items` (old_key None, limit sent)", () => {
    expect(
      parseApprovals({ items: [{ id: "a1", status: "approved" }], next_cursor: null }),
    ).toEqual({ ok: true, statuses: ["approved"], hasMore: false });
  });
});

describe("completion (FR-5, FR-7)", () => {
  const all: Answers = {
    runs: { ok: true, count: 1 },
    approvals: { ok: true, statuses: ["approved"], hasMore: false },
    invites: { ok: true, count: 1 },
  };
  it("C18 all four proofs is complete", () => {
    expect(isComplete(deriveSteps(all, prefs))).toBe(true);
  });
  it("C19 invite skipped completes it; C20 done wins over skipped", () => {
    const a: Answers = { ...all, invites: { ok: true, count: 0 } };
    expect(isComplete(deriveSteps(a, prefs))).toBe(false);
    const skipped = deriveSteps(a, { ...prefs, invite_skipped: true });
    expect(status(skipped, "invite")).toBe("skipped");
    expect(isComplete(skipped)).toBe(true);
    expect(
      status(deriveSteps(all, { ...prefs, invite_skipped: true }), "invite"),
    ).toBe("done");
  });
  it("C21 run and approve are never skipped", () => {
    const steps = deriveSteps(none, { ...prefs, invite_skipped: true });
    expect(status(steps, "run")).toBe("open");
    expect(status(steps, "approve")).toBe("open");
    expect(steps.map((s) => s.canSkip)).toEqual([false, false, false, true]);
  });
  it("unknown steps never complete the list", () => {
    expect(
      isComplete(deriveSteps(unknownAll, { ...prefs, invite_skipped: true })),
    ).toBe(false);
  });
});
