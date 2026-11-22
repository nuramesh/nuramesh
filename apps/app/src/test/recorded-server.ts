import { vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { createServerClient } from "@/api/server-client";

import { answer } from "./contract";

export interface Sent {
  method: string;
  url: URL;
  body: unknown;
}

interface Reply {
  status: number;
  body: unknown;
}
type Route = (sent: Sent) => Reply;

/** One recorded answer of the real server (`scripts/app/gen_contract.py`), as a reply. */
export const recorded = (file: Parameters<typeof answer>[0], name: string): Reply => answer<unknown>(file, name);

/**
 * The real server client over a fake fetch that routes by "METHOD /path": what a screen does
 * against the server, with the server's recorded answers. A path with no route answers 404.
 */
export function recordedServer(routes: Record<string, Route>): { api: ApiClient; calls: Sent[] } {
  const calls: Sent[] = [];
  const fetchFn = vi.fn((input: string, init?: RequestInit) => {
    const url = new URL(input, "http://127.0.0.1:5173");
    const sent: Sent = { method: init?.method ?? "GET", url, body: init?.body === undefined ? undefined : (JSON.parse(init.body as string) as unknown) };
    calls.push(sent);
    const route = routes[`${sent.method} ${url.pathname}`];
    const { status, body } = route === undefined ? { status: 404, body: { title: "not_found", detail: "no route", status: 404 } } : route(sent);
    return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" } }));
  });
  const api = createServerClient({
    baseUrl: "",
    fetch: fetchFn,
    now: () => new Date("2026-10-25T12:00:00Z"),
    socket: () => {
      throw new Error("no sockets in this test");
    },
  });
  return { api, calls };
}
