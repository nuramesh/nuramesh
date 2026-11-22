import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import { Providers } from "@/app/providers";
import { routes } from "@/app/routes";
import { computeIssues } from "@/builder/issues";
import { BUILT_IN_TEMPLATES } from "@/builder/templates";
import { createUnavailableClient } from "@/test/unavailable-client";

// Lazy routes load slowly on a busy machine: generous explicit waits, not the 1000 ms default.
const LOAD_WAIT_MS = 20_000;

const TEAMS = [
  { id: "scrum", name: "Scrum team", roles: ["Product owner", "Scrum master", "Developer", "Tester", "Independent reviewer"] },
  { id: "kanban", name: "Kanban team", roles: ["Flow lead", "Developer", "Tester", "Independent reviewer"] },
  { id: "review-board", name: "Review board", roles: ["Chair", "Change summarizer", "Correctness reviewer", "Security reviewer", "Maintainability reviewer"] },
  { id: "support", name: "Support team", roles: ["Support lead", "Triager", "Responder", "Reply checker"] },
  { id: "sales", name: "Sales team", roles: ["Sales lead", "Qualifier", "Outreach writer", "Claims checker"] },
  { id: "qa", name: "QA and testing team", roles: ["QA lead", "Test designer", "Test runner", "Test reviewer"] },
  { id: "devops", name: "DevOps team", roles: ["Ops lead", "Change planner", "Staging checker", "Change reviewer"] },
] as const;

function renderAt(client: ApiClient) {
  const router = createMemoryRouter(routes, { initialEntries: ["/templates"] });
  render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

function card(heading: HTMLElement): HTMLElement {
  const article = heading.closest("article");
  if (article === null) throw new Error("no card");
  return article;
}

describe("Scrum, Kanban and review-board templates (ND64)", () => {
  it.each(TEAMS)("$name is a built-in template with no builder issues and a verification line", ({ id, name, roles }) => {
    const template = BUILT_IN_TEMPLATES.find((t) => t.id === id);
    expect(template?.name).toBe(name);
    const doc = template?.document;
    expect(doc?.org.roles.map((r) => r.name)).toEqual(roles);
    if (doc === undefined) return;
    expect(computeIssues(doc.org, doc.config).issues).toEqual([]);
    const checkers = doc.org.agents.filter((a) => a.checks.length > 0);
    expect(checkers.length).toBeGreaterThan(0);
    for (const a of checkers) {
      expect(doc.org.roles.find((r) => r.id === a.role)?.kind).toBe("verifier");
      expect(a.checks).not.toContain(a.id);
    }
    expect(doc.org.roles.filter((r) => r.kind === "chief_of_staff")).toHaveLength(1);
  });

  it("the review board's reviewers run on three different models, none the worker's", () => {
    const doc = BUILT_IN_TEMPLATES.find((t) => t.id === "review-board")?.document;
    const model = (role: string) => doc?.org.roles.find((r) => r.id === role)?.default_model;
    const reviewers = ["correctness-reviewer", "security-reviewer", "maintainability-reviewer"].map(model);
    expect(new Set(reviewers).size).toBe(3);
    expect(reviewers).not.toContain(model("change-summarizer"));
  });

  it.each(TEAMS)("$name appears on the templates screen with a roles list and Use / Preview actions", async ({ name, roles }) => {
    renderAt(createFixtureClient());
    const article = card(await screen.findByRole("heading", { name }));
    expect(within(article).getByRole("button", { name: "Use template" })).toBeInTheDocument();
    expect(within(article).getByRole("button", { name: "Preview YAML" })).toBeInTheDocument();
    const list = within(article).getByRole("list", { name: "Roles" });
    expect(within(list).getAllByRole("listitem").map((i) => i.textContent)).toEqual(expect.arrayContaining(roles.slice(0, 2)));
  });

  it("they also appear when the data source can't list templates", async () => {
    renderAt(createUnavailableClient());
    for (const { name } of TEAMS) expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
  });

  it("Use template hands the team to the builder", async () => {
    const user = userEvent.setup();
    const router = renderAt(createFixtureClient());
    await user.click(within(card(await screen.findByRole("heading", { name: "Scrum team" }, { timeout: LOAD_WAIT_MS }))).getByRole("button", { name: "Use template" }));
    expect(await screen.findByText("Loaded template Scrum team. Undo to go back.", undefined, { timeout: LOAD_WAIT_MS })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/builder");
    expect(await screen.findByTestId("builder-summary", undefined, { timeout: LOAD_WAIT_MS })).toHaveTextContent("6 agents");
  }, 40_000);
});
