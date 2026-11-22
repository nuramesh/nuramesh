import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createFixtureClient } from "@/api/fixture-client";
import { Providers } from "@/app/providers";
import { TooltipProvider } from "@/components/ui/tooltip";

import { BuilderPage } from "./builder-page";

type Listener = () => void;

/** A matchMedia whose phone query can be flipped by the test (a phone rotating to portrait). */
function installMedia(): { setPhone: (value: boolean) => void } {
  let phone = false;
  // jsdom reports no full-screen element as undefined; browsers say null.
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => null });
  Object.defineProperty(document, "exitFullscreen", { configurable: true, value: () => Promise.resolve() });
  const listeners = new Set<Listener>();
  const original = window.matchMedia.bind(window);
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string): MediaQueryList => {
      const isPhone = query.includes("max-width: 767px");
      return {
        get matches() {
          return isPhone ? phone : query.includes("min-width: 1280px");
        },
        media: query,
        onchange: null,
        addEventListener: (_: string, fn: Listener) => listeners.add(fn),
        removeEventListener: (_: string, fn: Listener) => listeners.delete(fn),
        dispatchEvent: () => false,
      } as unknown as MediaQueryList;
    },
  });
  restore = () => {
    Object.defineProperty(window, "matchMedia", { writable: true, configurable: true, value: original });
  };
  return {
    setPhone: (value) => {
      phone = value;
      act(() => {
        for (const fn of listeners) fn();
      });
    },
  };
}

let restore: () => void = () => undefined;
afterEach(() => {
  restore();
});

describe("builder on a phone that rotates", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("leaves full screen when the view becomes a phone, so no toggle is dead and nothing traps", async () => {
    const media = installMedia();
    render(
      <Providers client={createFixtureClient()}>
        <TooltipProvider>
          <MemoryRouter>
            <BuilderPage />
          </MemoryRouter>
        </TooltipProvider>
      </Providers>,
    );
    const root = await screen.findByTestId("builder-root");
    await userEvent.click(await screen.findByRole("button", { name: "Full screen" }));
    expect(root).toHaveAttribute("data-fullscreen", "true");
    media.setPhone(true);
    expect(root).toHaveAttribute("data-fullscreen", "false");
    media.setPhone(false);
    expect(root).toHaveAttribute("data-fullscreen", "false");
  });

  it("marks the phone notice as a note, not a live status, and keeps the heading", async () => {
    const media = installMedia();
    media.setPhone(true);
    render(
      <Providers client={createFixtureClient()}>
        <TooltipProvider>
          <MemoryRouter>
            <BuilderPage />
          </MemoryRouter>
        </TooltipProvider>
      </Providers>,
    );
    const heading = await screen.findByTestId("phone-org-name");
    expect(heading.tagName).toBe("H1");
    expect(heading.parentElement).toHaveAttribute("role", "note");
  });
});
