/** Identifier helpers shared by the builder's pure edits. */

const SLUG_MAX = 63;

/** `base` if free, otherwise `base-2`, `base-3`, … (kept a valid slug of at most 63 chars). */
export function uniqueId(base: string, taken: ReadonlySet<string>): string {
  let clean = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[-_]+/, "");
  if (clean === "") clean = "agent";
  clean = clean.slice(0, SLUG_MAX);
  if (!taken.has(clean)) return clean;
  for (let n = 2; ; n += 1) {
    const suffix = `-${String(n)}`;
    const candidate = `${clean.slice(0, SLUG_MAX - suffix.length)}${suffix}`;
    if (!taken.has(candidate)) return candidate;
  }
}
