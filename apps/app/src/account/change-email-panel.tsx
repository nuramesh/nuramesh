import { type SyntheticEvent, useState } from "react";

import { ProblemError } from "@/api/client";
import { useApi } from "@/api/context";
import { describeLoginFailure, waitWords } from "@/auth/messages";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Account settings: ask to change the sign-in email. Every accepted request ends on the same "check your new inbox". */
export function ChangeEmailPanel() {
  const api = useApi();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || email.trim() === "" || password === "") return;
    setBusy(true);
    setProblem(null);
    api.emailChange.start({ newEmail: email.trim(), password }).then(
      () => {
        setBusy(false);
        setPassword("");
        setEmail("");
        setSent(true);
      },
      (error: unknown) => {
        setBusy(false);
        setSent(false);
        if (error instanceof ProblemError && error.status === 401) setProblem("The password is not right.");
        else if (error instanceof ProblemError && error.status === 409) setProblem("That is already your email address.");
        else if (error instanceof ProblemError && error.status === 429) {
          const failure = describeLoginFailure(error);
          setProblem(failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`);
        } else setProblem(error instanceof Error ? error.message : "Something went wrong.");
      },
    );
  };

  return (
    <section aria-labelledby="change-email-title" className="mt-8 flex max-w-md flex-col gap-3">
      <h2 id="change-email-title" className="text-lg font-semibold">Change email</h2>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <label htmlFor="new-email" className="text-sm font-medium">New email address</label>
        <Input id="new-email" name="email" type="email" className="h-11" autoComplete="email" value={email} onChange={(e) => { setEmail(e.target.value); }} />
        <label htmlFor="email-password" className="text-sm font-medium">Current password</label>
        <Input id="email-password" name="current-password" type="password" className="h-11" autoComplete="current-password" value={password} onChange={(e) => { setPassword(e.target.value); }} />
        <div role="alert" className="empty:hidden">
          {problem !== null && <p className="text-sm text-destructive" data-testid="email-change-failure">{problem}</p>}
        </div>
        <Button type="submit" className="min-h-11" disabled={busy || email.trim() === "" || password === ""}>
          {busy ? "Sending…" : "Send confirmation"}
        </Button>
      </form>
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground" data-testid="email-change-sent">
        {sent ? "Check your new inbox. If the address can be used, we sent a link to it that lasts 24 hours." : ""}
      </p>
    </section>
  );
}
