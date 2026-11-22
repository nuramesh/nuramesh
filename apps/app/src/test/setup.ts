import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// The default findBy*/waitFor limit is 1 s, which a loaded machine (parallel workers, a busy CI runner or
// a neighbouring test run) overruns while the app is still rendering. A wait that succeeds returns at
// once, so a long limit costs nothing on a healthy run and only changes how long a real bug takes to fail.
configure({ asyncUtilTimeout: 15_000 });

afterEach(() => {
  cleanup();
  document.documentElement.className = "";
  try {
    window.localStorage.clear();
  } catch {
    // Some tests replace storage with one that throws.
  }
});

// jsdom has no ResizeObserver (React Flow measures with it); a no-op one is enough for tests.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe(): void {
      // Nothing to measure in jsdom.
    }
    unobserve(): void {
      // Nothing to measure in jsdom.
    }
    disconnect(): void {
      // Nothing to measure in jsdom.
    }
  };
}

// jsdom has no matchMedia; tests that care replace it with their own.
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  });
}

// Tabs of one test file must not hear each other's sign-in signals; the tab-sync tests stub their own BroadcastChannel.
Reflect.deleteProperty(globalThis, "BroadcastChannel");
