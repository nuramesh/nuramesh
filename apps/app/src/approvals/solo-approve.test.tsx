import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { NotAllowedError, ProblemError, type ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { usd } from "@/api/money";
import type { Approval, DecideInput } from "@/api/types";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";

/** Obviously fake: never a real secret. */
const PASSWORD = "obviously-fake-test-password";
const DIGEST = "d".repeat(64);

type Solo = NonNullable<Approval["solo"]>;

const base: Approval = {
  id: "s1",
  title: "Merge to main",
  requester: "dana",
  role: "developer",
  action: { kind: "merge", target: "main", amount: null },
  runId: "run-128",
  runLabel: "#128",
  taskId: "t-retries",
  requestedAt: "2026-10-23T10:00:00Z",
  state: "pending",
  effect: "",
  rule: "merge: ask",
  runSpend: { spent: usd("1.84"), limit: usd("50.00") },
  evidence: [],
  decision: null,
  solo: { actionDigest: DIGEST, freshProofNeeded: false, proofs: [] },
};

interface World {
  client: ApiClient;
  decide: ReturnType<typeof vi.fn<(id: string, input: DecideInput) => Promise<{ result: Approval; ledgerEntry: string | null }>>>;
  get: ReturnType<typeof vi.fn<(id: string) => Promise<Approval>>>;
  confirmPassword: ReturnType<typeof vi.fn<(password: string) => Promise<void>>>;
}

/** The sample client showing one request (`patch` over a sole decider's merge), with spies on decide, get and the password confirm. */
function world(patch: Partial<Approval> = {}, options: { signedIn?: boolean } = {}): World {
  const client = createFixtureClient(
    options.signedIn === true ? { signIn: { account: { email: "ann@example.test", password: PASSWORD }, signedIn: true } } : {},
  );
  let current: Approval = { ...base, ...patch };
  const get = vi.fn<(id: string) => Promise<Approval>>(() => Promise.resolve(current));
  const decide = vi.fn<(id: string, input: DecideInput) => Promise<{ result: Approval; ledgerEntry: string | null }>>((_id, input) => {
    current = { ...current, state: input.decision === "approve" ? "approved" : "denied", solo: null, decision: { by: "You", at: "2026-10-23T10:05:00Z", reasonHash: null, ledgerEntry: "" } };
    return Promise.resolve({ result: current, ledgerEntry: null });
  });
  const confirmPassword = vi.fn<(password: string) => Promise<void>>(() => Promise.resolve());
  client.approvals.list = () => Promise.resolve([current]);
  client.approvals.get = get;
  client.approvals.decide = decide;
  if (client.auth !== undefined) client.auth.confirmPassword = confirmPassword;
  return { client, decide, get, confirmPassword };
}

function renderAt(path: string, client: ApiClient) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

/** Writes a reason and presses the page's Approve; returns the user and the dialog (if one opened). */
async function pressApprove(client: ApiClient) {
  const user = userEvent.setup();
  renderAt("/approvals/s1", client);
  await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "read the diff");
  await user.click(screen.getByRole("button", { name: "Approve" }));
  return user;
}

const solo = (patch: Partial<Solo>): Solo => ({ actionDigest: DIGEST, freshProofNeeded: false, proofs: [], ...patch });

describe("solo approve: the confirm dialog (SA-17)", () => {
  it("opens a dialog instead of sending, with every line a sole decider must see, and Cancel as the focused action", async () => {
    const { client, decide } = world();
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    expect(decide).not.toHaveBeenCalled();
    const line = (label: string): HTMLElement => {
      const term = within(dialog).getByText(label, { selector: "dt" });
      return term.nextElementSibling as HTMLElement;
    };
    expect(line("Agent")).toHaveTextContent("dana");
    expect(line("Role")).toHaveTextContent("developer");
    expect(line("Task")).toHaveTextContent("t-retries");
    expect(line("Kind")).toHaveTextContent("merge");
    expect(line("Target")).toHaveTextContent("main");
    expect(line("Amount")).toHaveTextContent("none");
    expect(line("Why you were asked")).toHaveTextContent("merge: ask");
    expect(line("Asked")).toHaveTextContent(/ago/);
    expect(line("Run spend so far")).toHaveTextContent("$1.84");
    expect(line("Budget left")).toHaveTextContent("$48.16");
    expect(within(dialog).getByText("If you approve: changes the named branch.")).toBeInTheDocument();
    expect(within(dialog).getByText("Nobody else will review this.")).toBeInTheDocument();
    expect(within(dialog).queryByText("You started this run.")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    // no bulk action anywhere on the page
    expect(screen.queryByRole("button", { name: /approve all|approve selected/i })).toBeNull();
  });

  it("says 'You started this run.' before 'Nobody else will review this.' for the originator", async () => {
    const { client } = world({ startedByYou: true });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    const text = dialog.textContent;
    expect(text).toContain("You started this run.");
    expect(text.indexOf("You started this run.")).toBeLessThan(text.indexOf("Nobody else will review this."));
  });

  it("shows the whole target as inert text: nothing cut, control and bidirectional characters made visible", async () => {
    const target = `${"a".repeat(250)}‮gnp.exe\u0007`;
    const { client } = world({ action: { kind: "write_file", target, amount: null } });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    const shown = within(dialog).getByText("Target", { selector: "dt" }).nextElementSibling as HTMLElement;
    expect(shown.textContent).toBe(`${"a".repeat(250)}\\u202Egnp.exe\\u0007`);
    expect(shown.querySelector("a")).toBeNull();
  });

  it("says so when the role, the run's spend or a budget is not known, and shows a spend's amount", async () => {
    const { client } = world({ role: null, taskId: null, runSpend: null, action: { kind: "spend", target: null, amount: usd("12.50") } });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    const line = (label: string): HTMLElement => within(dialog).getByText(label, { selector: "dt" }).nextElementSibling as HTMLElement;
    expect(line("Role")).toHaveTextContent("not available");
    expect(line("Task")).toHaveTextContent("no task");
    expect(line("Target")).toHaveTextContent("(no target given)");
    expect(line("Amount")).toHaveTextContent("$12.50");
    expect(line("Run spend so far")).toHaveTextContent("not available");
    expect(line("Budget left")).toHaveTextContent("not available");
  });

  it("a kind that is not high-risk needs no typed word: the dialog's confirm sends one approve with the digest", async () => {
    const { client, decide } = world({ action: { kind: "write_file", target: "README.md", amount: null } });
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Approve on my own" }));
    expect(await screen.findByText("Done. The run continues.")).toBeInTheDocument();
    expect(decide).toHaveBeenCalledTimes(1);
    expect(decide).toHaveBeenCalledWith("s1", { decision: "approve", reason: "read the diff", solo: { actionDigest: DIGEST } });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a high-risk kind needs the kind word typed; Enter in the field never approves", async () => {
    const { client, decide } = world();
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Approve on my own" });
    expect(confirm).toBeDisabled();
    const word = within(dialog).getByRole("textbox", { name: /Type merge to confirm/ });
    await user.type(word, "merg{Enter}");
    expect(confirm).toBeDisabled();
    await user.type(word, "e{Enter}");
    expect(decide).not.toHaveBeenCalled();
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => { expect(decide).toHaveBeenCalledTimes(1); });
    expect(decide.mock.calls[0]?.[1]).toEqual({ decision: "approve", reason: "read the diff", solo: { actionDigest: DIGEST } });
  });

  it("Cancel closes the dialog and sends nothing", async () => {
    const { client, decide } = world();
    const user = await pressApprove(client);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(decide).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: /Your reason/ })).toHaveValue("read the diff");
  });
});

describe("solo approve: the fresh proof comes first (SA-7, SA-8)", () => {
  it("asks for the password, confirms it on the server, and only then lets one approve through", async () => {
    const { client, decide, confirmPassword } = world({ solo: solo({ freshProofNeeded: true, proofs: ["password", "sign_in"] }) }, { signedIn: true });
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Type merge to confirm/ }), "merge");
    const confirm = within(dialog).getByRole("button", { name: "Approve on my own" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Your password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm password" }));
    expect(await within(dialog).findByText(/Password confirmed/)).toBeInTheDocument();
    expect(confirmPassword).toHaveBeenCalledTimes(1);
    expect(confirmPassword).toHaveBeenCalledWith(PASSWORD);
    expect(decide).not.toHaveBeenCalled();
    expect(within(dialog).queryByLabelText("Your password")).toBeNull();
    await user.click(confirm);
    await waitFor(() => { expect(decide).toHaveBeenCalledTimes(1); });
    expect(confirmPassword).toHaveBeenCalledTimes(1);
  });

  it("a wrong password is said plainly and nothing is approved", async () => {
    const { client, decide, confirmPassword } = world({ solo: solo({ freshProofNeeded: true, proofs: ["password", "sign_in"] }) }, { signedIn: true });
    confirmPassword.mockRejectedValue(new ProblemError("invalid credentials", 401, "invalid_credentials"));
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Your password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm password" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("That password is not right. Nothing was approved.");
    expect(within(dialog).getByRole("button", { name: "Approve on my own" })).toBeDisabled();
    expect(decide).not.toHaveBeenCalled();
  });

  it("a locked account is told how long to wait", async () => {
    const { client, confirmPassword } = world({ solo: solo({ freshProofNeeded: true, proofs: ["password", "sign_in"] }) }, { signedIn: true });
    confirmPassword.mockRejectedValue(new ProblemError("too many attempts", 429, "too_many_attempts", 240));
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Your password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm password" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Too many wrong passwords. Try again in 4 minutes.");
  });

  it("where only the second factor counts, it says to sign in again and offers no approve", async () => {
    const { client, decide, confirmPassword } = world({ solo: solo({ freshProofNeeded: true, proofs: ["mfa"] }) }, { signedIn: true });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Sign out, sign in again with your second factor/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Your password")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Approve on my own" })).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(decide).not.toHaveBeenCalled();
    expect(confirmPassword).not.toHaveBeenCalled();
  });

  it("where no proof can help, it says why and offers no approve", async () => {
    const { client, decide } = world({ solo: solo({ freshProofNeeded: true, proofs: [] }) }, { signedIn: true });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/needs a second sign-in step, and it is not set up for you or not available on this server/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Approve on my own" })).toBeNull();
    expect(decide).not.toHaveBeenCalled();
  });

  it("a source without the confirm route says to sign in again instead of asking for a password", async () => {
    const { client } = world({ solo: solo({ freshProofNeeded: true, proofs: ["password", "sign_in"] }) });
    await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Sign out and sign in again/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Approve on my own" })).toBeNull();
  });

  it("a proof that ran out while the dialog was open: the refusal turns into the password step", async () => {
    const { client, decide, confirmPassword } = world({}, { signedIn: true });
    decide.mockRejectedValueOnce(new ProblemError("sign in again or confirm your password", 403, "step_up_required"));
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Type merge to confirm/ }), "merge");
    await user.click(within(dialog).getByRole("button", { name: "Approve on my own" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/more than 10 minutes/);
    await user.type(within(dialog).getByLabelText("Your password"), PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Confirm password" }));
    await within(dialog).findByText(/Password confirmed/);
    expect(confirmPassword).toHaveBeenCalledTimes(1);
    await user.click(within(dialog).getByRole("button", { name: "Approve on my own" }));
    await waitFor(() => { expect(decide).toHaveBeenCalledTimes(2); });
    expect(await screen.findByText("Done. The run continues.")).toBeInTheDocument();
  });
});

describe("solo approve: refusals in plain words", () => {
  it.each([
    ["mfa_required", 403, undefined, /second-factor sign-in is more than 10 minutes old\. Sign out, sign in again with your second factor/],
    ["step_up_required", 403, undefined, /sign-in is more than 10 minutes old\. Confirm your password/],
    ["mfa_unavailable", 403, undefined, /this server cannot check one right now\. Nothing was approved/],
    ["mfa_enrollment_required", 403, undefined, /you have not set one up\. Nothing was approved/],
    ["solo_rate_limited", 429, 300, /as often as this organization allows in one hour\. Try again in 5 minutes\./],
  ] as const)("%s", async (title, status, retryAfter, words) => {
    const { client, decide } = world({ action: { kind: "write_file", target: "README.md", amount: null } }, { signedIn: true });
    decide.mockRejectedValue(new ProblemError(`${title} server detail`, status, title, retryAfter));
    const user = await pressApprove(client);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve on my own" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(words);
    expect(alert).not.toHaveTextContent("server detail");
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it("409 action_changed: the dialog closes, the request is read again and the page says nothing was approved", async () => {
    const { client, decide, get } = world({ action: { kind: "write_file", target: "README.md", amount: null } });
    decide.mockRejectedValue(new ProblemError("the action is not the one you saw", 409, "action_changed"));
    const user = await pressApprove(client);
    const before = get.mock.calls.length;
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Approve on my own" }));
    expect(await screen.findByText(/This request changed after you opened it, so nothing was approved/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => { expect(get.mock.calls.length).toBeGreaterThan(before); });
    expect(decide).toHaveBeenCalledTimes(1);
  });

  it("a request decided meanwhile closes the dialog and is read-only", async () => {
    const { client, decide } = world({ action: { kind: "write_file", target: "README.md", amount: null } });
    decide.mockRejectedValue(new NotAllowedError("approval 's1' is already decided"));
    const user = await pressApprove(client);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Approve on my own" }));
    expect(await screen.findByText(/Your decision was not recorded: this request was already decided/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("peer approves and every reject are unchanged (SA-4, SA-16)", () => {
  it("a peer approve opens no dialog and never carries the solo terms", async () => {
    const { client, decide } = world({ solo: null });
    await pressApprove(client);
    await waitFor(() => { expect(decide).toHaveBeenCalledTimes(1); });
    expect(decide).toHaveBeenCalledWith("s1", { decision: "approve", reason: "read the diff" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a sole decider's reject opens no dialog and never carries the solo terms", async () => {
    const { client, decide } = world();
    const user = userEvent.setup();
    renderAt("/approvals/s1", client);
    await user.type(await screen.findByRole("textbox", { name: /Your reason/ }), "not now");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => { expect(decide).toHaveBeenCalledTimes(1); });
    expect(decide).toHaveBeenCalledWith("s1", { decision: "reject", reason: "not now" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
