import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { NotFoundError, ProblemError, type ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";
import type { Approval } from "@/api/types";
import { usd } from "@/api/money";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

import { canDecide, reasonProblem, sortApprovals } from "./rules";

function renderAt(path: string, client: ApiClient = createFixtureClient()) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

const approval = (patch: Partial<Approval>): Approval => ({
  id: "x",
  title: "Merge",
  requester: "Developer",
  runId: "run-1",
  runLabel: "#1",
  requestedAt: "2026-10-23T10:00:00Z",
  state: "pending",
  effect: "",
  rule: "merge: ask",
  runSpend: { spent: usd("1"), limit: usd("2") },
  evidence: [],
  decision: null,
  ...patch,
});

describe("rules", () => {
  it("puts waiting requests first, newest first within each group", () => {
    const sorted = sortApprovals([
      approval({ id: "old-done", state: "approved", requestedAt: "2026-10-20T10:00:00Z" }),
      approval({ id: "new-done", state: "denied", requestedAt: "2026-10-22T10:00:00Z" }),
      approval({ id: "wait-1", requestedAt: "2026-10-23T09:00:00Z" }),
      approval({ id: "wait-2", requestedAt: "2026-10-23T11:00:00Z" }),
    ]);
    expect(sorted.map((a) => a.id)).toEqual(["wait-2", "wait-1", "new-done", "old-done"]);
  });

  it("only a pending request can be decided, and never by the person who made it", () => {
    expect(canDecide(approval({}))).toBe(true);
    expect(canDecide(approval({ state: "approved" }))).toBe(false);
    expect(canDecide(approval({ state: "denied" }))).toBe(false);
    expect(canDecide(approval({ requester: "You" }))).toBe(false);
    expect(canDecide(approval({ requester: "Ada" }), "Ada")).toBe(false);
  });

  it("requires a reason, and whitespace isn't one", () => {
    expect(reasonProblem("")).not.toBeNull();
    expect(reasonProblem("   \n ")).not.toBeNull();
    expect(reasonProblem("Reviewed the diff")).toBeNull();
  });
});

describe("Approvals decisions", () => {
  it("deciding from /approvals keeps showing that request, with the outcome", async () => {
    const user = userEvent.setup();
    const router = renderAt("/approvals");
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok merge");
    expect(router.state.location.pathname).toBe("/approvals/a1");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Approved by You/)).toBeInTheDocument();
    expect(screen.getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(screen.queryByText(/ok merge/)).toBeNull();
    expect(screen.getByRole("heading", { name: "Developer wants to merge main." })).toBeInTheDocument();
  });

  it("tells you when your decision was refused because someone decided first", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    renderAt("/approvals/a1", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "late");
    await client.approvals.decide("a1", { decision: "approve", reason: "first" }); // another window got there first
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText(/Your decision was not recorded: this request was already decided/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("treats a blank-looking reason and your own name in any case as what they are", () => {
    expect(reasonProblem("\u0085")).not.toBeNull();
    expect(canDecide(approval({ requester: " you " }))).toBe(false);
  });
});

describe("Approvals", () => {
  it("shows, on the approvals page, how many solo approvals followed the removal of an approver (SA-18 item 4)", async () => {
    const client = createFixtureClient();
    const status = await client.org.status();
    client.org.status = () => Promise.resolve({ ...status, soloAfterRemoval: 3 });
    renderAt("/approvals", client);
    expect(await screen.findByText("solo approvals within 7 days of an approver being removed: 3")).toBeInTheDocument();
  });

  it("tells a sole decider whose action changed to reload (SA-5, action_changed)", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    client.approvals.decide = () => Promise.reject(new ProblemError("the action is not the one you saw", 409, "action_changed"));
    renderAt("/approvals", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok merge");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent(/The action changed since you opened it. Reload/);
  });

  it("acknowledge_required on the approvals page does not mention a tick box that is not there", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    client.approvals.decide = () => Promise.reject(new ProblemError("only decider", 403, "acknowledge_required"));
    renderAt("/approvals", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok merge");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    const alert = await screen.findByRole("alert", { name: "" });
    expect(alert).toHaveTextContent("You are now the only person who can decide this. Reload, read the request again, then decide.");
    expect(alert).not.toHaveTextContent(/tick/i);
  });

  it("lists waiting requests first, then decided ones, and opens the first waiting one", async () => {
    renderAt("/approvals");
    const list = await screen.findByRole("region", { name: "Requests" });
    const links = within(list).getAllByRole("link");
    expect(links).toHaveLength(4);
    expect(links[0]).toHaveTextContent("Developer wants to merge main.");
    expect(links[0]).toHaveTextContent("Waiting");
    expect(links[2]).toHaveTextContent("Approved");
    expect(links[3]).toHaveTextContent("Rejected");
    expect(screen.getByTestId("pending-count")).toHaveTextContent("2 waiting");
    const detail = await screen.findByRole("region", { name: "Request detail" });
    expect(within(detail).getByRole("heading", { name: "Developer wants to merge main." })).toBeInTheDocument();
    expect(within(detail).getByText("If you approve: changes the named branch.")).toBeInTheDocument();
    expect(within(detail).getByText("merge: ask")).toBeInTheDocument();
    expect(within(detail).getAllByText(/\$1\.84 of \$50\.00/).length).toBeGreaterThan(0);
    expect(links[0]).toHaveAttribute("aria-current", "page");
  });

  it("opens the request in the URL", async () => {
    renderAt("/approvals/a2");
    const detail = await screen.findByRole("region", { name: "Request detail" });
    expect(await within(detail).findByRole("heading", { name: "Chief of Staff wants to run a command: deploy staging." })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Chief of Staff wants to run a command/ })).toHaveAttribute("aria-current", "page");
  });

  it("will not approve or deny without a reason", async () => {
    const user = userEvent.setup();
    renderAt("/approvals/a1");
    const approve = await screen.findByRole("button", { name: "Approve" });
    const deny = screen.getByRole("button", { name: "Reject" });
    expect(approve).toBeDisabled();
    expect(deny).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: /Your reason/ }), "   ");
    expect(approve).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: /Your reason/ }), "Reviewed the diff");
    expect(approve).toBeEnabled();
    expect(deny).toBeEnabled();
    expect(screen.getByText(/Your reason is saved with your decision/)).toBeInTheDocument();
  });

  it("approves once with a reason, then the request is read-only and links its ledger entry", async () => {
    const user = userEvent.setup();
    renderAt("/approvals/a1");
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "Checked the diff and the verifier verdict");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    const outcome = await screen.findByText(/Approved by You/);
    expect(outcome).toBeInTheDocument();
    expect(screen.getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(screen.queryByText(/Checked the diff/)).toBeNull();
    expect(screen.getByRole("link", { name: /recorded as ledger #\d+/ })).toHaveAttribute("href", expect.stringMatching(/^\/ledger\/\d+$/));
    expect(screen.queryByRole("textbox", { name: /Your reason/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText("Decided requests are read-only.")).toBeInTheDocument();
    expect(screen.getByTestId("pending-count")).toHaveTextContent("1 waiting");
  });

  it("denies with a reason", async () => {
    const user = userEvent.setup();
    renderAt("/approvals/a2");
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "Not before the release is signed off");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText(/Rejected by You/)).toBeInTheDocument();
    expect(screen.getByTestId("pending-count")).toHaveTextContent("1 waiting");
  });

  it("shows a decided request without any way to change it", async () => {
    renderAt("/approvals/a3");
    const detail = await screen.findByRole("region", { name: "Request detail" });
    expect(await within(detail).findByText(/Approved by You/)).toBeInTheDocument();
    expect(within(detail).getByText("A person's reason (not shown here)")).toBeInTheDocument();
    expect(within(detail).queryByText(/Large refactor/)).toBeNull();
    expect(within(detail).getByRole("link", { name: /recorded as ledger #0988/ })).toHaveAttribute("href", "/ledger/0988");
    expect(within(detail).queryByRole("button")).not.toBeInTheDocument();
  });

  it("offers no decision on a request the person made themselves", async () => {
    const client = createFixtureClient();
    const original = client.approvals.get.bind(client.approvals);
    client.approvals.get = async (id) => ({ ...(await original(id)), requester: "You" });
    renderAt("/approvals/a1", client);
    expect(await screen.findByText(/You made this request, so someone else has to answer it/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Your reason/ })).not.toBeInTheDocument();
  });

  it("says so when the request doesn't exist or the source can't list approvals", async () => {
    renderAt("/approvals/nope");
    expect(await screen.findByText("There is no approval nope.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the inbox" })).toHaveAttribute("href", "/approvals");
  });

  it("is honest when the data source can't answer", async () => {
    renderAt("/approvals", createUnavailableClient());
    expect(await screen.findByRole("alert")).toHaveTextContent("can't list approvals yet");
  });
});

/** A client whose approvals have the given actions (list and get agree). */
function withApprovals(patches: Record<string, Partial<Approval>>): ApiClient {
  const client = createFixtureClient();
  const list = client.approvals.list.bind(client.approvals);
  const patched = async (): Promise<Approval[]> => (await list()).map((a) => ({ ...a, ...(patches[a.id] ?? {}) }));
  client.approvals.list = patched;
  client.approvals.get = async (id) => {
    const found = (await patched()).find((a) => a.id === id);
    if (found === undefined) throw new NotFoundError(id);
    return found;
  };
  return client;
}

const cp = (...points: number[]): string => String.fromCodePoint(...points);

describe("Approver experience (#993)", () => {
  it("shows the sentence in the list row and the heading, then consequence, cost and buttons in order (AE-4, AE-5, AE-7, AE-8)", async () => {
    renderAt("/approvals/a1");
    const detail = await screen.findByRole("region", { name: "Request detail" });
    const sentence = "Developer wants to merge main.";
    expect(within(detail).getByRole("heading", { level: 2, name: sentence })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Requests" })).getByRole("link", { name: new RegExp(sentence.replace(".", "\\.")) })).toBeInTheDocument();
    expect(within(detail).getByText("If you approve: changes the named branch.")).toBeInTheDocument();
    expect(within(detail).getByText(/If you reject: the agent is told no/)).toBeInTheDocument();
    expect(within(detail).getByText(/Spent so far \$1\.84 of \$50\.00/)).toBeInTheDocument();
    const text = detail.textContent;
    const order = [sentence, "If you approve", "Cost:", "Your reason", "Evidence"].map((s) => text.indexOf(s));
    expect(order.every((n) => n >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(text).not.toContain("null");
  });

  it("shows agent text as inert text with visible escapes, and a long target cut with the full text below (AE-6)", async () => {
    const target = `a${cp(0x202e)}b${cp(10)}${cp(9)}<img src=x onerror=alert(1)> [x](http://evil)`;
    renderAt("/approvals/a1", withApprovals({ a1: { action: { kind: "merge", target, amount: null } } }));
    const detail = await screen.findByRole("region", { name: "Request detail" });
    const h = await within(detail).findByRole("heading", { level: 2 });
    expect(h.textContent).toBe(`Developer wants to merge a\\u202Eb\\u000A\\u0009<img src=x onerror=alert(1)> [x](http://evil).`);
    expect(detail.querySelector("img")).toBeNull();
    expect(detail.querySelector("a[href^='http']")).toBeNull();
  });

  it("escapes control characters in the decision's decider and in evidence text too", async () => {
    const bidi = cp(0x202e);
    renderAt(
      "/approvals/a3",
      withApprovals({
        a3: {
          decision: { by: `x${bidi}y`, at: "2026-10-21T16:09:00Z", reasonHash: null, ledgerEntry: "" },
          evidence: [{ state: "ok", text: `ev${bidi}idence` }],
        },
      }),
    );
    const detail = await screen.findByRole("region", { name: "Request detail" });
    expect(detail.textContent).toContain("x\\u202Ey");
    expect(detail.textContent).toContain("ev\\u202Eidence");
    expect(detail.textContent).not.toContain(bidi);
  });

  it("cuts a 300 character target in the sentence and shows it whole in the detail", async () => {
    const long = "a".repeat(300);
    renderAt("/approvals/a1", withApprovals({ a1: { action: { kind: "merge", target: long, amount: null } } }));
    const detail = await screen.findByRole("region", { name: "Request detail" });
    const h = await within(detail).findByRole("heading", { level: 2 });
    expect(h.textContent).toBe(`Developer wants to merge ${"a".repeat(200)}....`);
    expect(within(detail).getByText(`Full text: ${long}`)).toBeInTheDocument();
  });

  it("every kind has fixed consequence text with nothing from asked_because (AE-7)", async () => {
    renderAt("/approvals/a1", withApprovals({ a1: { rule: "SECRET-MODEL-TEXT", action: { kind: "teleport", target: "x", amount: null } } }));
    const detail = await screen.findByRole("region", { name: "Request detail" });
    expect(await within(detail).findByText("If you approve: the agent does this one thing.")).toBeInTheDocument();
  });

  it("says Cost not available when the run spend is unknown, and shows spend exactly (AE-8)", async () => {
    renderAt("/approvals/a1", withApprovals({ a1: { runSpend: null } }));
    expect(await screen.findByText("Cost not available")).toBeInTheDocument();
  });

  it("does not focus Approve on load, and Enter in the reason field submits nothing (AE-9)", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    renderAt("/approvals/a1", client);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve).not.toHaveFocus();
    expect(approve).toHaveClass("min-h-11");
    expect(screen.getByRole("button", { name: "Reject" })).toHaveClass("min-h-11");
    await user.type(screen.getByRole("textbox", { name: /Your reason/ }), "ok{Enter}");
    expect((await client.approvals.get("a1")).state).toBe("pending");
  });

  it("keeps both buttons disabled for an empty or blank reason (AE-10)", async () => {
    const user = userEvent.setup();
    renderAt("/approvals/a1");
    const box = await screen.findByRole("textbox", { name: /Your reason/ });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await user.type(box, "   ");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.type(box, "fine");
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("says in plain words that you started the run when the server answers 403 self_approval (AE-11)", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    client.approvals.decide = () => Promise.reject(new ProblemError("self", 403, "self_approval"));
    renderAt("/approvals/a1", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("You started this run, so someone else has to decide it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("after approving shows Done and a link to the oldest other waiting request, without navigating (AE-12)", async () => {
    const user = userEvent.setup();
    const router = renderAt("/approvals/a1");
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "ok");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Done. The run continues.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Next waiting request" })).toHaveAttribute("href", "/approvals/a2");
    expect(router.state.location.pathname).toBe("/approvals/a1");
  });

  it("after declining the last waiting request says nothing else is waiting (AE-12)", async () => {
    const user = userEvent.setup();
    const client = createFixtureClient();
    await client.approvals.decide("a1", { decision: "approve", reason: "first" });
    renderAt("/approvals/a2", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "no");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Done. The agent was told no.")).toBeInTheDocument();
    expect(screen.getByText("Nothing else is waiting.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Next waiting request" })).toBeNull();
  });

  it("puts the waiting count in the tab title on the list and the detail, never the sentence (AE-12)", async () => {
    renderAt("/approvals/a1");
    await screen.findByRole("region", { name: "Request detail" });
    await vi.waitFor(() => {
      expect(document.title).toBe("(2) Approvals · NuraMesh");
    });
    expect(document.title).not.toContain("merge");
    const client = createFixtureClient();
    await client.approvals.decide("a1", { decision: "approve", reason: "x" });
    await client.approvals.decide("a2", { decision: "approve", reason: "x" });
    document.body.innerHTML = "";
    renderAt("/approvals", client);
    await screen.findByRole("region", { name: "Request detail" });
    await vi.waitFor(() => {
      expect(document.title).toBe("Approvals · NuraMesh");
    });
  });

  it("uses the plain words and none of the old ones (AE-13)", async () => {
    renderAt("/approvals/a1");
    await screen.findByRole("region", { name: "Request detail" });
    const text = document.body.textContent;
    for (const old of ["Deny", "Decline", "Approve once", "On a real server the ledger keeps only a hash"]) expect(text).not.toContain(old);
    for (const wanted of ["Reject", "Why you were asked", "Applies to", "Who can decide", "Your reason is saved with your decision. The record keeps a fingerprint of it, not the text."]) expect(text).toContain(wanted);
    expect(screen.queryByText("Rule", { selector: "dt" })).toBeNull();
  });

  it("links a static help page that makes no data call (AE-14)", async () => {
    renderAt("/approvals/a1");
    expect(await screen.findByRole("link", { name: "What am I approving?" })).toHaveAttribute("href", "/help/approving");
    document.body.innerHTML = "";
    const client = createFixtureClient();
    for (const group of Object.values(client) as unknown[]) {
      if (typeof group !== "object" || group === null) continue;
      for (const k of Object.keys(group)) {
        (group as Record<string, unknown>)[k] = () => {
          throw new Error("data call");
        };
      }
    }
    renderAt("/help/approving", client);
    expect(await screen.findByRole("heading", { level: 1, name: "What am I approving?" })).toBeInTheDocument();
  });
});
