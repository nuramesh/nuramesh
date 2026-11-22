import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import { useAuth, useAuthState } from "@/auth/use-session";
import { hasMfa } from "@/auth/types";

import { codesUnsaved } from "./notice";

/**
 * The lost-codes notice on the first page of a signed-in view (spec/mfa-web W-23): shown while the non-secret flag is
 * set and the server says MFA is on, until the person goes to another page. (The flag is dropped when the signed-in
 * person changes or signs out by `AuthGate`, which stays mounted while the state is being re-read.)
 */
export function CodesUnsavedNotice() {
  const auth = useAuth();
  const state = useAuthState();
  const location = useLocation();
  const userId = state.kind === "signed-in" ? state.user.id : null;
  const [firstPath] = useState(location.pathname);
  const [enrolled, setEnrolled] = useState(false);

  useEffect(() => {
    if (userId === null || !codesUnsaved() || !hasMfa(auth)) return;
    let live = true;
    auth.mfaStatus().then(
      (status) => { if (live) setEnrolled(status.enrolled); },
      () => undefined, // not known: the line is never shown
    );
    return () => { live = false; };
  }, [auth, userId]);

  const here = location.pathname;
  if (userId === null || !enrolled || !codesUnsaved() || here !== firstPath || here === "/settings") return null;
  return (
    <p role="status" className="callout-warning mx-4 mt-3 px-3 py-2 text-sm" data-testid="mfa-lost-notice-page">
      MFA is on. Your recovery codes were not saved: regenerate them in{" "}
      <Link to="/settings" className="underline">Settings &gt; Security</Link>.
    </p>
  );
}
