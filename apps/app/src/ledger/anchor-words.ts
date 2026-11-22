import type { AnchorState, ChainStatus } from "@/api/types";
import type { ChipTone } from "@/components/ui/chip";

/** What the user is told about a chain check, in words. "Verified" is only ever said for `verified: true`. */
export interface ChainSummary {
  /** Short headline, the chip's text. */
  headline: string;
  tone: ChipTone;
  /** One sentence of explanation. */
  detail: string;
  /** True only for a chain that is intact AND anchored with signatures that check out. */
  verified: boolean;
}

export const ANCHOR_WORDS: Record<AnchorState, string> = {
  anchored: "anchored",
  unanchored: "not anchored yet",
  problems: "anchor problems",
  not_configured: "anchors not checked",
  unreadable: "anchors unreadable",
};

/** The server's hash-chain problem codes in plain words (the first word is the code as sent). */
const CHAIN_WORDS: Record<string, string> = {
  bad_hash: "an entry doesn't match its own hash (it was changed)",
  broken_link: "an entry doesn't link to the one before it (entries were changed, removed or inserted)",
  wrong_seq: "entries are missing or out of order",
  wrong_stream: "an entry belongs to another stream",
  time_went_backwards: "an entry is dated before the one before it",
};

/** "stream_shorter: the ledger is shorter..." for display. */
export function describeChainProblem(problem: string): string {
  const code = problem.split(" ")[0] ?? problem;
  return `${problem}: ${CHAIN_WORDS[code] ?? "the server reported a problem"}`;
}

/** Anchor problems that mean the ledger no longer matches what was signed (as opposed to a bad signature or file). */
const MISMATCH = new Set(["head_mismatch", "stream_shorter"]);

/**
 * Plain wording for a chain check. An intact chain that has no signed anchors is "not anchored
 * yet", never "verified": the chain check alone only finds accidental damage, anchors are what
 * prove history wasn't rewritten (docs/api.md, Verify).
 */
export function summarizeChain(status: ChainStatus): ChainSummary {
  if (status.anchorState === undefined) {
    // A source without anchors (the sample data): the old two-state wording.
    return status.verified && status.gaps === 0
      ? { headline: "Chain verified", tone: "success", detail: "no edits", verified: true }
      : { headline: "Chain broken", tone: "danger", detail: status.verified ? "entries are missing" : "an entry doesn't match its hash", verified: false };
  }
  if (status.chainOk === false) {
    const first = status.streams?.find((s) => s.chainProblem !== null)?.chainProblem;
    return { headline: "Chain broken", tone: "danger", detail: first === undefined || first === null ? "an entry doesn't match its hash or entries are missing" : describeChainProblem(first), verified: false };
  }
  switch (status.anchorState) {
    case "anchored":
      return status.verified
        ? { headline: "Verified", tone: "success", detail: "the chain is intact and signed anchors check out", verified: true }
        : { headline: "Not verified", tone: "warning", detail: "the server did not confirm the anchors", verified: false };
    case "unanchored":
      return { headline: "Chain intact, not anchored yet", tone: "warning", detail: "no signed anchor covers these entries yet, so history is not proven", verified: false };
    case "problems": {
      const codes = (status.streams ?? []).flatMap((s) => s.anchorProblems.map((p) => p.code));
      const mismatch = codes.some((c) => MISMATCH.has(c));
      return {
        headline: mismatch ? "Anchor mismatch" : "Anchor problems",
        tone: "danger",
        detail: mismatch
          ? "the chain is intact, but it no longer matches what was signed and anchored"
          : "a signed anchor could not be checked",
        verified: false,
      };
    }
    case "unreadable":
      return { headline: "Anchors couldn't be read", tone: "danger", detail: "the server could not check the anchor repository", verified: false };
    case "not_configured":
      return { headline: "Chain intact, anchors not checked", tone: "neutral", detail: "this server was started without --anchors and --pubkey", verified: false };
  }
}

