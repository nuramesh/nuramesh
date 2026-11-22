import { type SyntheticEvent, useEffect, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { AuthCard, Field } from "./auth-card";
import { describeSetupFailure, tokenFromFragment, waitWords } from "./messages";
import { useAuth, useAuthState, useLoadSession } from "./use-session";

/** The shortest password the server accepts for a new account (spec/auth/README.md section 1). */
export const MIN_PASSWORD_LENGTH = 12;

/** First-owner setup: redeem the one-time token printed by `nuramesh serve` (a link) or `nuramesh auth new-setup-token`. */
export function SetupPage() {
  const auth = useAuth();
  const state = useAuthState();
  const navigate = useNavigate();
  useLoadSession();
  const [token, setToken] = useState(() => tokenFromFragment(window.location.hash) ?? "");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // SA-0.10: remove the fragment from the address bar and the history entry at once. The token was read by the
  // state initialiser above (strict mode runs initialisers twice before any effect, so both see the fragment).
  useEffect(() => {
    if (window.location.hash !== "") {
      window.history.replaceState(window.history.state as unknown, "", window.location.pathname + window.location.search);
    }
  }, []);

  if (auth === undefined || state.kind === "signed-in" || state.kind === "open") return <Navigate to="/" replace />;

  const tooShort = password !== "" && Array.from(password.normalize("NFKC")).length < MIN_PASSWORD_LENGTH;
  const mismatch = again !== "" && again !== password;
  const ready = token.trim() !== "" && email.trim() !== "" && password !== "" && !tooShort && again === password;

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setProblem(null);
    auth.redeemSetup({ token: token.trim(), email: email.trim(), displayName: name, password }).then(
      () => {
        setPassword("");
        setAgain("");
        setToken("");
        void navigate("/login", { replace: true, state: { setupDone: true } });
      },
      (error: unknown) => {
        const failure = describeSetupFailure(error);
        setProblem(
          failure.waitSeconds === undefined ? failure.message : `${failure.message} Try again in ${waitWords(failure.waitSeconds)}.`,
        );
        setBusy(false);
      },
    );
  };

  return (
    <AuthCard
      title="Create the owner account"
      intro="One time, on a new server. Open the link that nuramesh serve printed, or run nuramesh auth new-setup-token on the server and paste what it prints."
      footer={
        <>
          Already have an account? <Link to="/login" className="underline underline-offset-2">Sign in</Link>.
        </>
      }
    >
      <title>Set up · NuraMesh</title>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <Field id="setup-token" label="Setup token" hint="It works once: 15 minutes (from serve), or 24 hours (from new-setup-token).">
          <Input
            id="setup-token"
            name="token"
            className="h-11 font-mono"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            required
            aria-describedby="setup-token-hint"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
            }}
          />
        </Field>
        <Field id="setup-email" label="Email">
          <Input
            id="setup-email"
            name="email"
            type="email"
            className="h-11"
            autoComplete="username"
            inputMode="email"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <Field id="setup-name" label="Name (optional)">
          <Input
            id="setup-name"
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
        <Field id="setup-password" label="Password" hint={`At least ${String(MIN_PASSWORD_LENGTH)} characters. A long phrase is best.`}>
          <Input
            id="setup-password"
            name="new-password"
            type="password"
            className="h-11"
            autoComplete="new-password"
            required
            aria-describedby="setup-password-hint"
            aria-invalid={tooShort ? true : undefined}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        {tooShort && (
          <p role="alert" className="-mt-2 text-[13px] text-destructive">
            That is shorter than {MIN_PASSWORD_LENGTH} characters.
          </p>
        )}
        <Field id="setup-again" label="Password again">
          <Input
            id="setup-again"
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
            <p className="text-sm text-destructive" data-testid="setup-failure">
              {problem}
            </p>
          )}
        </div>
        <Button type="submit" className="min-h-11 w-full" disabled={busy || !ready}>
          {busy ? "Creating…" : "Create owner account"}
        </Button>
      </form>
    </AuthCard>
  );
}
