import type { ApiError} from "../client";
import { ConflictError, InvalidRequestError, NotAllowedError, NotFoundError, ProblemError } from "../client";

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** The 409 codes of the org routes (the organization safety notes section 11): the state already is what was asked for. */
const ORG_CONFLICTS = new Set([
  "already_halted",
  "already_running",
  "already_paused",
  "not_paused",
  "not_tripped",
  "already_disabled",
  "not_disabled",
  "breaker_tripped",
]);

interface Problem {
  title?: string;
  detail?: string;
  status?: number;
  problems?: unknown;
  retry_after_seconds?: unknown;
}

/** The `[{path, message}]` list a 422 and some 409s carry. */
function problemFields(body: Problem | null): { path: string; message: string }[] {
  if (!Array.isArray(body?.problems)) return [];
  return (body.problems as unknown[]).flatMap((p) => {
    const item = p as { path?: unknown; message?: unknown } | null;
    return typeof item?.path === "string" && typeof item.message === "string"
      ? [{ path: item.path, message: item.message }]
      : [];
  });
}

/** Turns an error response (application/problem+json) into the error class the screens branch on. */
export function problemError(status: number, body: Problem | null, path: string, retryAfter?: number): ApiError {
  const detail = body?.detail ?? `the server answered ${String(status)}`;
  const title = body?.title ?? "error";
  if (path.startsWith("/v1/org/")) {
    // Kill switch, role pause and breakers (E19): "not a person" is NotAllowed, "nothing to do" is Conflict.
    if (status === 403 && (title === "human_required" || title === "forbidden")) return new NotAllowedError(detail, title);
    if (status === 409 && ORG_CONFLICTS.has(title)) return new ConflictError(detail, title);
  }
  if (/^\/v1\/(agents\/[^/]+\/credentials|agent-credentials)/.test(path)) {
    // Agent credentials (E2): owner only, and issue or rotate is refused while halted or at the limit.
    if (status === 403 && (title === "human_required" || title === "forbidden")) return new NotAllowedError(detail, title);
    if (status === 409 && (title === "agent_suspended" || title === "credential_limit")) return new ConflictError(detail, title);
  }
  if (/^\/v1\/runs\/[^/]+\/tasks\/[^/]+\/audit$/.test(path) && status === 403 && (title === "human_required" || title === "forbidden")) {
    // The task audit view (R1.27): a caller without the read permission is not allowed, as in spec section 9.
    return new NotAllowedError(detail, title);
  }
  if (status === 404) return new NotFoundError(path);
  if (status === 422) return new InvalidRequestError(detail, problemFields(body));
  if (status === 409 && (title === "approval_refused" || title === "unblock_refused")) return new NotAllowedError(detail);
  const fromBody = typeof body?.retry_after_seconds === "number" ? body.retry_after_seconds : undefined;
  return new ProblemError(detail, status, title, retryAfter ?? fromBody);
}

/** `Retry-After` as whole seconds (the server sends seconds, never a date), or undefined. */
export function parseRetryAfter(value: string | null): number | undefined {
  if (value === null || !/^\d{1,9}$/.test(value.trim())) return undefined;
  return Number(value.trim());
}

/** What the sign-in layer adds to every call (the plain client has none). */
export interface HttpHooks {
  /** The CSRF token to send on writes, if there is a cookie session. */
  csrf?: () => string | undefined;
  /**
   * Called once when a call (other than to `/v1/auth/`) is answered 401; `renewed` means the session
   * was renewed, so the call is repeated one time.
   */
  recover?: () => Promise<"renewed" | "refused" | "failed">;
  /**
   * Called once when a write is answered 403 `csrf_invalid` (another tab rotated the token); `true`
   * means a newer token is now known, so the write is repeated one time.
   */
  refreshCsrf?: () => Promise<boolean>;
}

/** How long a call to `/v1/auth/` may take before it is given up (the lock must not be held for minutes). */
const AUTH_TIMEOUT_MS = 15_000;
/** How many unanswered keyed requests are remembered; past this the oldest is forgotten. */
export const MAX_PENDING_KEYS = 100;
/**
 * How long an unanswered key may be reused: the server's in-flight lease is about 120 s, and it
 * replays a stored 2xx for a day (core/idempotency.py). Past this window a repeat is a new user
 * action, so it gets a new key instead of replaying a stale answer (the idempotency notes section 1).
 */
export const PENDING_KEY_WINDOW_MS = 120_000;

type Method = "GET" | "POST" | "PUT" | "DELETE";

/** Per-call switches for the routes that return a secret. */
export interface CallOptions {
  /** A 403 `csrf_invalid` is not repeated (the first request may already have acted: a code was spent). */
  noCsrfRepeat?: boolean;
  /**
   * A 2xx answer is used only if its `Cache-Control` carries the `no-store` directive (token match, case
   * insensitive). Otherwise the body is dropped before any caller sees it and the call rejects with
   * `ProblemError` status 200, title `unsafe_answer`: the server has already acted.
   */
  requireNoStore?: boolean;
}

/** True when a `Cache-Control` value has the `no-store` directive (`no-store, private` yes; `no-cache`, `max-age=0` no). */
export function hasNoStore(value: string | null): boolean {
  return value?.split(",").some((token) => token.trim().toLowerCase() === "no-store") === true;
}

export interface HttpClient {
  readonly baseUrl: string;
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body: unknown, options?: CallOptions & WriteOptions): Promise<T>;
  put<T>(path: string, body: unknown, options?: WriteOptions): Promise<T>;
  /** A DELETE that carries a JSON body (a revoke needs its reason). */
  delete<T>(path: string, body: unknown, options?: WriteOptions): Promise<T>;
}

export interface WriteOptions {
  /**
   * Send an `Idempotency-Key`: one key per user action, reused on every repeat of the same request
   * until the server has answered it (a lost answer, a timeout or a 5xx keeps the key, so a second
   * click replays the first answer instead of running twice). Only for routes that honour a key:
   * the server answers 400 to a route that does not (the idempotency notes section 8), and the
   * kill switch never sends one (8.2).
   */
  keyed?: boolean;
}

/**
 * A small JSON client for the API. `baseUrl` is "" for the same origin (the dev server proxies
 * `/v1`, which is what the server's loopback checks need) or a full address such as
 * http://127.0.0.1:8000. A server that can't be reached rejects with `ProblemError` status 0.
 */
export function createHttp(baseUrl: string, fetchFn: FetchFn, hooks: HttpHooks = {}): HttpClient {
  const base = baseUrl.replace(/\/+$/, "");
  // Keys of requests the server has not answered yet. A request that keeps failing (5xx, no network)
  // would otherwise keep its entry for good, so the oldest are dropped past MAX_PENDING_KEYS.
  const pendingKeys = new Map<string, { key: string; at: number }>();
  async function once(method: Method, path: string, body?: unknown, key?: string): Promise<{ response: Response; parsed: unknown }> {
    const csrf = method !== "GET" ? hooks.csrf?.() : undefined;
    let response: Response;
    // Sign-in calls are cut off after a while, so a hanging one cannot hold the cross-tab lock for minutes.
    const timed = path.startsWith("/v1/auth/");
    const controller = timed ? new AbortController() : undefined;
    const timer = controller === undefined ? undefined : setTimeout(() => { controller.abort(); }, AUTH_TIMEOUT_MS);
    try {
      response = await fetchFn(`${base}${path}`, {
        method,
        headers: {
          accept: "application/json",
          ...(body === undefined ? {} : { "content-type": "application/json" }),
          ...(csrf === undefined || csrf === "" ? {} : { "x-csrf-token": csrf }),
          ...(key === undefined ? {} : { "idempotency-key": key }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        ...(controller === undefined ? {} : { signal: controller.signal }),
      });
    } catch {
      if (timer !== undefined) clearTimeout(timer);
      throw new ProblemError(
        `Couldn't reach the NuraMesh server${base === "" ? "" : ` at ${base}`}. Is "nuramesh serve" running?`,
        0,
        "unreachable",
      );
    }
    let parsed: unknown = null;
    try {
      parsed = await response.json();
    } catch {
      parsed = null;
    }
    if (timer !== undefined) clearTimeout(timer);
    return { response, parsed };
  }
  async function call<T>(method: Method, path: string, body?: unknown, options?: CallOptions & WriteOptions): Promise<T> {
    const id = `${method} ${path} ${JSON.stringify(body ?? null)}`;
    const now = Date.now();
    const known = pendingKeys.get(id);
    const fresh = known !== undefined && now - known.at <= PENDING_KEY_WINDOW_MS;
    const key = options?.keyed === true && method !== "GET" ? (fresh ? known.key : crypto.randomUUID()) : undefined;
    if (key !== undefined && !fresh) {
      pendingKeys.delete(id);
      pendingKeys.set(id, { key, at: now });
      while (pendingKeys.size > MAX_PENDING_KEYS) {
        const oldest = pendingKeys.keys().next();
        if (oldest.done === true) break;
        pendingKeys.delete(oldest.value);
      }
    }
    let result = await once(method, path, body, key);
    if (result.response.status === 401 && hooks.recover !== undefined && !path.startsWith("/v1/auth/")) {
      if ((await hooks.recover()) === "renewed") result = await once(method, path, body, key);
    }
    if (
      method !== "GET" &&
      result.response.status === 403 &&
      (result.parsed as Problem | null)?.title === "csrf_invalid" &&
      hooks.refreshCsrf !== undefined &&
      options?.noCsrfRepeat !== true
    ) {
      if (await hooks.refreshCsrf()) result = await once(method, path, body, key);
    }
    const { response, parsed } = result;
    // The key is kept while the answer is unknown (the request threw above), the server failed (5xx) or
    // the first attempt is still running (409 idempotency_in_progress: "try again in a moment").
    const inProgress = response.status === 409 && (parsed as Problem | null)?.title === "idempotency_in_progress";
    if (response.status < 500 && !inProgress) pendingKeys.delete(id);
    if (!response.ok) {
      throw problemError(response.status, parsed as Problem | null, path, parseRetryAfter(response.headers.get("retry-after")));
    }
    if (options?.requireNoStore === true && !hasNoStore(response.headers.get("cache-control"))) {
      throw new ProblemError("the server answered without a no-store header", 200, "unsafe_answer");
    }
    return parsed as T;
  }
  return {
    baseUrl: base,
    get: (path) => call("GET", path),
    post: (path, body, options) => call("POST", path, body, options),
    put: (path, body, options) => call("PUT", path, body, options),
    delete: (path, body, options) => call("DELETE", path, body, options),
  };
}
