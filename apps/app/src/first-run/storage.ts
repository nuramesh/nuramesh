/**
 * The one thing the checklist stores (the first-run checklist notes FR-11): a browser-local preference per
 * (organization, person). Never server state, never personal data. Every read and write is inside try/catch.
 */
export interface FirstRunPrefs {
  dismissed: boolean;
  invite_skipped: boolean;
}

export const DEFAULT_PREFS: FirstRunPrefs = {
  dismissed: false,
  invite_skipped: false,
};

const MAX_BYTES = 256;
const KEYS = ["v", "dismissed", "invite_skipped"];

/** A length-safe key: a JSON pair, so an id holding ":" cannot collide with another pair (C41). */
export function firstRunKey(orgId: string, userId: string): string {
  return `nuramesh.firstRun.v1:${JSON.stringify([orgId, userId])}`;
}

function parse(raw: string | null): FirstRunPrefs {
  if (raw === null || raw.length > MAX_BYTES) return { ...DEFAULT_PREFS };
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null || Array.isArray(value))
      return { ...DEFAULT_PREFS };
    const rec = value as Record<string, unknown>;
    if (Object.keys(rec).some((k) => !KEYS.includes(k)))
      return { ...DEFAULT_PREFS };
    if (
      rec.v !== 1 ||
      typeof rec.dismissed !== "boolean" ||
      typeof rec.invite_skipped !== "boolean"
    ) {
      return { ...DEFAULT_PREFS };
    }
    return { dismissed: rec.dismissed, invite_skipped: rec.invite_skipped };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function readPrefs(orgId: string, userId: string): FirstRunPrefs {
  try {
    return parse(localStorage.getItem(firstRunKey(orgId, userId)));
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function writePrefs(
  orgId: string,
  userId: string,
  prefs: FirstRunPrefs,
): void {
  try {
    const value = {
      v: 1,
      dismissed: prefs.dismissed,
      invite_skipped: prefs.invite_skipped,
    };
    localStorage.setItem(firstRunKey(orgId, userId), JSON.stringify(value));
  } catch {
    // Blocked storage: the choice lasts until reload (C25).
  }
}
