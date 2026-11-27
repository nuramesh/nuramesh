import { NuraMeshError } from "./errors.js";
import { GeneratedApi } from "./generated.js";
import { fillPath } from "./path.js";
import type { RequestOptions } from "./transport.js";

export interface ClientOptions {
  /** Server root, for example `http://127.0.0.1:8765`. */
  baseUrl: string;
  /** Bearer token for API credentials; sessions use cookies (`credentials`). */
  token?: string;
  /** Headers sent with every call. */
  headers?: Record<string, string>;
  /** Fetch implementation (default: the global one). Tests pass a fake. */
  fetch?: typeof fetch;
  /** Passed to fetch: `include` sends session cookies cross-origin. */
  credentials?: RequestCredentials;
}

interface CallArgs {
  path?: object;
  query?: object;
  headers?: object;
  body?: unknown;
}

const plain = (value: unknown): string =>
  typeof value === "string"
    ? value
    : typeof value === "number" || typeof value === "boolean"
      ? String(value)
      : JSON.stringify(value);

/** Typed client for the NuraMesh REST API. Sends only the call you make: no telemetry. */
export class NuraMeshClient extends GeneratedApi {
  readonly #options: ClientOptions;

  constructor(options: ClientOptions) {
    super();
    this.#options = options;
  }

  async request<T>(
    method: string,
    path: string,
    args: CallArgs | undefined,
    options: RequestOptions | undefined,
  ): Promise<T> {
    const pathValues = (args?.path ?? {}) as Record<string, unknown>;
    const filled = fillPath(path, (name) => encodeURIComponent(plain(pathValues[name])));
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries((args?.query ?? {}) as Record<string, unknown>)) {
      if (v !== undefined && v !== null) params.append(k, plain(v));
    }
    const base = this.#options.baseUrl.replace(/\/+$/, "");
    const url = `${base}${filled}${params.size > 0 ? `?${params.toString()}` : ""}`;

    const headers: Record<string, string> = { accept: "application/json" };
    if (this.#options.token !== undefined) headers.authorization = `Bearer ${this.#options.token}`;
    Object.assign(headers, this.#options.headers);
    for (const [k, v] of Object.entries((args?.headers ?? {}) as Record<string, unknown>)) {
      if (v !== undefined && v !== null) headers[k] = plain(v);
    }
    Object.assign(headers, options?.headers);
    const init: RequestInit = { method, headers };
    if (args?.body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(args.body);
    }
    if (this.#options.credentials !== undefined) init.credentials = this.#options.credentials;
    if (options?.signal !== undefined) init.signal = options.signal;

    let response: Response;
    try {
      response = await (this.#options.fetch ?? fetch)(url, init);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "network failure";
      throw new NuraMeshError(0, "network_error", message, {}, { cause });
    }
    return (await parse(response)) as T;
  }
}

async function parse(response: Response): Promise<unknown> {
  const isJson = (response.headers.get("content-type") ?? "").includes("json");
  const text = await response.text();
  let body: unknown;
  if (text !== "" && isJson) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  if (response.ok) return body ?? (text === "" ? undefined : text);
  throw failure(response.status, body, text);
}

function failure(status: number, body: unknown, text: string): NuraMeshError {
  if (typeof body === "object" && body !== null) {
    const doc = body as Record<string, unknown>;
    if (
      typeof doc.title === "string" &&
      typeof doc.type === "string" &&
      doc.type.startsWith("urn:nuramesh:")
    ) {
      const { title, detail, ...rest } = doc;
      const extra = Object.fromEntries(
        Object.entries(rest).filter(([k]) => k !== "type" && k !== "status"),
      );
      return new NuraMeshError(status, title, typeof detail === "string" ? detail : "", extra);
    }
    if (status === 422 && "detail" in doc) {
      return new NuraMeshError(status, "invalid_request", "the request was not valid", {
        errors: doc.detail,
      });
    }
  }
  return new NuraMeshError(status, "unknown_error", text.slice(0, 200));
}
