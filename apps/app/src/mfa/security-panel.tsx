import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import type { MfaEnrollment, MfaStatus } from "@/auth/mfa-types";
import { useCountdown } from "@/auth/use-countdown";
import { useAuth, useAuthState } from "@/auth/use-session";
import { hasMfa, type AuthApi, type MfaAuth } from "@/auth/types";
import { Button } from "@/components/ui/button";

import {
  CODES_LOST_TEXT,
  describeMfaFailure,
  type MfaCall,
  type MfaFailure,
  UNKNOWN_REGEN_TEXT,
  UNKNOWN_TEXT,
  UNSAFE_TEXT,
  totpOf,
} from "./messages";
import { clearCodesUnsaved, codesUnsaved, setCodesUnsaved } from "./notice";
import { type FormMode, PasswordCodeForm } from "./password-code-form";
import { RecoveryCodes } from "./recovery-codes";
import { SetupBody } from "./setup-body";

type Flow =
  | { k: "idle" }
  | { k: "form"; mode: FormMode }
  | { k: "s4"; enrollment: MfaEnrollment }
  | { k: "s5"; codes: string[]; note?: string }
  /** The secrets were dropped (pagehide, bfcache, a timer, sign-out); `committed` says whether setup had succeeded. */
  | { k: "cleared"; committed: boolean };

/** S4 is dropped after this long with the tab hidden. S5 never is. */
const HIDDEN_CLEAR_MS = 5 * 60 * 1000;
/** After this many wrong passwords in a row on one page view the password form is shut for a minute (W-25). */
const SSO_STRIKES = 3;
const SSO_LOCK_SECONDS = 60;

/** Settings > Security (S3, S4, S5): set up, regenerate recovery codes and turn off two-step sign-in. */
export function SecurityPanel() {
  const auth: AuthApi | undefined = useAuth();
  if (!hasMfa(auth)) return null;
  return <Panel auth={auth} />;
}

function Panel({ auth }: { auth: AuthApi & MfaAuth }) {
  const state = useAuthState();
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [rawFlow, setFlow] = useState<Flow>({ k: "idle" });
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [wait, setWait] = useState<{ from: number; seconds: number } | null>(null);
  const [lock, setLock] = useState<{ from: number; seconds: number } | null>(null);
  const strikes = useRef(0);
  const [unsafeSeen, setUnsafeSeen] = useState(false);
  const [lostNotice, setLostNotice] = useState(codesUnsaved());
  const waitLeft = useCountdown(wait);
  const lockLeft = useCountdown(lock);

  const reload = useCallback(async (): Promise<MfaStatus | null> => {
    try {
      const next = await auth.mfaStatus();
      setStatus(next);
      setLoadFailed(false);
      return next;
    } catch {
      setLoadFailed(true);
      return null;
    }
  }, [auth]);

  useEffect(() => {
    let live = true;
    auth.mfaStatus().then(
      (next) => { if (live) setStatus(next); },
      () => { if (live) setLoadFailed(true); },
    );
    return () => { live = false; };
  }, [auth]);

  const signedOutHere = state.kind === "signed-out" && state.reason === "logged-out";
  const flow: Flow = signedOutHere ? { k: "idle" } : rawFlow;

  // AU-6: while S4 or S5 is on the screen, nothing another tab does may unmount it.
  const showing = flow.k === "s4" || flow.k === "s5";
  useEffect(() => {
    if (!showing) return;
    return auth.hold();
  }, [showing, auth]);

  // W-6: drop the secrets on pagehide (committed synchronously, so the back/forward cache holds none), on pageshow
  // from that cache, and S4 only after five minutes hidden.
  useEffect(() => {
    const drop = (): void => {
      flushSync(() => {
        setFlow((current) =>
          current.k === "s4" ? { k: "cleared", committed: false } : current.k === "s5" ? { k: "cleared", committed: true } : current,
        );
      });
    };
    const onShow = (event: PageTransitionEvent): void => {
      if (event.persisted) drop();
    };
    globalThis.addEventListener("pagehide", drop);
    globalThis.addEventListener("pageshow", onShow);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onVisibility = (): void => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      if (document.visibilityState !== "hidden") return;
      timer = setTimeout(() => {
        setFlow((current) => (current.k === "s4" ? { k: "cleared", committed: false } : current));
      }, HIDDEN_CLEAR_MS);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      globalThis.removeEventListener("pagehide", drop);
      globalThis.removeEventListener("pageshow", onShow);
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer !== undefined) clearTimeout(timer);
    };
  }, []);

  // Sign-out in this tab empties everything and clears the notice (W-6, W-23).
  useEffect(() => {
    if (signedOutHere) clearCodesUnsaved();
  }, [signedOutHere]);

  const clearFields = (): void => {
    setPassword("");
    setCode("");
  };
  const restart = (mode: FormMode | null): void => {
    clearFields();
    setFlow(mode === null ? { k: "idle" } : { k: "form", mode });
  };

  /** W-21: an expired access token. Renew, resend nothing (a code may already be spent). */
  const renewSession = async (): Promise<void> => {
    const result = await auth.recover();
    if (result === "renewed") {
      clearFields();
      setInfo("Your session was renewed. Submit again.");
      await reload();
    } else if (result === "refused") {
      setError("Your session ended. Sign in again to carry on.");
    } else {
      setError("The server could not renew your session. Try again in a moment.");
    }
  };

  /** W-20: the server may have committed; ask it, then say one fixed thing. */
  const resolveUnknown = async (call: MfaCall): Promise<void> => {
    setCodesUnsaved();
    setLostNotice(true);
    if (call === "regenerate") {
      setFlow({ k: "idle" });
      setError(UNKNOWN_REGEN_TEXT);
      return;
    }
    const next = await reload();
    setFlow({ k: "idle" });
    if (next === null) {
      setError(UNKNOWN_TEXT);
    } else if (next.enrolled) {
      setError(CODES_LOST_TEXT);
    } else {
      clearCodesUnsaved();
      setLostNotice(false);
      setError("Setup may not have finished. Start again.");
    }
  };

  const fail = async (raised: unknown, call: MfaCall, formMode: FormMode | null): Promise<void> => {
    const failure: MfaFailure = describeMfaFailure(raised, call);
    switch (failure.effect) {
      case "show":
        setError(failure.text);
        if (call !== "confirm") setPassword("");
        if (call === "enroll" && failure.text.startsWith("That password")) {
          strikes.current += 1;
          if (strikes.current >= SSO_STRIKES) {
            strikes.current = 0;
            setLock({ from: Date.now(), seconds: SSO_LOCK_SECONDS });
          }
        }
        return;
      case "restart":
        restart(formMode ?? "enrol");
        setError(failure.text);
        return;
      case "refresh":
        restart(null);
        setError(failure.text);
        await reload();
        return;
      case "wait":
        setError(failure.text);
        if (failure.seconds !== undefined) setWait({ from: Date.now(), seconds: failure.seconds });
        return;
      case "session":
        await renewSession();
        return;
      case "unknown":
        await resolveUnknown(call);
        return;
      case "unsafe":
        // The server acted but the answer cannot be trusted or shown: drop it, say so, tell the person to regenerate.
        setCodesUnsaved();
        setLostNotice(true);
        setUnsafeSeen(true);
        restart(call === "enroll" ? "enrol" : null);
        setError(UNSAFE_TEXT);
        return;
    }
  };

  /** Single flight: one request at a time per panel; the secret calls are never made from an effect (W-19). */
  const run = async (task: () => Promise<void>): Promise<void> => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      await task();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const submitForm = (mode: FormMode): Promise<void> =>
    run(async () => {
      const typed = totpOf(code);
      try {
        if (mode === "enrol") {
          const enrollment = await auth.mfaEnroll(password);
          strikes.current = 0;
          setPassword("");
          setFlow({ k: "s4", enrollment });
        } else if (typed === null) {
          return;
        } else if (mode === "regenerate") {
          const codes = await auth.mfaRegenerate({ password, code: typed });
          setCodesUnsaved();
          setLostNotice(true);
          clearFields();
          setFlow({ k: "s5", codes });
        } else {
          await auth.mfaDisable({ password, code: typed });
          clearFields();
          clearCodesUnsaved();
          setLostNotice(false);
          setFlow({ k: "idle" });
          setInfo("MFA is off. Other devices were signed out.");
          await reload();
        }
      } catch (raised) {
        await fail(raised, mode === "enrol" ? "enroll" : mode === "regenerate" ? "regenerate" : "disable", mode);
      }
    });

  const confirm = (typed: string, enrollment: MfaEnrollment): Promise<void> =>
    run(async () => {
      try {
        const codes = await auth.mfaConfirm({ factorId: enrollment.factorId, code: typed });
        setCodesUnsaved(); // before S5 renders (W-23)
        setLostNotice(true);
        setFlow({ k: "s5", codes, note: "Other devices were signed out." });
        void reload();
      } catch (raised) {
        await fail(raised, "confirm", null);
      }
    });

  const finish = (): void => {
    clearCodesUnsaved();
    setLostNotice(false);
    setFlow({ k: "idle" });
    void reload();
  };

  const start = (mode: FormMode): void => {
    clearFields();
    setError(null);
    setInfo(null);
    setFlow({ k: "form", mode });
  };

  const form = flow.k === "form" ? flow.mode : null;

  return (
    <section aria-labelledby="mfa-security-title" className="mt-8 flex max-w-md flex-col gap-3" data-testid="mfa-security">
      <h2 id="mfa-security-title" className="text-lg font-semibold">Security</h2>
      <h3 className="text-sm font-semibold">Two-step sign-in</h3>
      {status === null && !loadFailed && <p role="status" className="text-sm text-muted-foreground">Checking…</p>}
      {loadFailed && status === null && <p role="alert" className="text-sm text-destructive">Could not read your two-step sign-in status. Reload the page.</p>}

      {status !== null && flow.k === "idle" && <StatusBody status={status} lost={lostNotice && !signedOutHere} unsafeSeen={unsafeSeen} onStart={start} />}

      {form !== null && (
        <PasswordCodeForm
          mode={form}
          password={password}
          code={code}
          busy={busy}
          error={error}
          waitLeft={waitLeft}
          lockLeft={lockLeft}
          onPassword={setPassword}
          onCode={setCode}
          onSubmit={() => { void submitForm(form); }}
          onCancel={() => { restart(null); setError(null); }}
        />
      )}
      {flow.k === "s4" && (
        <SetupBody enrollment={flow.enrollment} busy={busy} error={error} waitLeft={waitLeft} onConfirm={(typed) => { void confirm(typed, flow.enrollment); }} />
      )}
      {flow.k === "s5" && <RecoveryCodes codes={flow.codes} {...(flow.note === undefined ? {} : { note: flow.note })} onContinue={finish} />}
      {flow.k === "cleared" && (
        <div role="status" className="flex flex-col gap-2 text-sm" data-testid="mfa-cleared">
          {flow.committed ? (
            <p>{CODES_LOST_TEXT}</p>
          ) : (
            <>
              <p>Setup was interrupted. Start again.</p>
              <Button className="min-h-11 self-start" onClick={() => { start("enrol"); }}>Start again</Button>
            </>
          )}
          {flow.committed && <Button className="min-h-11 self-start" onClick={finish}>Continue</Button>}
        </div>
      )}

      {flow.k !== "form" && flow.k !== "s4" && error !== null && <p role="alert" className="text-sm text-destructive" data-testid="mfa-error">{error}</p>}
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground" data-testid="mfa-info">{info ?? ""}</p>
    </section>
  );
}

function StatusBody({
  status,
  lost,
  unsafeSeen,
  onStart,
}: {
  status: MfaStatus;
  lost: boolean;
  unsafeSeen: boolean;
  onStart: (mode: FormMode) => void;
}) {
  if (!status.available && !status.enrolled) {
    return <p className="text-sm text-muted-foreground">Not set up on this server. Ask the operator.</p>;
  }
  if (!status.available) {
    return (
      <div className="flex flex-col gap-2 text-sm">
        <p>MFA is on for your account. Codes can't be checked on this server right now; your recovery codes still work. Ask the operator.</p>
        <Remaining count={status.recoveryCodesRemaining} />
      </div>
    );
  }
  if (!status.enrolled) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm text-muted-foreground">Off. Add a second step to your sign-in with an authenticator app.</p>
        <Button className="min-h-11 self-start" onClick={() => { onStart("enrol"); }}>Set up</Button>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm">Two-step sign-in is on.</p>
      {lost && (
        <p role="status" className="callout-warning px-3 py-2 text-sm" data-testid="mfa-lost-notice">{CODES_LOST_TEXT}</p>
      )}
      <Remaining count={status.recoveryCodesRemaining} />
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" className="min-h-11" disabled={unsafeSeen} onClick={() => { onStart("regenerate"); }}>
          Regenerate recovery codes
        </Button>
        <Button variant="outline" className="min-h-11" disabled={status.required} onClick={() => { onStart("disable"); }}>
          Turn off
        </Button>
      </div>
      {unsafeSeen && <p className="text-[13px] text-muted-foreground">{UNSAFE_TEXT}</p>}
      {status.required && <p className="text-[13px] text-muted-foreground">Your organization requires it for owners.</p>}
    </div>
  );
}

function Remaining({ count }: { count: number }) {
  return (
    <p className={count <= 3 ? "callout-warning px-3 py-2 text-sm" : "text-sm text-muted-foreground"} data-testid="mfa-remaining">
      Recovery codes left: {String(count)}
      {count <= 3 ? ". Make new ones before you run out." : ""}
    </p>
  );
}
