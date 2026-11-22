import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiContext } from "@/api/context";
import type { ApiClient } from "@/api/client";
import { createFixtureClient } from "@/api/fixture-client";
import type { RunEvent } from "@/api/types";

import { useRunEvents } from "./use-run";

const ev = (seq: number): RunEvent => ({ seq, at: "2026-10-23T10:00:00Z", kind: "x", category: "tasks", text: `e${String(seq)}`, tone: "muted" });

function setup(client: ApiClient, active = true) {
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>
      <ApiContext value={client}>{children}</ApiContext>
    </QueryClientProvider>
  );
  return renderHook(({ on }) => useRunEvents("r", on), { wrapper, initialProps: { on: active } });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useRunEvents", () => {
  it("does not lose an event the stream dropped: only polls move the cursor", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const all = [ev(1), ev(2), ev(3)];
    let visible = 1;
    let push: (e: RunEvent) => void = () => undefined;
    const base = createFixtureClient();
    const client: ApiClient = {
      ...base,
      runs: {
        ...base.runs,
        events: (_id, after = 0) => {
          const events = all.slice(0, visible).filter((e) => e.seq > after);
          return Promise.resolve({ events, nextSeq: events.at(-1)?.seq ?? after });
        },
        subscribe: (_id, on) => {
          push = on;
          return () => undefined;
        },
      },
    };
    const { result } = setup(client);
    await waitFor(() => { expect(result.current.events.map((e) => e.seq)).toEqual([1]); });
    visible = 3; // seq 2 and 3 now exist; the stream drops 2 and delivers 3
    act(() => {
      push(ev(3));
    });
    expect(result.current.mode).toBe("live");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    await waitFor(() => { expect(result.current.events.map((e) => e.seq)).toEqual([3, 2, 1]); });
  });

  it("keeps its events when the run stops being active, fetches the tail once, and then stops polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls: number[] = [];
    const base = createFixtureClient();
    const client: ApiClient = { ...base, runs: { ...base.runs, events: (_i, a = 0) => { calls.push(a); return Promise.resolve({ events: [ev(1)], nextSeq: 1 }); } } };
    const { result, rerender } = setup(client);
    await waitFor(() => { expect(result.current.events).toHaveLength(1); });
    const n = calls.length;
    rerender({ on: false });
    expect(result.current.events).toHaveLength(1);
    expect(result.current.loading).toBe(false);
    await act(async () => {
      await Promise.resolve();
    });
    expect(calls.length).toBe(n + 1); // exactly one final catch-up, then nothing more
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(calls.length).toBe(n + 1);
  });
});

describe("useRunEvents tail", () => {
  it("fetches events written just before the run ended", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let visible = 1;
    const all = [ev(1), ev(2), ev(3)];
    const base = createFixtureClient();
    const client: ApiClient = {
      ...base,
      runs: {
        ...base.runs,
        events: (_i, after = 0) => {
          const events = all.slice(0, visible).filter((e) => e.seq > after);
          return Promise.resolve({ events, nextSeq: events.at(-1)?.seq ?? after });
        },
        subscribe: () => () => undefined,
      },
    };
    const { result, rerender } = setup(client);
    await waitFor(() => { expect(result.current.events.map((e) => e.seq)).toEqual([1]); });
    visible = 3;
    rerender({ on: false });
    await waitFor(() => { expect(result.current.events.map((e) => e.seq)).toEqual([3, 2, 1]); });
  });
});
