import { type SyntheticEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { InvalidRequestError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { INVITE_MIN_PASSWORD, type InviteInspection } from "@/api/invites";
import { AuthCard, Field } from "@/auth/auth-card";
import { describeLoginFailure, waitWords } from "@/auth/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { forgetInviteToken, takeToken } from "./invite-token";

type Stage =
  | { kind: "loading" }
  | { kind: "invalid" }
  | { kind: "unreachable"; message: string }
  | { kind: "ready"; invite: InviteInspection };

/** Accepts an invitation: shows who it is for, asks for a name and a password, creates the account, goes to sign-in. */
export function AcceptInvitePage() {
  const api = useApi();
  const navigate = useNavigate();
  // The fragment is cleared in the first render's initializer, before the effect makes any request.
  const [token] = useState(takeToken);
  const [stage, setStage] = useState<Stage>(token === undefined ? { kind: "invalid" } : { kind: "loading" });
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (token === undefined) return;
    let live = true;
    api.invites.inspect(token).then(
      (invite) => {
        if (live) setStage({ kind: "ready", invite });
      },
      (error: unknown) => {
        if (!live) return;
        if (error instanceof ProblemError && error.status === 403) {
          forgetInviteToken();
          setStage({ kind: "invalid" });
        } else {
          setStage({ kind: "unreachable", message: error instanceof Error ? error.message : "The server could not be reached." });
        }
      },
    );
    return () => {
      live = false;
    };
  }, [api, token]);

  const tooShort = password !== "" && Array.from(password.normalize("NFKC")).length < INVITE_MIN_PASSWORD;
  const mismatch = again !== "" && again !== password;
  const ready = password !== "" && !tooShort && again === password;

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || !ready || token === undefined) return;
    setBusy(true);
    setProblem(null);
    api.invites.accept({ token, password, displayName: name }).then(
      () => {
        forgetInviteToken();
        setPassword("");
        setAgain("");
        void navigate("/login", { replace: true, state: { inviteDone: true } });
      },
      (error: unknown) => {
        setBusy(false);
        if (error instanceof ProblemError && error.status === 403) {
          forgetInviteToken();
          setStage({ kind: "invalid" });
        } else if (error instanceof ProblemError && error.status === 429) {
          const failure = describeLoginFailure(error);
          setProblem(failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`);
        } else if (error instanceof ProblemError && error.status === 409) {
          setProblem("A user with this address already exists, so this invitation can't create another.");
        } else if (error instanceof InvalidRequestError) {
          setProblem(`The server refused these details. The password needs at least ${String(INVITE_MIN_PASSWORD)} characters.`);
        } else {
          setProblem(error instanceof Error ? error.message : "Something went wrong.");
        }
      },
    );
  };

  if (stage.kind === "loading") {
    return (
      <AuthCard title="Accept your invitation">
        <title>Invitation · NuraMesh</title>
        <p role="status" className="text-center text-sm text-muted-foreground">
          Checking your invitation…
        </p>
      </AuthCard>
    );
  }
  if (stage.kind === "invalid") {
    return (
      <AuthCard
        title="This invitation can't be used"
        intro="It may be wrong, already used, expired or withdrawn. Ask the person who invited you for a new one."
        footer={
          <>
            Already have an account? <Link to="/login" className="underline underline-offset-2">Sign in</Link>.
          </>
        }
      >
        <title>Invitation · NuraMesh</title>
      </AuthCard>
    );
  }
  if (stage.kind === "unreachable") {
    return (
      <AuthCard title="Can't check your invitation">
        <title>Invitation · NuraMesh</title>
        <p role="alert" className="text-sm text-destructive">
          {stage.message}
        </p>
        <p className="text-center text-[13px] text-muted-foreground">Reload this page to try again. Your link still works.</p>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Accept your invitation"
      intro={
        <>
          Join <strong>{stage.invite.organization}</strong> as <strong>{stage.invite.role}</strong>.
        </>
      }
    >
      <title>Invitation · NuraMesh</title>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">Email</span>
          <p className="min-h-11 rounded-md border bg-muted/40 px-3 py-2.5 text-sm" data-testid="invite-email">
            {stage.invite.email}
          </p>
        </div>
        <Field id="invite-name" label="Name (optional)">
          <Input
            id="invite-name"
            name="name"
            className="h-11"
            autoComplete="name"
            maxLength={200}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
          />
        </Field>
        <Field id="invite-password" label="Password" hint={`At least ${String(INVITE_MIN_PASSWORD)} characters. A long phrase is best.`}>
          <Input
            id="invite-password"
            name="new-password"
            type="password"
            className="h-11"
            autoComplete="new-password"
            required
            aria-describedby="invite-password-hint"
            aria-invalid={tooShort ? true : undefined}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        {tooShort && (
          <p role="alert" className="-mt-2 text-[13px] text-destructive">
            That is shorter than {INVITE_MIN_PASSWORD} characters.
          </p>
        )}
        <Field id="invite-again" label="Password again">
          <Input
            id="invite-again"
            name="new-password-again"
            type="password"
            className="h-11"
            autoComplete="new-password"
            required
            aria-invalid={mismatch ? true : undefined}
            value={again}
            onChange={(e) => {
              setAgain(e.target.value);
            }}
          />
        </Field>
        {mismatch && (
          <p role="alert" className="-mt-2 text-[13px] text-destructive">
            The two passwords are not the same.
          </p>
        )}
        <div role="alert" className="empty:hidden">
          {problem !== null && (
            <p className="text-sm text-destructive" data-testid="invite-failure">
              {problem}
            </p>
          )}
        </div>
        <Button type="submit" className="min-h-11 w-full" disabled={busy || !ready}>
          {busy ? "Creating…" : "Create my account"}
        </Button>
      </form>
    </AuthCard>
  );
}
