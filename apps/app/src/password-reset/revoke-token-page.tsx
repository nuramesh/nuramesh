import { useEffect, useRef, useState } from "react";

import { ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { AuthCard } from "@/auth/auth-card";

import { revokeLinkToken } from "./fragment-token";
import { LinkUnusable } from "./link-unusable";

type Stage = "working" | "done" | "invalid" | "unreachable";

/** Revokes one API key from the link in the "your password was changed" email, on open. */
export function RevokeTokenPage() {
  const api = useApi();
  const [token] = useState(() => revokeLinkToken.take());
  const [stage, setStage] = useState<Stage>(token === undefined ? "invalid" : "working");
  const started = useRef(false);

  useEffect(() => {
    if (token === undefined || started.current) return;
    started.current = true;
    api.passwordReset.revokeByLink(token).then(
      () => {
        revokeLinkToken.forget();
        setStage("done");
      },
      (error: unknown) => {
        revokeLinkToken.forget();
        setStage(error instanceof ProblemError && error.status === 403 ? "invalid" : "unreachable");
      },
    );
  }, [api, token]);

  if (stage === "invalid") return <LinkUnusable />;
  if (stage === "done") {
    return (
      <AuthCard title="That key was revoked" intro="It can no longer be used. You can make a new key after signing in.">
        <title>Key revoked · NuraMesh</title>
      </AuthCard>
    );
  }
  if (stage === "unreachable") {
    return (
      <AuthCard title="Can't revoke the key">
        <title>Key · NuraMesh</title>
        <p role="alert" className="text-sm text-destructive">
          The server could not be reached. Open the link in the email again to try again.
        </p>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Revoking the key">
      <title>Key · NuraMesh</title>
      <p role="status" className="text-center text-sm text-muted-foreground">
        One moment…
      </p>
    </AuthCard>
  );
}
