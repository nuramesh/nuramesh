import { describe, expect, it } from "vitest";

import { approval } from "./map";
import type { RawApproval } from "./raw";

// #1458 (SA-16, SA-17): the server says whether the signed-in caller is the run's originator, also when the run
// was started with an API token they issued. Display only; anything but a plain `true` is "not known" = false.
const pending: RawApproval = {
  id: "ap-1",
  run_id: "run-1-abcd",
  task_id: null,
  agent_id: "omar",
  action: { kind: "merge", target: "staging", amount: null },
  asked_because: "merges ask a person",
  status: "pending",
  requested_at: "2026-10-24T10:00:00Z",
  decided_by: null,
  decided_at: null,
  decision_reason_hash: null,
  decision_reason: null,
};

type Terms = NonNullable<RawApproval["caller_terms"]>;
const terms = (extra: Record<string, unknown>): Terms => ({ mode: "solo", fresh_proof_needed: false, proofs: [], ...extra });

describe("callerIsOriginator", () => {
  it("is true only when the server's caller_terms says so", () => {
    expect(approval({ ...pending, caller_terms: terms({ caller_is_originator: true }) }, null).callerIsOriginator).toBe(true);
    expect(approval({ ...pending, caller_terms: terms({ caller_is_originator: false }) }, null).callerIsOriginator).toBe(false);
    expect(approval({ ...pending, caller_terms: terms({ mode: "refused", caller_is_originator: true }) }, null).callerIsOriginator).toBe(true);
  });

  it("fails closed: no terms, an older server without the key, or a value that is not a boolean is false", () => {
    expect(approval(pending, null).callerIsOriginator).toBe(false);
    expect(approval({ ...pending, caller_terms: null }, null).callerIsOriginator).toBe(false);
    expect(approval({ ...pending, caller_terms: terms({}) }, null).callerIsOriginator).toBe(false);
    for (const odd of ["true", 1, {}, null]) {
      expect(approval({ ...pending, caller_terms: terms({ caller_is_originator: odd }) }, null).callerIsOriginator).toBe(false);
    }
  });

  it("does not change the solo terms", () => {
    const solo = { ...pending, action_digest: "d".repeat(64), caller_terms: terms({ caller_is_originator: true }) };
    expect(approval(solo, null).solo).toMatchObject({ actionDigest: "d".repeat(64), freshProofNeeded: false });
    expect(approval({ ...pending, caller_terms: terms({ mode: "peer", caller_is_originator: true }) }, null).solo).toBeNull();
  });
});
