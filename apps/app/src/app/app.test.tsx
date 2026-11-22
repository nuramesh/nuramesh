import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "@/components/error-boundary";
import { ErrorFallback } from "@/components/error-fallback";

import { createFixtureClient } from "@/api/fixture-client";
import { createUnavailableClient } from "@/test/unavailable-client";

import { Providers } from "./providers";
import { routes } from "./routes";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <Providers client={createFixtureClient()}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return router;
}

describe("app shell", () => {
  it("has banner, navigation and main landmarks and a skip link", async () => {
    renderAt("/builder");
    // The builder renders (not an error page).
    // The lazy route can take seconds on a busy machine; the suite-wide wait limit (src/test/setup.ts) covers it.
    expect(await screen.findByRole("region", { name: "Organization canvas" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("My organization");
    expect(screen.getByRole("banner")).toBeInTheDocument(); // the top bar (shown below 768 px)
    expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(1);
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });

  it("shows a 404 page for unknown routes, with a way back", async () => {
    const router = renderAt("/no/such/page");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText("/no/such/page")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Go to the builder" }));
    expect(router.state.location.pathname).toBe("/builder");
  });

  it("switches theme from the header menu", async () => {
    const user = userEvent.setup();
    renderAt("/builder");
    await user.click(await screen.findByRole("button", { name: /^Theme:/ }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(screen.getByRole("button", { name: "Theme: Dark" })).toBeInTheDocument();
  });
});

describe("organization status bar (E19)", () => {
  it.each(["/templates", "/runs", "/approvals", "/budgets", "/ledger", "/settings", "/no/such/page"])(
    "is on %s, the first thing in the scrolling page (in flow, never pinned over content)",
    async (path) => {
      renderAt(path);
      const bar = await screen.findByRole("region", { name: "Organization status" });
      expect(await within(bar).findByText("Organization running")).toBeInTheDocument();
      const main = screen.getByRole("main");
      expect(main).toContainElement(bar);
      expect(main.firstElementChild).toBe(bar);
    },
  );

  it("is on /builder, above the canvas, outside the main landmark and before it", async () => {
    renderAt("/builder");
    const bar = await screen.findByRole("region", { name: "Organization status" });
    expect(await within(bar).findByText("Organization running")).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(main).not.toContainElement(bar);
    expect(bar.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows a halt on every page, with the Resume button for a person", async () => {
    const router = createMemoryRouter(routes, { initialEntries: ["/runs"] });
    render(
      <Providers client={createFixtureClient({ orgSafety: "halted" })}>
        <RouterProvider router={router} />
      </Providers>,
    );
    const bar = await screen.findByRole("region", { name: "Organization status" });
    expect(await within(bar).findByText("Organization halted")).toBeInTheDocument();
    await userEvent.click(within(bar).getByRole("button", { name: "Resume organization" }));
    expect(await screen.findByRole("dialog", { name: "Resume the organization?" })).toBeInTheDocument();
  });

  it("keeps the page usable when the status cannot be read", async () => {
    const client = { ...createUnavailableClient(), templates: createFixtureClient().templates };
    render(
      <Providers client={client}>
        <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/templates"] })} />
      </Providers>,
    );
    expect(await screen.findByText("Could not read the organization status.")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "Templates" })).toBeInTheDocument();
  });
});

describe("routes and sidebar", () => {
  it("sends / to the builder", async () => {
    const router = renderAt("/");
    expect(await screen.findByRole("region", { name: "Organization canvas" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/builder");
  });

  it.each([
    ["/templates", "Templates"],
    ["/budgets", "Budgets and spend"],
    ["/settings", "Settings"],
  ])("%s is a page with a heading and no invented data", async (path, title) => {
    renderAt(path);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    expect(screen.getByRole("main")).not.toHaveTextContent(/\$\d/);
  });

  it("lists the spec's navigation and marks the current page", async () => {
    renderAt("/runs");
    await screen.findByRole("heading", { level: 1, name: "Runs" });
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    for (const label of ["Org builder", "Templates", "Runs", "Approvals", "Budgets and spend", "Ledger"]) {
      expect(nav.getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(nav.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    expect(nav.getByRole("link", { name: "Ledger" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });

  it("opens and closes the menu on small screens, and closes it when you navigate", async () => {
    const user = userEvent.setup();
    renderAt("/settings");
    await screen.findByRole("heading", { level: 1, name: "Settings" });
    const menu = screen.getByRole("button", { name: "Open menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("app-sidebar")).toHaveClass("hidden");
    await user.click(menu);
    expect(screen.getByRole("button", { name: "Close menu" })).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById("app-sidebar")).toHaveClass("flex");
    await user.keyboard("{Escape}");
    expect(document.getElementById("app-sidebar")).toHaveClass("hidden");
    expect(screen.getByRole("button", { name: "Open menu" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Ledger" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Ledger" })).toBeInTheDocument();
    expect(document.getElementById("app-sidebar")).toHaveClass("hidden");
  });
});

describe("ErrorBoundary", () => {
  function Boom({ fail }: { fail: boolean }) {
    if (fail) throw new Error("kaboom");
    return <p>recovered</p>;
  }

  it("shows the error and recovers on retry", async () => {
    const onError = vi.fn();
    let fail = true;
    const { rerender } = render(
      <ErrorBoundary onError={onError}>
        <Boom fail={fail} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("kaboom");
    expect(onError).toHaveBeenCalledOnce();

    fail = false;
    rerender(
      <ErrorBoundary onError={onError}>
        <Boom fail={fail} />
      </ErrorBoundary>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("recovered")).toBeInTheDocument();
  });

  it("shows page errors inside the shell, keeping header and landmarks", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const Thrower = () => {
      throw new Error("route failed");
    };
    const shell = routes[0];
    const pages = shell?.children?.[0];
    if (shell === undefined || pages === undefined) throw new Error("unexpected route tree");
    const router = createMemoryRouter(
      [
        {
          element: shell.element,
          errorElement: shell.errorElement,
          children: [{ errorElement: pages.errorElement, children: [{ path: "/", element: <Thrower /> }] }],
        },
      ],
      { initialEntries: ["/"] },
    );
    render(
      <Providers client={createFixtureClient()}>
        <RouterProvider router={router} />
      </Providers>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("route failed");
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("main")).toContainElement(screen.getByRole("alert"));
  });

  it("hides raw error messages unless details are on (production default)", () => {
    render(<ErrorFallback error={new Error("secret internals")} showDetails={false} />);
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret internals");
  });
});

describe("sidebar facts from the API client", () => {
  it("shows the live-run count, the approvals badge, the month's spend and that the data is sample data", async () => {
    renderAt("/runs");
    const nav = within(await screen.findByRole("navigation", { name: "Main" }));
    expect(await nav.findByText("1 live")).toBeInTheDocument(); // only #128 is running (the design's "1 live")
    expect(await nav.findByRole("link", { name: /Approvals/ })).toHaveTextContent(/2\s*waiting/);
    expect(screen.getByText("example-org")).toBeInTheDocument();
    expect(screen.getByText("EX")).toBeInTheDocument(); // the design's initials
    expect(await screen.findByText("$42.10")).toBeInTheDocument();
    expect(screen.getByText("of $200.00 · 21%")).toBeInTheDocument();
    expect(screen.getByText(/Sample data · main/)).toBeInTheDocument();
  });

  it("shows nothing invented when the data source can't answer", async () => {
    const { createUnavailableClient } = await import("@/test/unavailable-client");
    const router = createMemoryRouter(routes, { initialEntries: ["/runs"] });
    render(
      <Providers client={createUnavailableClient()}>
        <RouterProvider router={router} />
      </Providers>,
    );
    expect(await screen.findByText("Not connected")).toBeInTheDocument();
    expect(screen.queryByText(/live$/)).not.toBeInTheDocument();
    expect(screen.queryByText("THIS MONTH")).not.toBeInTheDocument();
  });
});
