import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { RawProposals } from "@/api/server/raw";
import { body } from "@/test/contract";
import { recorded, recordedServer, type Sent } from "@/test/recorded-server";
import { renderAt } from "@/test/server-like";

import { REASON_WORDS, reasonWords } from "./proposal-words";

/**
 * The autonomy page against the server's recorded answers (`contract/solo.json`): role ids come
 * from `GET /v1/org/status` (the server client has no `orgs.get`), the caller's role from
 * `GET /v1/auth/session`, and the proposals carry the server's own status words.
 */
const notEnough = body<RawProposals>("solo", "proposals_not_enough");
const first = notEnough.items[0];
if (first === undefined) throw new Error("the recorded proposals have no item");
/**
 * No recorded scenario reaches `proposable` (it takes 30 approved asks and 20 verdicts): this is
 * the recorded item with the status word and the empty reasons `core/proposals.py` gives then.
 */
const proposable: RawProposals = { ...notEnough, items: [{ ...first, status: "proposable", reasons: [], evidence: { ...first.evidence, approved_asks: 31 }, solo: { approved_asks_solo: 10, solo_share: "0.3226" } }] };

function server(dev: { status: number; body: unknown }, role = "owner", grant: { status: number; body: unknown } = { status: 201, body: {} }) {
  const session = recorded("solo", "session_owner");
  const user = (session.body as { user: object }).user;
  return recordedServer({
    "GET /v1/org/status": () => recorded("solo", "org_status"),
    "GET /v1/auth/session": () => ({ status: 200, body: { ...(session.body as object), user: { ...user, role } } }),
    "GET /v1/autonomy/proposals": (sent: Sent) => {
      const asked = sent.url.searchParams.get("role");
      if (asked === "dev") return dev;
      return asked === "cos" || asked === "ver" ? recorded("solo", "proposals_no_ask_rule") : recorded("solo", "proposals_unknown_role");
    },
    "POST /v1/autonomy/grants": () => grant,
  });
}

async function pickDev(user: ReturnType<typeof userEvent.setup>) {
  const select = await screen.findByRole("combobox", { name: "Role" });
  expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["cos", "dev", "ver"]);
  await user.selectOptions(select, "dev");
}

describe("Autonomy page against the real server's answers (SA-18 item 2)", () => {
  it("lists the role ids of the organization status and asks the server with an id it knows", async () => {
    const user = userEvent.setup();
    const { api, calls } = server(recorded("solo", "proposals_not_enough"));
    renderAt("/autonomy", api);
    expect(await screen.findByText(/No agent has earned autonomy yet/)).toBeInTheDocument(); // `cos` has no ask rule
    await pickDev(user);
    expect(await screen.findByText("role dev, write file")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(calls.filter((c) => c.url.pathname === "/v1/autonomy/proposals").map((c) => c.url.search)).toEqual(["?role=cos", "?role=dev"]);
  });

  it("says in words why a cell cannot be granted yet, and offers no Grant", async () => {
    const user = userEvent.setup();
    renderAt("/autonomy", server(recorded("solo", "proposals_not_enough")).api);
    await pickDev(user);
    expect(await screen.findByText("not enough evidence")).toBeInTheDocument();
    expect(screen.getByText("approved asks: 0 (solo 0)")).toBeInTheDocument();
    expect(screen.getByText(reasonWords("approved_below_min"))).toBeInTheDocument();
    expect(screen.getByText(reasonWords("verdicts_below_min"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("shows the server's `ceiling` status in words", async () => {
    const user = userEvent.setup();
    renderAt("/autonomy", server(recorded("solo", "proposals_ceiling")).api);
    await pickDev(user);
    expect(await screen.findByText("not allowed by the ceiling")).toBeInTheDocument();
    expect(screen.getByText(reasonWords("ceiling_none"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("an owner grants a `proposable` cell with what they read", async () => {
    const user = userEvent.setup();
    const { api, calls } = server({ status: 200, body: proposable });
    renderAt("/autonomy", api);
    await pickDev(user);
    expect(await screen.findByText("approved asks: 31 (solo 10, 32.26 %)")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /Grant autonomy/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Your reason/ }), "reviewed the evidence");
    await user.click(within(dialog).getByRole("button", { name: "Grant autonomy" }));
    await waitFor(() => {
      expect(calls.some((c) => c.method === "POST")).toBe(true);
    });
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      subject: { kind: "role", id: "dev" },
      rule_key: 2,
      as_of: notEnough.as_of,
      ledger_position: notEnough.ledger_position,
      evidence_hash: first.evidence_hash,
      reason: "reviewed the evidence",
    });
  });

  it("a grant the server refuses (the evidence moved under the reader) is said inside the dialog", async () => {
    const user = userEvent.setup();
    // the recorded refusal of the same body for a cell that is not proposable any more
    renderAt("/autonomy", server({ status: 200, body: proposable }, "owner", recorded("solo", "grant_not_proposable")).api);
    await pickDev(user);
    await user.click(await screen.findByRole("button", { name: /Grant autonomy/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Your reason/ }), "reviewed the evidence");
    await user.click(within(dialog).getByRole("button", { name: "Grant autonomy" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not grant: the cell is not proposable");
  });

  it("only an owner is offered Grant (the grant route is the owner's)", async () => {
    const user = userEvent.setup();
    renderAt("/autonomy", server({ status: 200, body: proposable }, "approver").api);
    await pickDev(user);
    expect(await screen.findByText("can be granted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Grant autonomy/ })).toBeNull();
  });

  it("has words for every reason code the server can send", () => {
    expect(Object.keys(REASON_WORDS).sort()).toEqual([...body<string[]>("solo", "reason_codes")].sort());
  });
});
