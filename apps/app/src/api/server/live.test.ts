import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveStatus, RunEvent } from "../types";
import { connectLive, type SocketLike } from "./live";

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  onopen: SocketLike["onopen"] = null;
  onmessage: SocketLike["onmessage"] = null;
  onclose: SocketLike["onclose"] = null;
  onerror: SocketLike["onerror"] = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  close(): void {
    this.closed = true;
  }
  open(): void {
    this.onopen?.({});
  }
  send(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  drop(code = 1006): void {
    this.onclose?.({ code });
  }
}

const frame = (seq: number, type = "task.created") => ({
  stream: "run:r1",
  event: { stream: "run:r1", seq, type, actor: "system", at: "2026-10-24T10:00:00Z", run_id: "r1", task_id: null, payload: {}, cost: null, cost_basis: null, hash: "h", prev_hash: "p" },
});

function start(afterSeq = 0) {
  const events: RunEvent[] = [];
  const statuses: LiveStatus[] = [];
  const stop = connectLive({ url: "ws://x/v1/runs/r1/events", afterSeq, onEvent: (e) => events.push(e), onStatus: (s) => statuses.push(s), socket: (url) => new FakeSocket(url) });
  return { events, statuses, stop, socket: (): FakeSocket => {
      const latest = FakeSocket.all.at(-1);
      if (latest === undefined) throw new Error("no socket was opened");
      return latest;
    },
  };
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the per-run live connection", () => {
  it("connects asking after the cursor, reports live and hands over each entry once", () => {
    const { events, statuses, socket } = start(5);
    expect(socket().url).toBe("ws://x/v1/runs/r1/events?after_seq=5");
    expect(statuses).toEqual(["connecting"]);
    socket().open();
    socket().send(frame(6));
    socket().send({ type: "caught_up", cursor: { "run:r1": 6 } }); // not an entry: ignored
    socket().send(frame(7, "gate.completed"));
    socket().send(frame(7, "gate.completed")); // a repeat is dropped
    expect(statuses).toEqual(["connecting", "live"]);
    expect(events.map((e) => [e.seq, e.kind])).toEqual([[6, "task.created"], [7, "gate.completed"]]);
  });

  it("ignores frames it cannot read", () => {
    const { events, socket } = start();
    socket().open();
    socket().onmessage?.({ data: "not json" });
    socket().onmessage?.({ data: new ArrayBuffer(2) });
    expect(events).toEqual([]);
  });

  it("reconnects after a drop from the last entry seen, with growing pauses that reset on success", () => {
    const { events, statuses, socket } = start();
    socket().open();
    socket().send(frame(1));
    socket().send(frame(2));
    socket().drop();
    expect(statuses.at(-1)).toBe("reconnecting");
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(999);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(2);
    expect(socket().url).toContain("after_seq=2"); // nothing missed, nothing repeated
    socket().drop(); // fails before opening: the pause doubles
    vi.advanceTimersByTime(1_999);
    expect(FakeSocket.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(3);
    socket().open();
    socket().send(frame(3));
    expect(statuses.at(-1)).toBe("live");
    socket().drop();
    vi.advanceTimersByTime(1_000); // back to the first pause after a success
    expect(FakeSocket.all).toHaveLength(4);
    expect(events.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it("caps the pause at 15 seconds", () => {
    const { socket } = start();
    for (let i = 0; i < 8; i += 1) {
      socket().drop();
      vi.advanceTimersByTime(15_000);
    }
    expect(FakeSocket.all).toHaveLength(9);
  });

  it("on 4401 renews the session, then reconnects at once from the last seq", async () => {
    const recover = vi.fn(() => Promise.resolve("renewed" as const));
    const statuses: LiveStatus[] = [];
    const events: RunEvent[] = [];
    connectLive({ url: "ws://x/v1/runs/r1/events", afterSeq: 0, onEvent: (e) => events.push(e), onStatus: (s) => statuses.push(s), socket: (url) => new FakeSocket(url), onUnauthorized: recover });
    FakeSocket.all[0]?.send(frame(1));
    FakeSocket.all[0]?.send(frame(2));
    FakeSocket.all[0]?.drop(4401);
    await vi.advanceTimersByTimeAsync(0);
    expect(recover).toHaveBeenCalledTimes(1);
    expect(FakeSocket.all).toHaveLength(2);
    expect(FakeSocket.all[1]?.url).toContain("after_seq=2");
    expect(statuses.at(-1)).not.toBe("closed");
  });

  it("on 4401 with a refused renewal it stops for good (the app shows the login)", async () => {
    const statuses: LiveStatus[] = [];
    connectLive({ url: "ws://x/v1/runs/r1/events", afterSeq: 0, onEvent: () => undefined, onStatus: (s) => statuses.push(s), socket: (url) => new FakeSocket(url), onUnauthorized: () => Promise.resolve("refused" as const) });
    FakeSocket.all[0]?.drop(4401);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(statuses.at(-1)).toBe("closed");
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("on 4401 with a renewal that failed for a temporary reason it retries with the normal pause", async () => {
    const statuses: LiveStatus[] = [];
    let answers: ("failed" | "renewed")[] = ["failed", "renewed"];
    connectLive({ url: "ws://x/v1/runs/r1/events", afterSeq: 0, onEvent: () => undefined, onStatus: (s) => statuses.push(s), socket: (url) => new FakeSocket(url), onUnauthorized: () => Promise.resolve(answers.shift() ?? "renewed") });
    FakeSocket.all[0]?.drop(4401);
    await vi.advanceTimersByTimeAsync(0);
    expect(statuses.at(-1)).not.toBe("closed");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(FakeSocket.all).toHaveLength(2);
    answers = [];
  });

  it("a server that keeps answering 4401 after a renewal is not retried forever", async () => {
    const statuses: LiveStatus[] = [];
    connectLive({ url: "ws://x/v1/runs/r1/events", afterSeq: 0, onEvent: () => undefined, onStatus: (s) => statuses.push(s), socket: (url) => new FakeSocket(url), onUnauthorized: () => Promise.resolve("renewed" as const) });
    for (let i = 0; i < 6; i += 1) {
      FakeSocket.all.at(-1)?.drop(4401);
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(statuses.at(-1)).toBe("closed");
    expect(FakeSocket.all.length).toBeLessThanOrEqual(3);
  });

  it("without an onUnauthorized hook 4401 is final too", async () => {
    const { statuses, socket } = start();
    socket().drop(4401);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(statuses.at(-1)).toBe("closed");
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("gives up for good when the run doesn't exist (4404) or the server closes with policy 1008", () => {
    for (const code of [4404, 1008]) {
      FakeSocket.all = [];
      const { statuses, socket } = start();
      socket().drop(code);
      vi.advanceTimersByTime(60_000);
      expect(statuses.at(-1)).toBe("closed");
      expect(FakeSocket.all).toHaveLength(1);
    }
  });

  it("a server that accepts and then closes with 1013 is not 'live', and the pause keeps growing", () => {
    const { statuses, socket } = start();
    for (const pause of [1_000, 2_000, 4_000]) {
      socket().open(); // accepted, then closed at once: nothing was ever received
      socket().drop(1013);
      vi.advanceTimersByTime(pause - 1);
      expect(FakeSocket.all.length).toBe(statuses.filter((x) => x === "reconnecting").length);
      vi.advanceTimersByTime(1);
    }
    expect(statuses).not.toContain("live");
    expect(FakeSocket.all).toHaveLength(4);
  });

  it("after five attempts in a row that receive nothing it says unreachable and waits the longest pause", () => {
    const { statuses, socket } = start();
    for (let i = 0; i < 5; i += 1) {
      socket().drop(1006); // what a browser reports when the server refuses the origin
      vi.advanceTimersByTime(15_000);
    }
    expect(statuses.at(-1)).toBe("unreachable");
    expect(statuses).not.toContain("live");
    const count = FakeSocket.all.length;
    vi.advanceTimersByTime(14_999);
    expect(FakeSocket.all).toHaveLength(count); // no hammering
    socket().open();
    socket().send({ type: "caught_up", cursor: {} });
    expect(statuses.at(-1)).toBe("live"); // and it recovers as soon as the server answers
  });

  it("retries when the server is full (1013)", () => {
    const { socket } = start();
    socket().drop(1013);
    vi.advanceTimersByTime(1_000);
    expect(FakeSocket.all).toHaveLength(2);
    expect(socket().url).toContain("after_seq=0");
  });

  it("stops cleanly: closes the socket and never reconnects", () => {
    const { stop, socket, statuses } = start();
    const first = socket();
    first.open();
    first.send({ type: "caught_up", cursor: {} });
    stop();
    expect(first.closed).toBe(true);
    first.drop();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(statuses).toEqual(["connecting", "live"]);
  });

  it("stopping while waiting to reconnect cancels the retry", () => {
    const { stop, socket } = start();
    socket().drop();
    stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("a constructor that throws is retried", () => {
    let calls = 0;
    const statuses: LiveStatus[] = [];
    connectLive({
      url: "ws://x",
      afterSeq: 0,
      onEvent: () => undefined,
      onStatus: (s) => statuses.push(s),
      socket: (url) => {
        calls += 1;
        if (calls === 1) throw new Error("blocked");
        return new FakeSocket(url);
      },
    });
    expect(statuses).toEqual(["connecting", "reconnecting"]);
    vi.advanceTimersByTime(1_000);
    expect(FakeSocket.all).toHaveLength(1);
  });
});
