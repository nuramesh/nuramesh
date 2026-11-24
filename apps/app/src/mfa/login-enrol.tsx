import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import type { MfaEnrollment } from "@/auth/mfa-types";
import type { AuthApi, MfaAuth } from "@/auth/types";
import { useCountdown } from "@/auth/use-countdown";
import { Button } from "@/components/ui/button";

import {
  describeStepFailure,
  LOGIN_UNSAFE_TEXT,
  SESSION_ENDED_TEXT,
  TIMEOUT_TEXT,
} from "./login-messages";
import { CODES_LOST_TEXT, UNKNOWN_TEXT } from "./messages";
import { clearCodesUnsaved, setCodesUnsaved } from "./notice";
import { RecoveryCodes } from "./recovery-codes";
import { SetupBody } from "./setup-body";

type Stage =
  | { k: "start" }
  | { k: "s4"; enrollment: MfaEnrollment }
  | { k: "s5"; codes: string[]; apply: () => Promise<void> }
  /** The secrets were dropped (pagehide, bfcache, five minutes hidden); `committed` says whether setup had succeeded. */
  | { k: "cleared"; committed: boolean; apply?: () => Promise<void> };

/** S4 is dropped after this long with the tab hidden. S5 never is. */
const HIDDEN_CLEAR_MS = 5 * 60 * 1000;
/** The server lets an enrolment challenge live at most this long (AU-1). */
const ENROL_CHALLENGE_MAX = 300;

/**
 * S2, then S4 and S5 inside the login page: an owner whose organization requires a second step and who has
 * none enrols before getting a session. The session cookie is set by the confirm, but `AuthState` stays signed out and no
 * other tab is told until "Continue" in S5 (AU-3); while S4 or S5 shows, the page holds the session state still (AU-6).
 */
export function LoginEnrol({
  auth,
  challenge,
  expiresIn,
  onBack,
  onTarget,
}: {
  auth: AuthApi & MfaAuth;
  challenge: string;
  expiresIn: number;
  /** Drops the challenge and shows the password step, with an optional line. */
  onBack: (message?: string) => void;
  /** Where to land after the session is applied, overriding the page the person came from (a lost batch goes to Settings). */
  onTarget: (path: string) => void;
}) {
  const [stage, setStage] = useState<Stage>({ k: "start" });
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const sent = useRef(false); // the enrolment challenge is single use (W-19): one start, never from an effect
  const lapsed = useRef(false);
  const committed = useRef(false); // a confirm answered 200: the challenge is spent, no timer may drop the page (AU-5)
  const [error, setError] = useState<string | null>(null);
  const [wait, setWait] = useState<{ from: number; seconds: number } | null>(null);
  const waitLeft = useCountdown(wait);
  const back = useRef(onBack);
  useEffect(() => {
    back.current = onBack;
  });

  // AU-6: hold the session state still while S4 or S5 is on the screen; every route off the screen releases it.
  const release = useRef<(() => void) | undefined>(undefined);
  const showing = stage.k === "s4" || stage.k === "s5";
  useEffect(() => {
    if (!showing) return;
    const done = auth.hold();
    release.current = done;
    return () => {
      done();
      if (release.current === done) release.current = undefined;
    };
  }, [showing, auth]);
  const letGo = useCallback((): void => {
    release.current?.();
    release.current = undefined;
  }, []);

  // AU-5: `min(expires_in, 300)` seconds from the password answer. Cancelled by the first good confirm. In flight, the answer decides.
  const seconds = Math.min(expiresIn, ENROL_CHALLENGE_MAX);
  const live = stage.k === "start" || stage.k === "s4" || (stage.k === "cleared" && !stage.committed);
  useEffect(() => {
    if (!live || committed.current) return;
    const timer = setTimeout(() => {
      if (committed.current) return;
      if (busyRef.current) lapsed.current = true;
      else {
        letGo();
        back.current(TIMEOUT_TEXT);
      }
    }, seconds * 1000);
    return () => { clearTimeout(timer); };
  }, [seconds, live, letGo]);

  // W-6: pagehide drops the secrets, committed synchronously so the back/forward cache holds none; pageshow is the backstop;
  // S4 is also dropped after five minutes with the tab hidden. S5 never is.
  useEffect(() => {
    const drop = (): void => {
      flushSync(() => {
        setStage((current) =>
          current.k === "s4"
            ? { k: "cleared", committed: committed.current }
            : current.k === "s5"
              ? { k: "cleared", committed: true, apply: current.apply }
              : current,
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
        setStage((current) => (current.k === "s4" ? { k: "cleared", committed: committed.current } : current));
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

  /** After a failure that leaves the secrets off screen: ask the server whether a session exists (the W-9 and W-20 rule). */
  const sessionCheck = async (textIfNone: string): Promise<void> => {
    letGo();
    await auth.load();
    if (auth.state().kind !== "signed-in") back.current(textIfNone);
    // signed in: the login route redirects, and the first page shows the lost-codes notice (the flag is set)
  };

  const start = async (): Promise<void> => {
    if (busyRef.current || sent.current) return; // single flight (W-19)
    sent.current = true;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const enrollment = await auth.loginEnroll(challenge);
      setStage({ k: "s4", enrollment });
    } catch (raised) {
      const failure = describeStepFailure(raised, "enroll");
      if (failure.effect === "lapse") back.current(TIMEOUT_TEXT);
      else if (failure.effect === "unsafe") back.current(LOGIN_UNSAFE_TEXT);
      else if (failure.effect === "restart" || failure.effect === "show") back.current(failure.text);
      else back.current();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const confirm = async (code: string, enrollment: MfaEnrollment): Promise<void> => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const done = await auth.loginEnrollConfirm({ challenge, factorId: enrollment.factorId, code });
      committed.current = true;
      setCodesUnsaved(); // before S5 renders (W-23)
      setStage({ k: "s5", codes: done.recoveryCodes, apply: done.apply });
    } catch (raised) {
      const failure = describeStepFailure(raised, "confirm");
      const late = lapsed.current;
      switch (failure.effect) {
        case "show":
          if (late) back.current(TIMEOUT_TEXT);
          else setError(failure.text);
          break;
        case "wait":
          setError(failure.text);
          if (failure.seconds !== undefined) setWait({ from: Date.now(), seconds: failure.seconds });
          break;
        case "lapse":
          letGo();
          back.current(TIMEOUT_TEXT);
          break;
        case "restart":
          letGo();
          back.current(failure.text);
          break;
        case "unsafe":
          // The server acted (factor active, session cookie set) but the answer cannot be shown: the codes are lost (W-9).
          committed.current = true;
          setCodesUnsaved();
          setStage({ k: "start" });
          await sessionCheck(LOGIN_UNSAFE_TEXT);
          break;
        case "unknown":
          committed.current = true;
          setCodesUnsaved();
          setStage({ k: "start" });
          await sessionCheck(UNKNOWN_TEXT);
          break;
        default:
          setError(UNKNOWN_TEXT);
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  /** Continue: the session is read again from the server now, never the stale answer (AU-3). */
  const finish = async (apply: () => Promise<void>, clearFlag: boolean): Promise<void> => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await apply();
      if (clearFlag) clearCodesUnsaved();
      letGo();
      if (auth.state().kind !== "signed-in") back.current(SESSION_ENDED_TEXT);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-4" data-testid="mfa-login-enrol">
      {stage.k === "start" && (
        <div className="flex flex-col gap-3" data-testid="mfa-enroll-required">
          <p className="text-sm">Your organization requires a second step for owners.</p>
          <p className="text-sm text-muted-foreground">Set up an authenticator app now. You will get ten recovery codes to keep.</p>
          {error !== null && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <Button className="min-h-11 w-full" disabled={busy} onClick={() => { void start(); }}>
            {busy ? "Starting…" : "Set up"}
          </Button>
          <Button type="button" variant="link" className="min-h-11 self-start px-0" onClick={() => { back.current(); }}>
            Back
          </Button>
        </div>
      )}
      {stage.k === "s4" && (
        <SetupBody
          enrollment={stage.enrollment}
          busy={busy}
          error={error}
          waitLeft={waitLeft}
          onConfirm={(code) => { void confirm(code, stage.enrollment); }}
        />
      )}
      {stage.k === "s5" && (
        <RecoveryCodes
          codes={stage.codes}
          onContinue={() => { void finish(stage.apply, true); }}
        />
      )}
      {stage.k === "cleared" && (
        <div role="status" className="flex flex-col gap-2 text-sm" data-testid="mfa-cleared">
          {stage.committed ? (
            <>
              <p>{CODES_LOST_TEXT}</p>
              <Button
                className="min-h-11 self-start"
                disabled={busy}
                onClick={() => {
                  onTarget("/settings");
                  void finish(stage.apply ?? (() => auth.load()), false);
                }}
              >
                Continue
              </Button>
            </>
          ) : (
            <>
              <p>Setup was interrupted. Start again.</p>
              <Button className="min-h-11 self-start" onClick={() => { back.current(); }}>Start again</Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
