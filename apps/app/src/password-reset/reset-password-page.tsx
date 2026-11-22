import { type SyntheticEvent, useState } from "react";
import { useNavigate } from "react-router";

import { InvalidRequestError, ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { RESET_MIN_PASSWORD } from "@/api/password-reset";
import { AuthCard, Field } from "@/auth/auth-card";
import { describeLoginFailure, waitWords } from "@/auth/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { resetLinkToken } from "./fragment-token";
import { LinkUnusable } from "./link-unusable";

/** Sets a new password from a single-use link. The token is cleared from the address in the first render. */
export function ResetPasswordPage() {
  const api = useApi();
  const navigate = useNavigate();
  const [token] = useState(() => resetLinkToken.take());
  const [invalid, setInvalid] = useState(token === undefined);
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const tooShort = password !== "" && Array.from(password.normalize("NFKC")).length < RESET_MIN_PASSWORD;
  const mismatch = again !== "" && again !== password;
  const ready = password !== "" && !tooShort && again === password;

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || !ready || token === undefined) return;
    setBusy(true);
    setProblem(null);
    api.passwordReset.redeem({ token, password }).then(
      () => {
        resetLinkToken.forget();
        setPassword("");
        setAgain("");
        void navigate("/login", { replace: true, state: { resetDone: true } });
      },
      (error: unknown) => {
        setBusy(false);
        if (error instanceof ProblemError && error.status === 403) {
          resetLinkToken.forget();
          setPassword("");
          setAgain("");
          setInvalid(true);
        } else if (error instanceof ProblemError && error.status === 429) {
          const failure = describeLoginFailure(error);
          setProblem(failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`);
        } else if (error instanceof InvalidRequestError) {
          setProblem(`The server refused this password. It needs at least ${String(RESET_MIN_PASSWORD)} characters.`);
        } else {
          setProblem(error instanceof Error ? error.message : "Something went wrong.");
        }
      },
    );
  };

  if (invalid) return <LinkUnusable />;

  return (
    <AuthCard title="Set a new password" intro="Signing in again is needed everywhere after this.">
      <title>Reset password · NuraMesh</title>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <Field id="reset-password" label="New password" hint={`At least ${String(RESET_MIN_PASSWORD)} characters. A long phrase is best.`}>
          <Input
            id="reset-password"
            name="new-password"
            type="password"
            className="h-11"
            autoComplete="new-password"
            required
            aria-describedby="reset-password-hint"
            aria-invalid={tooShort ? true : undefined}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        {tooShort && (
          <p role="alert" className="-mt-2 text-[13px] text-destructive">
            That is shorter than {RESET_MIN_PASSWORD} characters.
          </p>
        )}
        <Field id="reset-again" label="New password again">
          <Input
            id="reset-again"
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
            <p className="text-sm text-destructive" data-testid="reset-failure">
              {problem}
            </p>
          )}
        </div>
        <Button type="submit" className="min-h-11 w-full" disabled={busy || !ready}>
          {busy ? "Saving…" : "Set the new password"}
        </Button>
      </form>
    </AuthCard>
  );
}
