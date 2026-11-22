/**
 * Browser persistence: the user's saved templates and the current draft. Storage can be missing,
 * full or blocked (privacy modes); every access is guarded, and stored data is re-validated on
 * read like any import, so a corrupted entry is skipped instead of breaking the app.
 */
import { type BuilderDocument, parseDocument } from "./document";
import type { Template } from "./templates";

export const TEMPLATES_KEY = "nuramesh-user-templates";
export const DRAFT_KEY = "nuramesh-draft";

const LEGACY_PURGED_KEY = "nuramesh-legacy-unscoped-purged";

/**
 * Deletes the drafts and templates saved before they were filed per organization and person (T127): the unscoped
 * keys, once per browser. Called only against a server with accounts; sample-data mode keeps using the unscoped keys,
 * so it never calls this. Returns true when it ran now, false when it had run before or storage is unavailable.
 */
export function purgeLegacyUnscopedStorage(): boolean {
  try {
    if (window.localStorage.getItem(LEGACY_PURGED_KEY) !== null) return false;
    window.localStorage.removeItem(DRAFT_KEY);
    window.localStorage.removeItem(TEMPLATES_KEY);
    window.localStorage.setItem(LEGACY_PURGED_KEY, "1");
    return true;
  } catch {
    return false;
  }
}

/** The key for a person's data: the plain key with no sign-in, else one per organization and person. */
function keyFor(key: string, scope: string | undefined): string {
  return scope === undefined ? key : `${key}:${scope}`;
}

function read(key: string): unknown {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? null : (JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}

/** Writes JSON; returns false when storage is unavailable or full. */
function write(key: string, value: unknown): boolean {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/** Saved user templates; invalid entries are skipped. */
export function loadUserTemplates(scope?: string): Template[] {
  const raw = read(keyFor(TEMPLATES_KEY, scope));
  if (!Array.isArray(raw)) return [];
  const templates: Template[] = [];
  for (const item of raw as unknown[]) {
    if (typeof item !== "object" || item === null) continue;
    const { id, name, description, document } = item as Record<string, unknown>;
    if (typeof id !== "string" || typeof name !== "string") continue;
    const parsed = parseDocument(document);
    if (!parsed.ok) continue;
    templates.push({
      id,
      name,
      description: typeof description === "string" ? description : "",
      document: parsed.value,
    });
  }
  return templates;
}

/** Saves the list; returns false if the browser refused (the UI tells the user). */
export function saveUserTemplates(templates: readonly Template[], scope?: string): boolean {
  return write(keyFor(TEMPLATES_KEY, scope), templates);
}

export function loadDraft(scope?: string): BuilderDocument | null {
  const parsed = parseDocument(read(keyFor(DRAFT_KEY, scope)));
  return parsed.ok ? parsed.value : null;
}

export function saveDraft(doc: BuilderDocument, scope?: string): boolean {
  return write(keyFor(DRAFT_KEY, scope), doc);
}
