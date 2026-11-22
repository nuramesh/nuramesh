import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import { useAuth } from "@/auth/use-session";
import { hasMfa } from "@/auth/types";

import { clearRecoverySignIn, recoverySignInPending } from "./recovery-signin";

/** W-4: "You signed in with a recovery code. N left." on the first page after such a sign-in, gone on navigation. */
export function RecoverySignInNotice() {
  const auth = useAuth();
  const location = useLocation();
  const [firstPath] = useState(location.pathname);
  const [armed] = useState(recoverySignInPending);
  const [left, setLeft] = useState<number | null>(null);

  useEffect(() => {
    clearRecoverySignIn(); // once: the flag does not outlive this first view
  }, []);
  useEffect(() => {
    if (!armed || !hasMfa(auth)) return;
    let live = true;
    auth.mfaStatus().then(
      (status) => { if (live) setLeft(status.recoveryCodesRemaining); },
      () => undefined, // not known: the line is not shown
    );
    return () => { live = false; };
  }, [armed, auth]);

  if (!armed || left === null || location.pathname !== firstPath || location.pathname === "/settings") return null;
  return (
    <p role="status" className="callout-warning mx-4 mt-3 px-3 py-2 text-sm" data-testid="mfa-recovery-signin-notice">
      You signed in with a recovery code. {String(left)} left.{" "}
      <Link to="/settings" className="underline">Settings &gt; Security</Link>
    </p>
  );
}
