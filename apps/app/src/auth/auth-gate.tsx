import { useQueryClient } from "@tanstack/react-query";
import { Fragment, type ReactNode, useEffect, useRef } from "react";
import { Navigate, useLocation } from "react-router";

import { Button } from "@/components/ui/button";
import { CodesUnsavedNotice } from "@/mfa/codes-unsaved-notice";
import { clearCodesUnsaved } from "@/mfa/notice";
import { RecoverySignInNotice } from "@/mfa/recovery-signin-notice";

import { useAuth, useAuthHeld, useAuthState, useLoadSession } from "./use-session";

/**
 * Wraps the app shell, so every page that needs a person. Where the data source has no sign-in it lets everything
 * through; otherwise nobody sees a page until the server says who they are. When the session ends
 * (a refresh that fails, Sign out), what was fetched is dropped and the person is sent to /login,
 * to come back to the same page afterwards.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const state = useAuthState();
  const held = useAuthHeld();
  const location = useLocation();
  const queries = useQueryClient();
  useLoadSession();
  const signedOut = state.kind === "signed-out";
  const userId = state.kind === "signed-in" ? state.user.id : null;
  const lastUser = useRef<string | null>(null);
  useEffect(() => {
    // Whenever the person shown changes, or nobody is shown (another tab signed in or out), drop what was fetched.
    if (lastUser.current !== null && lastUser.current !== userId) queries.clear();
    lastUser.current = userId;
  }, [userId, queries]);
  const lastKnown = useRef<string | null>(null);
  useEffect(() => {
    // The lost-codes flag never reaches the next person: a sign-out, or another person, drops it.
    if (signedOut || (userId !== null && lastKnown.current !== null && lastKnown.current !== userId)) clearCodesUnsaved();
    if (userId !== null) lastKnown.current = userId;
  }, [signedOut, userId]);
  useEffect(() => {
    if (signedOut) queries.clear(); // the next person must not see this one's data
  }, [signedOut, queries]);

  // A screen is showing secrets: it stays mounted until it lets go; the real state is acted on then.
  // (the page sits in a keyed Fragment in BOTH returns, so it is matched by key, not by position: adding a notice never
  // remounts it when a hold starts or ends)
  const page = <Fragment key="page">{children}</Fragment>;
  if (held) return <>{page}</>;
  if (state.kind === "checking") {
    return (
      <p role="status" className="p-8 text-sm text-muted-foreground">
        Checking your session…
      </p>
    );
  }
  if (state.kind === "unavailable") {
    return (
      <div role="alert" className="mx-auto flex max-w-md flex-col gap-3 p-8">
        <h1 className="text-xl font-semibold">Can't check your sign-in</h1>
        <p className="text-sm text-muted-foreground">{state.message}</p>
        <Button
          className="min-h-11 self-start"
          onClick={() => {
            void auth?.load();
          }}
        >
          Try again
        </Button>
      </div>
    );
  }
  if (state.kind === "signed-out") {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}${location.hash}` }} />;
  }
  return (
    <>
      {state.kind === "signed-in" && <CodesUnsavedNotice />}
      {state.kind === "signed-in" && <RecoverySignInNotice />}
      {page}
    </>
  );
}
