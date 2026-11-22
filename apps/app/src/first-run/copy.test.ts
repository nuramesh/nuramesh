import { describe, expect, it } from "vitest";

import { COPY, STEP_LABELS } from "./copy";

describe("copy rules (FR-3, FR-15, C44)", () => {
  it("labels are the four from FR-3, in order", () => {
    expect(STEP_LABELS).toEqual({
      owner: "Create the owner account",
      run: "Start the demo run",
      approve: "Decide the first approval",
      invite: "Invite a teammate",
    });
  });
  it("C44 no cost claim and no safety claim in the strings", () => {
    const all = JSON.stringify([COPY, STEP_LABELS]).toLowerCase();
    expect(all).not.toMatch(/\bfree\b/);
    expect(all).not.toContain("fake model");
    expect(all).not.toMatch(/\bsafe\b/);
  });
  it("carries the required sentences", () => {
    expect(COPY.couldNotCheck).toBe("Could not check. Try again.");
    expect(COPY.noApprovalAsked).toBe("No approval has been asked yet");
    expect(COPY.nothingToSetUp).toBe("Nothing left to set up");
    expect(COPY.complete).toBe("You are set up");
    expect(COPY.soloApproval).toContain("recorded as a solo approval");
    expect(COPY.invitesNotYet).toBe("Invites arrive with the next release");
  });
});
