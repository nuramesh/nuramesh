import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { chainStatus } from "@/api/server/map";
import type { RawVerify } from "@/api/server/raw";
import type { ChainStatus } from "@/api/types";
import { body } from "@/test/contract";
import { renderAt, serverLike } from "@/test/server-like";

import { summarizeChain } from "./anchor-words";
import { entryHref, entryLabel } from "./format";

const NOW = new Date("2026-10-24T12:00:00Z");
const status = (file: "demo" | "tamper", name: string): ChainStatus => chainStatus(body<RawVerify>(file, name), NOW);

const CASES: [string, ChainStatus][] = [
  ["anchored", status("demo", "verify_all")],
  ["unanchored", status("tamper", "partial_verify_all")],
  ["not configured", status("demo", "noanchors_verify_all")],
  ["unreadable", status("tamper", "unreadable_verify_all")],
  ["chain broken", status("tamper", "chain_verify_all")],
  ["anchor mismatch (cut)", status("tamper", "truncated_verify_all")],
  ["anchor mismatch (rewritten)", status("tamper", "rewritten_verify_all")],
];

function chain(index: number): ChainStatus {
  const found = CASES[index];
  if (found === undefined) throw new Error(`no case ${String(index)}`);
  return found[1];
}

describe("the words for a chain check", () => {
  it("says 'Verified' only for the one real case that is anchored and verified", () => {
    expect(CASES.filter(([, s]) => summarizeChain(s).verified).map(([n]) => n)).toEqual(["anchored"]);
  });

  it("each real case has its own headline", () => {
    const heads = Object.fromEntries(CASES.map(([n, s]) => [n, summarizeChain(s).headline]));
    expect(heads).toEqual({
      anchored: "Verified",
      unanchored: "Chain intact, not anchored yet",
      "not configured": "Chain intact, anchors not checked",
      unreadable: "Anchors couldn't be read",
      "chain broken": "Chain broken",
      "anchor mismatch (cut)": "Anchor mismatch",
      "anchor mismatch (rewritten)": "Anchor mismatch",
    });
  });

  it("a broken chain says what the server reported, an anchor mismatch says the chain is intact", () => {
    expect(summarizeChain(chain(4)).detail).toMatch(/^bad_hash at entry 38: an entry doesn't match its own hash/);
    expect(summarizeChain(chain(5)).detail).toMatch(/chain is intact, but it no longer matches/);
  });

  it("the server's own 'not verified' flag wins even for anchored streams", () => {
    const flagged = chainStatus({ ...body<RawVerify>("demo", "verify_all"), verified: false }, NOW);
    expect(summarizeChain(flagged)).toMatchObject({ verified: false, headline: "Not verified" });
  });

  it("ledger entry ids become links and labels", () => {
    expect(entryLabel("run:r1@32")).toBe("32");
    expect(entryLabel("1043")).toBe("1043");
    expect(entryHref("run:r1@32")).toBe("/ledger/run%3Ar1%4032");
  });
});

describe("Ledger page with the real server's verification answers", () => {
  const client = (s: ChainStatus) =>
    serverLike({
      ledger: { list: () => Promise.resolve([]), chainStatus: () => Promise.resolve(s), verifyChain: () => Promise.resolve(s), get: () => Promise.reject(new Error("none")) },
      info: () => Promise.resolve({ name: "demo", detail: "x" }),
    });

  it("a verified, anchored ledger shows Verified and each stream as anchored", async () => {
    const user = userEvent.setup();
    renderAt("/ledger", client(chain(0)));
    const banner = await screen.findByTestId("chain-status");
    expect(within(banner).getByText("Verified")).toBeInTheDocument();
    expect(banner).toHaveTextContent("73 entries · no gaps");
    await user.click(within(banner).getByText(/2 streams checked/));
    expect(within(banner).getAllByTestId("stream-check").map((r) => r.getAttribute("data-anchor"))).toEqual(["anchored", "anchored"]);
  });

  it("an unanchored stream says 'not anchored yet' with the number of newer entries; never Verified", async () => {
    const user = userEvent.setup();
    renderAt("/ledger", client(chain(1)));
    const banner = await screen.findByTestId("chain-status");
    expect(within(banner).getByText("Chain intact, not anchored yet")).toBeInTheDocument();
    expect(within(banner).queryByText(/^Verified$/)).toBeNull();
    await user.click(within(banner).getByText(/2 streams checked/));
    expect(within(banner).getAllByTestId("stream-check")[1]).toHaveTextContent(/not anchored yet \(41 newer entries\)/);
  });

  it("an edited entry shows 'Chain broken' with the server's code and entry number; the page does not crash", async () => {
    const user = userEvent.setup();
    renderAt("/ledger", client(chain(4)));
    const banner = await screen.findByTestId("chain-status");
    expect(within(banner).getByText("Chain broken")).toBeInTheDocument();
    expect(banner).toHaveTextContent(/bad_hash at entry 38/);
    await user.click(within(banner).getByText(/2 streams checked/));
    expect(within(banner).getAllByTestId("stream-check")[1]).toHaveTextContent(/chain broken/);
    expect(within(banner).queryByText("Verified")).toBeNull();
  });

  it.each([
    ["cut", 5, /stream_shorter: anchored at seq 41 but the stream ends at 30/],
    ["rewritten", 6, /head_mismatch: the event at seq 41 isn't the anchored one/],
  ])("%s: 'Anchor mismatch' with the server's problem text (objects with code and detail render as text)", async (_name, index, problem) => {
    const user = userEvent.setup();
    renderAt("/ledger", client(chain(index)));
    const banner = await screen.findByTestId("chain-status");
    expect(within(banner).getByText("Anchor mismatch")).toBeInTheDocument();
    expect(banner).toHaveTextContent(/chain is intact, but it no longer matches/);
    await user.click(within(banner).getByText(/2 streams checked/));
    expect(within(banner).getAllByTestId("stream-check")[1]).toHaveTextContent(problem);
    expect(within(banner).queryByText("Verified")).toBeNull();
    expect(within(banner).queryByText("Chain broken")).toBeNull();
  });

  it("without anchors configured it says the anchors were not checked", async () => {
    renderAt("/ledger", client(chain(2)));
    expect(await screen.findByText("Chain intact, anchors not checked")).toBeInTheDocument();
  });
});
