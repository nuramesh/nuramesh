import { useEffect, useSyncExternalStore } from "react";

import { useApi } from "@/api/context";

import { purgeLegacyUnscopedStorage } from "@/builder/storage";

import type { AuthApi, AuthState } from "./types";

const OPEN: AuthState = { kind: "open" };
const NO_UNSUBSCRIBE = (): void => undefined;

/** The sign-in state of the current data source (`open` where there is none). */
export function useAuthState(): AuthState {
  const auth: AuthApi | undefined = useApi().auth;
  return useSyncExternalStore(
    (notify) => (auth === undefined ? NO_UNSUBSCRIBE : auth.subscribe(notify)),
    () => (auth === undefined ? OPEN : auth.state()),
  );
}

/** The sign-in API of the current data source, or undefined when it has none. */
export function useAuth(): AuthApi | undefined {
  return useApi().auth;
}

/** Asks the server who is signed in, once, when the state is still unknown. */
export function useLoadSession(): void {
  const auth = useAuth();
  useEffect(() => {
    if (auth?.state().kind === "checking") void auth.load();
  }, [auth]);
  const kind = useAuthState().kind;
  useEffect(() => {
    // A server with accounts: drafts and templates saved before the per-person keys (T127) are deleted, once.
    if (kind === "signed-in" || kind === "signed-out") purgeLegacyUnscopedStorage();
  }, [kind]);
}

/** True while a screen that shows secrets holds the session state still (`AuthApi.hold`); re-renders on every change. */
export function useAuthHeld(): boolean {
  const auth: AuthApi | undefined = useApi().auth;
  return useSyncExternalStore(
    (notify) => (auth === undefined ? NO_UNSUBSCRIBE : auth.subscribe(notify)),
    () => auth?.held?.() ?? false,
  );
}
