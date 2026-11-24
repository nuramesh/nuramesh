import { lazy, type SyntheticEvent, Suspense, useState } from "react";

import type { MfaEnrollment } from "@/auth/mfa-types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { totpOf } from "./messages";

// Its own chunk: the QR library is loaded only when a code is drawn, and can be checked alone.
const QrCode = lazy(() => import("./qr-code").then((m) => ({ default: m.QrCode })));

/**
 * S4: the QR drawn here from the `otpauth` address, the key as text behind a click, one code field.
 * The enrolment and the typed code are React state of this page only (AU-4, W-6).
 */
export function SetupBody({
  enrollment,
  busy,
  error,
  waitLeft,
  onConfirm,
}: {
  enrollment: MfaEnrollment;
  busy: boolean;
  error: string | null;
  /** Seconds left of a rate-limit wait (0 when none); the field is disabled meanwhile. */
  waitLeft: number;
  onConfirm: (code: string) => void;
}) {
  const [code, setCode] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied] = useState(false);
  const parsed = totpOf(code);
  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || parsed === null || waitLeft > 0) return;
    onConfirm(parsed);
    setCode("");
  };
  const copyKey = (): void => {
    const clipboard = (globalThis.navigator as Navigator | undefined)?.clipboard;
    clipboard?.writeText(enrollment.secret).then(
      () => { setCopied(true); },
      () => { setCopied(false); },
    );
  };
  return (
    <div className="flex flex-col gap-3" data-testid="mfa-setup">
      <p className="text-sm">Scan this code with an authenticator app, then type the 6-digit code it shows.</p>
      <Suspense fallback={<p role="status" className="text-sm text-muted-foreground">Drawing the code…</p>}>
        <QrCode text={enrollment.otpauthUri} label="QR code for your authenticator app" />
      </Suspense>
      <div className="flex flex-col gap-2">
        <Button variant="outline" className="min-h-11 self-start" aria-expanded={showKey} onClick={() => { setShowKey((v) => !v); }}>
          {showKey ? "Hide the key" : "Can't scan? Show the key"}
        </Button>
        {showKey && (
          <div className="flex flex-wrap items-center gap-2">
            <code className="rounded border bg-panel px-2 py-1 font-mono text-sm break-all" data-testid="mfa-key-text">{enrollment.secret}</code>
            <Button variant="outline" className="min-h-11" onClick={copyKey}>Copy the key</Button>
            {copied && <span role="status" className="text-[13px] text-muted-foreground">Copied.</span>}
          </div>
        )}
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <label htmlFor="mfa-setup-code" className="text-sm font-medium">6-digit code from your app</label>
        <Input
          id="mfa-setup-code"
          name="one-time-code"
          className="h-11 max-w-xs font-mono"
          inputMode="numeric"
          autoComplete="one-time-code"
          spellCheck={false}
          value={code}
          disabled={waitLeft > 0}
          onChange={(e) => { setCode(e.target.value); }}
        />
        <div role="alert" className="empty:hidden">
          {error !== null && <p className="text-sm text-destructive" data-testid="mfa-error">{error}</p>}
          {waitLeft > 0 && <p className="text-sm text-destructive">Try again in {String(waitLeft)} seconds.</p>}
        </div>
        <Button type="submit" className="min-h-11 self-start" disabled={busy || parsed === null || waitLeft > 0}>
          {busy ? "Checking…" : "Confirm"}
        </Button>
      </form>
    </div>
  );
}
