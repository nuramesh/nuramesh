import type { Unsubscribe } from "../client";
import type { LiveStatus, RunEvent } from "../types";
import type { RawEvent } from "./raw";
import { runEvent } from "./map";

/** The slice of the browser's WebSocket this uses (so tests can pass a fake). */
export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(code?: number): void;
}
export type SocketFactory = (url: string) => SocketLike;

/** Close codes after which retrying is pointless: no such run (4404), policy (1008, e.g. a foreign origin). */
const FINAL = new Set([4404, 1008, 1003]);
/** Renew-and-reconnect rounds in a row that got no frame before 4401 counts as final. */
const MAX_AUTH_ROUNDS = 2;
const MAX_DELAY_MS = 15_000;
/** Attempts in a row that never received a single frame before the connection counts as unreachable. */
const UNREACHABLE_AFTER = 5;

export interface LiveOptions {
  url: string;
  afterSeq: number;
  onEvent: (event: RunEvent) => void;
  onStatus?: (status: LiveStatus) => void;
  socket: SocketFactory;
  /** Called on close code 4401 (credential stopped working): `renewed` = reconnect at once, `refused` = the session is over (stop), `failed` = temporary (retry with the normal pause). */
  onUnauthorized?: () => Promise<"renewed" | "refused" | "failed">;
}

/**
 * The per-run live view: `WS /v1/runs/{id}/events?after_seq=N`. The server first replays every
 * entry after N, then sends new ones. When the connection drops it reconnects with growing
 * pauses and asks again from the last entry it saw, so nothing is missed and nothing repeats.
 */
export function connectLive(options: LiveOptions): Unsubscribe {
  let cursor = options.afterSeq;
  let attempt = 0;
  let failures = 0; // consecutive attempts that received nothing
  let live = false;
  let authRounds = 0; // 4401 renewals since the last frame
  let closed = false;
  let socket: SocketLike | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const status = (s: LiveStatus): void => options.onStatus?.(s);

  const open = (): void => {
    if (closed) return;
    let ws: SocketLike;
    try {
      ws = options.socket(`${options.url}?after_seq=${String(cursor)}`);
    } catch {
      failures += 1;
      retry();
      return;
    }
    socket = ws;
    // A connection counts as working only once the server has sent something (the replay or
    // `caught_up`): a server can accept the socket and close it straight away (1013 "full"), and a
    // browser reports a refused origin as a plain 1006 close, so "open" alone proves nothing.
    ws.onopen = () => undefined;
    ws.onmessage = (message) => {
      if (typeof message.data !== "string") return;
      if (!live) {
        live = true;
        attempt = 0;
        failures = 0;
        authRounds = 0;
        status("live");
      }
      let frame: { event?: RawEvent; type?: string };
      try {
        frame = JSON.parse(message.data) as { event?: RawEvent; type?: string };
      } catch {
        return;
      }
      const raw = frame.event;
      if (raw === undefined || raw.seq <= cursor) return; // a repeat after a reconnect
      cursor = raw.seq;
      options.onEvent(runEvent(raw));
    };
    ws.onclose = (ev) => {
      if (closed || socket !== ws) return;
      if (!live) failures += 1;
      live = false;
      if (ev.code === 4401) {
        // The session ran out. Renew it once for everyone (shared with the REST calls) and ask again from the cursor.
        authRounds += 1;
        const renew = options.onUnauthorized;
        if (renew === undefined || authRounds > MAX_AUTH_ROUNDS) {
          closed = true;
          status("closed");
          return;
        }
        status("reconnecting");
        void renew().then(
          (result) => {
            if (closed) return;
            if (result === "renewed") open();
            else if (result === "failed") {
              authRounds -= 1; // a temporary fault is not the server refusing a renewed session
              retry();
            } else {
              closed = true;
              status("closed");
            }
          },
          () => {
            if (closed) return;
            authRounds -= 1;
            retry();
          },
        );
        return;
      }
      if (FINAL.has(ev.code)) {
        closed = true;
        status("closed");
        return;
      }
      retry();
    };
    ws.onerror = () => undefined; // the close event follows and decides what to do
  };

  const retry = (): void => {
    if (closed) return;
    const unreachable = failures >= UNREACHABLE_AFTER;
    status(unreachable ? "unreachable" : "reconnecting");
    const delay = unreachable ? MAX_DELAY_MS : Math.min(MAX_DELAY_MS, 1_000 * 2 ** attempt);
    attempt += 1;
    timer = setTimeout(open, delay);
  };

  status("connecting");
  open();
  return () => {
    closed = true;
    if (timer !== undefined) clearTimeout(timer);
    const current = socket;
    socket = null;
    if (current !== null) {
      current.onclose = null;
      current.close();
    }
  };
}
