import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConflictError, NotAllowedError } from "@/api/client";

import { OrgStatusBar } from "./org-status-bar";
import { deferred, fixture, renderSafety } from "./test-utils";

const bar = () => screen.findByRole("region", { name: "Organization status" });

describe("roles area", () => {
  it("is a disclosure button named Roles; closed while nothing is paused", async () => {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />);
    const region = await bar();
    const toggle = await within(region).findByRole("button", { name: "Roles" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(region).queryByRole("list", { name: "Roles" })).not.toBeInTheDocument();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = within(region).getByRole("list", { name: "Roles" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => within(r).getByText(/^(researcher|writer|verifier)$/).textContent)).toEqual(["researcher", "writer", "verifier"]);
    for (const row of rows) expect(within(row).getByText("Running")).toBeInTheDocument();
  });

  it("counts paused roles in the button and opens by itself", async () => {
    renderSafety(<OrgStatusBar />, fixture("rolePaused"));
    const region = await bar();
    const toggle = await within(region).findByRole("button", { name: "Roles (1 paused)" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const row = within(within(region).getByRole("list", { name: "Roles" })).getAllByRole("listitem")[0]!;
    expect(within(row).getByText("Paused")).toBeInTheDocument();
    expect(within(row).getByText(/^human:ann paused it at /)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Resume role researcher" })).toBeInTheDocument();
    const other = within(region).getAllByRole("listitem")[1]!;
    expect(within(other).getByRole("button", { name: "Pause role writer" })).toBeInTheDocument();
  });

  it("names the breaker that paused a role", async () => {
    const api = fixture("rolePaused");
    const status = await api.org.status();
    api.org.status = () => Promise.resolve({ ...status, roles: status.roles.map((r) => (r.paused ? { ...r, cause: "breaker" as const, rule: "denied-loop", by: "service:breaker" } : r)) });
    renderSafety(<OrgStatusBar />, api);
    expect(await screen.findByText(/^Breaker denied-loop paused it at /)).toBeInTheDocument();
  });

  it("shows a paused role that is gone from the config with only Resume", async () => {
    const api = fixture("rolePaused");
    const status = await api.org.status();
    api.org.status = () =>
      Promise.resolve({ ...status, roles: [...status.roles, { ...status.roles[0]!, role: "retired", paused: true, inConfig: false }] });
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    const row = (await within(region).findByText("retired")).closest("li")!;
    expect(within(row).getByText("(removed from config)")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Resume role retired" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Pause role/ })).not.toBeInTheDocument();
  });

  it("shows no role buttons to anyone who is not a person, and says why", async () => {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />, fixture("viewer"));
    const region = await bar();
    await user.click(await within(region).findByRole("button", { name: "Roles" }));
    expect(within(region).queryByRole("button", { name: /(Pause|Resume) role/ })).not.toBeInTheDocument();
    expect(within(region).getByText("Only a signed-in person can pause or resume a role.")).toBeInTheDocument();
  });

  it("shows nothing for roles when the config has none", async () => {
    const api = fixture("running");
    const status = await api.org.status();
    api.org.status = () => Promise.resolve({ ...status, roles: [] });
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await within(region).findByText("Organization running");
    expect(within(region).queryByRole("button", { name: /^Roles/ })).not.toBeInTheDocument();
  });
});

describe("pause role dialog", () => {
  async function open(role = "writer", api = fixture("running")) {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await user.click(await within(region).findByRole("button", { name: "Roles" }));
    const opener = within(region).getByRole("button", { name: `Pause role ${role}` });
    await user.click(opener);
    const dialog = await screen.findByRole("dialog", { name: `Pause role ${role}?` });
    return { user, api, region, opener, dialog };
  }

  it("says what pausing does, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("This role's running tasks are asked to stop and none of its tasks or model calls start until a person resumes it. Other roles keep working. Steps already running finish and are charged.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("Escape cancels and focus returns to the opener", async () => {
    const { user, api, opener } = await open();
    const pause = vi.spyOn(api.org, "pauseRole");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(pause).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  });

  it("pauses that one role once; the row says Paused and offers Resume (focus stays on the button)", async () => {
    const api = fixture("running");
    const real = api.org.pauseRole.bind(api.org);
    const gate = deferred<undefined>();
    const pause = vi.fn(async (role: string) => {
      await gate.promise;
      return real(role);
    });
    api.org.pauseRole = pause;
    const { user, region, dialog } = await open("writer", api);
    await user.click(within(dialog).getByRole("button", { name: "Pause role" }));
    expect(await within(dialog).findByRole("button", { name: "Pausing..." })).toBeDisabled();
    gate.resolve(undefined);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(pause).toHaveBeenCalledTimes(1);
    expect(pause).toHaveBeenCalledWith("writer");
    const resume = await within(region).findByRole("button", { name: "Resume role writer" });
    expect(resume).toHaveFocus();
    expect(within(region).getByRole("button", { name: "Roles (1 paused)" })).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Pause role researcher" })).toBeInTheDocument();
  });

  it("treats 409 already_paused as done and refetches", async () => {
    const api = fixture("running");
    api.org.pauseRole = () => Promise.reject(new ConflictError("already", "already_paused"));
    const { user, dialog } = await open("writer", api);
    await user.click(within(dialog).getByRole("button", { name: "Pause role" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("stays open with the problem when refused", async () => {
    const api = fixture("running");
    api.org.pauseRole = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open("writer", api);
    await user.click(within(dialog).getByRole("button", { name: "Pause role" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not pause: Only a signed-in person can do this");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

describe("resume role dialog", () => {
  async function open(api = fixture("rolePaused")) {
    const user = userEvent.setup();
    renderSafety(<OrgStatusBar />, api);
    const region = await bar();
    await user.click(await within(region).findByRole("button", { name: "Resume role researcher" }));
    const dialog = await screen.findByRole("dialog", { name: "Resume role researcher?" });
    return { user, api, region, dialog };
  }

  it("says that cancelled tasks are not restarted, and starts on Cancel", async () => {
    const { dialog } = await open();
    expect(within(dialog).getByText("The role can start tasks and model calls again. Cancelled tasks are not restarted.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("resumes that one role once", async () => {
    const { user, api, region, dialog } = await open();
    const resume = vi.spyOn(api.org, "resumeRole");
    await user.click(within(dialog).getByRole("button", { name: "Resume role" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(resume).toHaveBeenCalledTimes(1);
    expect(resume).toHaveBeenCalledWith("researcher");
    expect(await within(region).findByRole("button", { name: "Pause role researcher" })).toBeInTheDocument();
  });

  it("treats 409 not_paused as done", async () => {
    const api = fixture("rolePaused");
    api.org.resumeRole = () => Promise.reject(new ConflictError("not paused", "not_paused"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Resume role" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("stays open with the problem when refused", async () => {
    const api = fixture("rolePaused");
    api.org.resumeRole = () => Promise.reject(new NotAllowedError("Only a signed-in person can do this", "human_required"));
    const { user, dialog } = await open(api);
    await user.click(within(dialog).getByRole("button", { name: "Resume role" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not resume: Only a signed-in person can do this");
  });
});
