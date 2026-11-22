/**
 * Where to go after signing in: the page the person came from, if it is a page of this app.
 * Anything else (another site, a protocol-relative `//host`, the sign-in pages themselves, a
 * backslash trick) falls back to `/`.
 */
export function safeNextPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  if (Array.from(value).some((ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f)) return "/";
  const path = value.split(/[?#]/)[0] ?? "";
  if (path === "/login" || path === "/setup") return "/";
  return value;
}
