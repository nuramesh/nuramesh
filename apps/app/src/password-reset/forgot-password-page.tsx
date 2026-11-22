import { type SyntheticEvent, useState } from "react";
import { Link } from "react-router";

import { ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { AuthCard, Field } from "@/auth/auth-card";
import { describeLoginFailure, waitWords } from "@/auth/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Asks for a reset link. The answer is the same sentence for every address (spec A-P1); the address is never echoed. */
export function ForgotPasswordPage() {
  const api = useApi();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || email.trim() === "") return;
    setBusy(true);
    setProblem(null);
    api.passwordReset.request(email.trim()).then(
      () => {
        setEmail("");
        setSent(true);
      },
      (error: unknown) => {
        setBusy(false);
        if (error instanceof ProblemError && error.status === 429) {
          const failure = describeLoginFailure(error);
          setProblem(failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`);
        } else {
          setProblem(error instanceof Error ? error.message : "Something went wrong.");
        }
      },
    );
  };

  const footer = (
    <Link to="/login" className="underline underline-offset-2">
      Back to sign in
    </Link>
  );

  if (sent) {
    return (
      <AuthCard title="Check your email" footer={footer}>
        <title>Forgot password · NuraMesh</title>
        <p role="status" className="text-center text-sm" data-testid="forgot-sent">
          If that address has an account, a message is on its way.
        </p>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Forgot your password?" intro="Enter your email and we will send you a link to set a new one." footer={footer}>
      <title>Forgot password · NuraMesh</title>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <Field id="forgot-email" label="Email">
          <Input
            id="forgot-email"
            name="email"
            type="email"
            className="h-11"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <div role="alert" className="empty:hidden">
          {problem !== null && (
            <p className="text-sm text-destructive" data-testid="forgot-failure">
              {problem}
            </p>
          )}
        </div>
        <Button type="submit" className="min-h-11 w-full" disabled={busy || email.trim() === ""}>
          {busy ? "Sending…" : "Send the link"}
        </Button>
      </form>
    </AuthCard>
  );
}
