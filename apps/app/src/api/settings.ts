/**
 * The two things the app remembers about where its data comes from. Both are plain, non-secret
 * strings kept in this browser only: which source to use, and the address of the server. Nothing
 * else (no token, no key) is ever stored in the browser; real sign-in is a later release.
 */
export type DataSource = "fixtures" | "server";

const SOURCE_KEY = "nuramesh.source";
const URL_KEY = "nuramesh.serverUrl";

function read(key: string): string | null {
  try {
    return globalThis.localStorage.getItem(key);
  } catch {
    return null; // private window, blocked site data
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null || value === "") globalThis.localStorage.removeItem(key);
    else globalThis.localStorage.setItem(key, value);
  } catch {
    // The choice then lasts only until the page is closed; nothing else is lost.
  }
}

export function storedSource(): DataSource | null {
  const value = read(SOURCE_KEY);
  return value === "fixtures" || value === "server" ? value : null;
}

export function storedServerUrl(): string {
  return read(URL_KEY) ?? "";
}

export function saveSource(source: DataSource | null): void {
  write(SOURCE_KEY, source);
}

export function saveServerUrl(url: string): void {
  write(URL_KEY, normalizeServerUrl(url));
}

/** "" (this site) or an http(s) origin without a trailing slash or path. Anything else is "". */
export function normalizeServerUrl(input: string): string {
  const text = input.trim();
  if (text === "") return "";
  try {
    const url = new URL(text);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    return url.origin;
  } catch {
    return "";
  }
}

/** Why a server address can't be used from this page, or null. A different origin needs the server to allow it. */
export function crossOriginNote(url: string, here: string = globalThis.location.origin): string | null {
  const normalized = normalizeServerUrl(url);
  if (normalized === "" || normalized === here) return null;
  return "This is a different address than this page. The server only answers requests that come from its own address (it refuses cross-site requests), so the browser will usually be refused. Leave this empty and let the dev server forward /v1 instead (docs/app.md).";
}
