import { useEffect, useState } from "react";
import { Link } from "react-router";

import { ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { AuthCard } from "@/auth/auth-card";
import { describeLoginFailure, waitWords } from "@/auth/messages";

import { forgetEmailToken, takeEmailToken } from "./email-token";

type Stage =
  | { kind: "working" }
  | { kind: "done" }
  | { kind: "invalid" }
  | { kind: "taken" }
  | { kind: "failed"; message: string };

/** Opens the link mailed to a new address: posts the token from the fragment once and says what happened. */
export function VerifyEmailPage() {
  const api = useApi();
  // The fragment is cleared in the first render's initializer, before the effect makes any request.
  const [token] = useState(takeEmailToken);
  const [stage, setStage] = useState<Stage>(token === undefined ? { kind: "invalid" } : { kind: "working" });

  useEffect(() => {
    if (token === undefined) return;
    let live = true;
    api.emailChange.verify(token).then(
      () => {
        forgetEmailToken();
        if (live) setStage({ kind: "done" });
      },
      (error: unknown) => {
        if (!live) return;
        if (error instanceof ProblemError && error.status === 403) {
          forgetEmailToken();
          setStage({ kind: "invalid" });
        } else if (error instanceof ProblemError && error.status === 409) {
          forgetEmailToken();
          setStage({ kind: "taken" });
        } else if (error instanceof ProblemError && error.status === 429) {
          const failure = describeLoginFailure(error);
          setStage({
            kind: "failed",
            message: failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`,
          });
        } else {
          setStage({ kind: "failed", message: error instanceof Error ? error.message : "The server could not be reached." });
        }
      },
    );
    return () => {
      live = false;
    };
  }, [api, token]);

  const signIn = (
    <>
      <Link to="/login" className="underline underline-offset-2">Sign in</Link>
    </>
  );
  if (stage.kind === "working") {
    return (
      <AuthCard title="Confirming your new email">
        <title>Confirm email · NuraMesh</title>
        <p role="status" className="text-center text-sm text-muted-foreground">Checking your link…</p>
      </AuthCard>
    );
  }
  if (stage.kind === "done") {
    return (
      <AuthCard title="Your email address is changed" intro="Use the new address the next time you sign in. Your password is the same." footer={signIn}>
        <title>Confirm email · NuraMesh</title>
      </AuthCard>
    );
  }
  if (stage.kind === "taken") {
    return (
      <AuthCard title="That address is no longer available" intro="Someone else now uses it. Your email address was not changed." footer={signIn}>
        <title>Confirm email · NuraMesh</title>
      </AuthCard>
    );
  }
  if (stage.kind === "failed") {
    return (
      <AuthCard title="Can't confirm your email">
        <title>Confirm email · NuraMesh</title>
        <p role="alert" className="text-sm text-destructive">{stage.message}</p>
        <p className="text-center text-[13px] text-muted-foreground">Reload this page to try again. Your link still works.</p>
      </AuthCard>
    );
  }
  return (
    <AuthCard
      title="This link can't be used"
      intro="It may be wrong, already used, expired or replaced by a newer request. Start the change again from your settings."
      footer={signIn}
    >
      <title>Confirm email · NuraMesh</title>
    </AuthCard>
  );
}
