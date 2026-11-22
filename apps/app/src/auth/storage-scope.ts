import { useAuthState } from "./use-session";
import type { AuthState } from "./types";

/**
 * What the browser's saved drafts and templates are filed under: the organization and the person.
 * `undefined` for a source with no sign-in (sample data), which keeps the unscoped keys. A signed-in person never
 * loads the unscoped data, and nobody loads another person's (shared devices).
 */
export function storageScopeOf(state: AuthState): string | undefined {
  return state.kind === "signed-in" ? `${state.user.orgId}:${state.user.id}` : undefined;
}

/** The scope of whoever is signed in now. */
export function useStorageScope(): string | undefined {
  return storageScopeOf(useAuthState());
}
