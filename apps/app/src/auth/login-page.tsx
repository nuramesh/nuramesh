import { Eye, EyeOff } from "lucide-react";
import { type SyntheticEvent, useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Link, Navigate, useLocation } from "react-router";

import { ProblemError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoginEnrol } from "@/mfa/login-enrol";
import { LOGIN_UNSAFE_TEXT, NO_APP_CODE_TEXT, recoveryOf, TIMEOUT_TEXT } from "@/mfa/login-messages";
import { totpOf } from "@/mfa/messages";
import { clearRecoverySignIn, markRecoverySignIn } from "@/mfa/recovery-signin";

import { AuthCard, Field } from "./auth-card";
import { describeLoginFailure, describeMfaFailure, MFA_ENROLL_MESSAGE, waitWords } from "./messages";
import { safeNextPath } from "./next-path";
import { useCountdown } from "./use-countdown";
import { hasMfa } from "./types";
import { useAuth, useAuthHeld, useAuthState, useLoadSession } from "./use-session";

interface LoginLocationState {
  from?: unknown;
  setupDone?: boolean;
  inviteDone?: boolean;
  resetDone?: boolean;
}

/** The sign-in screen. Shown for any page when nobody is signed in; afterwards the person lands where they were going. */
export function LoginPage() {
  const auth = useAuth();
  const state = useAuthState();
  const location = useLocation();
  const arrived = location.state as LoginLocationState | null;
  const [target, setTarget] = useState<string | null>(null);
  const next = target ?? safeNextPath(arrived?.from);
  const held = useAuthHeld();
  // The enrolment challenge lives here, in memory, and nowhere else.
  const [enrol, setEnrol] = useState<{ challenge: string; expiresIn: number } | null>(null);
  useLoadSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [ownerMissing, setOwnerMissing] = useState(false);
  const [mailOn, setMailOn] = useState(false); // unknown counts as off: the administrator text shows first
  const [step, setStep] = useState<{ challenge: string; expiresIn: number } | null>(null);
  const [useRecovery, setUseRecovery] = useState(false);
  const [code, setCode] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const [wait, setWait] = useState<{ from: number; seconds: number } | null>(null);
  const left = useCountdown(wait);
  const locked = left > 0;
  const codeRef = useRef<HTMLInputElement>(null);
  const recoveryLink = useRef<HTMLButtonElement>(null);
  const focusRecovery = useRef(false);
  const inMfaStep = step !== null;
  const busyRef = useRef(false);
  const lapsed = useRef(false);
  const dropStep = useCallback((message?: string): void => {
    setStep(null);
    setCode("");
    setWait(null);
    setPassword("");
    setFailure(message ?? null);
  }, []);

  // AU-5: the challenge lives `expiresIn` seconds. A timer that fires while a request is on its way lets the answer decide.
  const expiresIn = step?.expiresIn;
  useEffect(() => {
    if (expiresIn === undefined) return;
    const timer = setTimeout(() => {
      if (busyRef.current) lapsed.current = true;
      else dropStep(TIMEOUT_TEXT);
    }, expiresIn * 1000);
    return () => {
      clearTimeout(timer);
    };
  }, [expiresIn, step?.challenge, dropStep]);

  // W-6: the challenge is dropped on pagehide (committed synchronously, so the back/forward cache holds the password step,
  // never a code step with a live challenge); pageshow with `persisted` is the backstop.
  useEffect(() => {
    if (!inMfaStep) return;
    const drop = (): void => {
      flushSync(() => {
        dropStep();
      });
    };
    const onShow = (event: PageTransitionEvent): void => {
      if (event.persisted) drop();
    };
    globalThis.addEventListener("pagehide", drop);
    globalThis.addEventListener("pageshow", onShow);
    return () => {
      globalThis.removeEventListener("pagehide", drop);
      globalThis.removeEventListener("pageshow", onShow);
    };
  }, [inMfaStep, dropStep]);

  // Keyboard flow on the second step: focus the code field when the step appears, when the field switches between the
  // authenticator and recovery code, and again once a check ends (the Verify button was disabled while busy, so focus fell away).
  useEffect(() => {
    if (!inMfaStep || busy) return;
    if (focusRecovery.current) {
      focusRecovery.current = false;
      recoveryLink.current?.focus();
    } else codeRef.current?.focus();
  }, [inMfaStep, useRecovery, busy]);

  // Whether to offer "Create the owner account": only while the server says no owner has ever existed.
  const signedOutNow = state.kind === "signed-out" || state.kind === "unavailable";
  useEffect(() => {
    if (auth === undefined || !signedOutNow) return;
    let current = true;
    void auth.setupStatus().then((missing) => {
      if (current) setOwnerMissing(missing);
    });
    void auth.mailEnabled?.().then(
      (on) => {
        if (current) setMailOn(on);
      },
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [auth, signedOutNow]);

  // Signed in already (or nothing to sign in to): go on to the page. While a screen holds secrets this
  // page stays: the real state is acted on when it lets go.
  if (auth === undefined || ((state.kind === "signed-in" || state.kind === "open") && !held)) {
    return <Navigate to={next} replace />;
  }

  if (state.kind === "checking" && !held) {
    return (
      <p role="status" className="p-8 text-sm text-muted-foreground">
        Checking your session…
      </p>
    );
  }

  const notice =
    state.kind === "signed-out" && state.reason === "expired"
      ? "Your session ended. Sign in again to carry on."
      : state.kind === "signed-out" && state.reason === "logged-out"
        ? "You are signed out."
        : arrived?.setupDone === true
          ? "The owner account is ready. Sign in with it."
          : arrived?.inviteDone === true
            ? "Your account is ready. Sign in with it."
            : arrived?.resetDone === true
              ? "Your password was changed. Sign in with the new one."
              : null;

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || locked) return;
    setBusy(true);
    setFailure(null);
    auth.login(email.trim(), password).then(
      (next) => {
        setPassword("");
        setWait(null);
        if (next.kind === "mfa") {
          setStep({ challenge: next.challenge, expiresIn: next.expiresIn });
          lapsed.current = false;
          setCode("");
          setUseRecovery(false);
          setBusy(false);
        } else if (next.kind === "mfa-enroll") {
          // A well-formed challenge and the multi-factor routes: the person sets the second step up here.
          if (next.challenge !== undefined && next.expiresIn !== undefined && hasMfa(auth)) {
            setEnrol({ challenge: next.challenge, expiresIn: next.expiresIn });
            setShowPassword(false);
          } else {
            setFailure(MFA_ENROLL_MESSAGE);
          }
          setBusy(false);
        }
        // Signed in: the state change above makes this page redirect (one navigation, not two).
      },
      (error: unknown) => {
        const problem = describeLoginFailure(error);
        setPassword("");
        setShowPassword(false);
        setFailure(problem.message);
        setWait(problem.waitSeconds === undefined ? null : { from: Date.now(), seconds: problem.waitSeconds });
        setBusy(false);
      },
    );
  };

  const parsedCode = useRecovery ? recoveryOf(code) : totpOf(code);
  const submitCode = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busyRef.current || locked || step === null || parsedCode === null) return;
    busyRef.current = true;
    setBusy(true);
    setFailure(null);
    const answer = useRecovery ? { recoveryCode: parsedCode } : { code: parsedCode };
    // W-4: remembered before the request (the sign-in changes the state and the next page reads it); cleared on a failure.
    if (useRecovery) markRecoverySignIn();
    auth.completeMfa(step.challenge, answer).then(
      () => {
        busyRef.current = false;
        setCode("");
      },
      (error: unknown) => {
        busyRef.current = false;
        clearRecoverySignIn();
        const problem = describeMfaFailure(error);
        setCode("");
        const unsafe = error instanceof ProblemError && error.title === "unsafe_answer";
        // An unsafe answer is handled as unsafe even after the timer lapsed (AU-5: the in-flight answer decides).
        if (problem.startOver || (lapsed.current && !unsafe)) {
          dropStep(problem.startOver ? problem.message : TIMEOUT_TEXT);
          setBusy(false);
          return;
        }
        if (error instanceof ProblemError && error.status === 503 && error.title === "mfa_unavailable" && !useRecovery) {
          // An app code cannot be checked right now; recovery codes still work.
          setFailure(NO_APP_CODE_TEXT);
          focusRecovery.current = true;
        } else if (error instanceof ProblemError && error.title === "unsafe_answer") {
          // The server acted (a session cookie may be set) but its answer cannot be trusted: ask it who is signed in.
          setFailure(LOGIN_UNSAFE_TEXT);
          void auth.load();
        } else {
          setFailure(problem.message);
        }
        setWait(problem.waitSeconds === undefined ? null : { from: Date.now(), seconds: problem.waitSeconds });
        setBusy(false);
      },
    );
  };

  const backToPassword = (message?: string): void => {
    setPassword("");
    setEnrol(null);
    setFailure(message ?? null);
  };

  if (enrol !== null && hasMfa(auth)) {
    return (
      <AuthCard title="Set up a second step">
        <title>Sign in · NuraMesh</title>
        <LoginEnrol auth={auth} challenge={enrol.challenge} expiresIn={enrol.expiresIn} onBack={backToPassword} onTarget={setTarget} />
      </AuthCard>
    );
  }

  if (step !== null) {
    const label = useRecovery ? "Recovery code" : "Code from your authenticator app";
    return (
      <AuthCard
        title="Enter your code"
        intro={useRecovery ? "Use one of the recovery codes you saved. Each works once." : "Open your authenticator app and type the 6-digit code."}
      >
        <title>Second step · NuraMesh</title>
        <form onSubmit={submitCode} className="flex flex-col gap-4" noValidate data-testid="mfa-code-step">
          <Field id="login-code" label={label}>
            <Input
              id="login-code"
              ref={codeRef}
              name="code"
              className="h-11"
              autoComplete={useRecovery ? "off" : "one-time-code"}
              maxLength={useRecovery ? 40 : undefined}
              disabled={locked}
              inputMode={useRecovery ? "text" : "numeric"}
              autoCapitalize="none"
              spellCheck={false}
              required
              value={code}
              onChange={(e) => {
                setCode(e.target.value);
              }}
            />
          </Field>
          <div role="alert" className="empty:hidden">
            {failure !== null && (
              <p className="text-sm text-destructive" data-testid="login-failure">
                {failure}
              </p>
            )}
          </div>
          {wait !== null && (
            <p className="text-sm text-muted-foreground" data-testid="login-wait">
              {locked ? `Try again in ${waitWords(left)}.` : "You can try again now."}
            </p>
          )}
          <Button type="submit" className="min-h-11 w-full" disabled={busy || locked || parsedCode === null}>
            {busy ? "Verifying…" : "Verify"}
          </Button>
          <Button
            ref={recoveryLink}
            type="button"
            variant="ghost"
            className="min-h-11"
            onClick={() => {
              setUseRecovery((on) => !on);
              setCode("");
              setFailure(null);
            }}
          >
            {useRecovery ? "Use the authenticator app instead" : "Use a recovery code instead"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            className="min-h-11"
            onClick={() => {
              setStep(null);
              setCode("");
              setFailure(null);
              setWait(null);
            }}
          >
            Back to sign in
          </Button>
        </form>
        <p className="text-center text-[13px] text-muted-foreground" data-testid="login-mfa-lost">
          Lost your phone and your recovery codes? Ask an owner to reset your MFA. If you are the only owner, ask whoever runs the server.
        </p>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Sign in to NuraMesh"
      intro={notice ?? "Use the account your administrator gave you."}
      footer={
        ownerMissing ? (
          <>
            First time on a new server? <Link to="/setup" className="underline underline-offset-2">Create the owner account</Link> with a setup token.
          </>
        ) : undefined
      }
    >
      <title>Sign in · NuraMesh</title>
      {state.kind === "unavailable" && (
        <div role="alert" className="callout-warning flex flex-col gap-2 px-3 py-2 text-sm">
          <span>{state.message}</span>
          <Button
            variant="outline"
            className="min-h-11 self-start"
            onClick={() => {
              void auth.load();
            }}
          >
            Try again
          </Button>
        </div>
      )}
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <Field id="login-email" label="Email">
          <Input
            id="login-email"
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
        <Field id="login-password" label="Password">
          <div className="relative">
            <Input
              id="login-password"
              name="password"
              type={showPassword ? "text" : "password"}
              className="h-11 w-full pr-12"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
              }}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="absolute top-0 right-0 size-11"
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              onClick={() => {
                setShowPassword((shown) => !shown);
              }}
            >
              {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
            </Button>
          </div>
        </Field>
        <div role="alert" className="empty:hidden">
          {failure !== null && (
            <p className="text-sm text-destructive" data-testid="login-failure">
              {failure}
            </p>
          )}
        </div>
        {wait !== null && (
          <p className="text-sm text-muted-foreground" data-testid="login-wait">
            {locked ? `Try again in ${waitWords(left)}.` : "You can try again now."}
          </p>
        )}
        <Button type="submit" className="min-h-11 w-full" disabled={busy || locked || email.trim() === "" || password === ""}>
          {busy ? "Signing in…" : locked ? `Wait ${waitWords(left)}` : "Sign in"}
        </Button>
        {mailOn ? (
          <p className="text-center text-[13px] text-muted-foreground">
            Forgot your password? <Link to="/forgot-password" className="underline underline-offset-2">Reset it by email</Link>
          </p>
        ) : (
          <p className="text-center text-[13px] text-muted-foreground">Forgot your password? Ask your administrator.</p>
        )}
      </form>
    </AuthCard>
  );
}
