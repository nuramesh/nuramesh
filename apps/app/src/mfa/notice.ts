/**
 * The lost-codes notice flag (spec/mfa-web W-23): the one thing the MFA screens may put in browser storage.
 * A fixed key and the value `1`: no user, no challenge, no code. `sessionStorage` only, and every access is
 * guarded (a private window or blocked storage must not break the screen).
 */
export const CODES_UNSAVED_KEY = "nm.mfa.codes-unsaved";

export function setCodesUnsaved(): void {
  try {
    globalThis.sessionStorage.setItem(CODES_UNSAVED_KEY, "1");
  } catch {
    // storage blocked: the beforeunload prompt, the route blocker and the S3 text still apply
  }
}

export function clearCodesUnsaved(): void {
  try {
    globalThis.sessionStorage.removeItem(CODES_UNSAVED_KEY);
  } catch {
    // nothing to clear
  }
}

export function codesUnsaved(): boolean {
  try {
    return globalThis.sessionStorage.getItem(CODES_UNSAVED_KEY) === "1";
  } catch {
    return false;
  }
}
