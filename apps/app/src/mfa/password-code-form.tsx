import { type SyntheticEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { totpOf } from "./messages";

export type FormMode = "enrol" | "regenerate" | "disable";

const SUBMIT: Record<FormMode, string> = {
  enrol: "Start setup",
  regenerate: "Make new codes",
  disable: "Turn off MFA",
};

/** The password (and, for regenerate and turn off, a current code) the server asks for again. */
export function PasswordCodeForm({
  mode,
  password,
  code,
  busy,
  error,
  waitLeft,
  lockLeft,
  onPassword,
  onCode,
  onSubmit,
  onCancel,
}: {
  mode: FormMode;
  password: string;
  code: string;
  busy: boolean;
  error: string | null;
  waitLeft: number;
  /** Seconds the form is shut after repeated wrong passwords (W-25). */
  lockLeft: number;
  onPassword: (value: string) => void;
  onCode: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const needsCode = mode !== "enrol";
  const ready = password !== "" && (!needsCode || totpOf(code) !== null);
  const shut = waitLeft > 0 || lockLeft > 0;
  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    if (busy || shut || !ready) return;
    onSubmit();
  };
  return (
    <form onSubmit={submit} className="flex max-w-md flex-col gap-3" noValidate data-testid={`mfa-form-${mode}`}>
      <label htmlFor="mfa-password" className="text-sm font-medium">Your password</label>
      <Input
        id="mfa-password"
        name="current-password"
        type="password"
        className="h-11"
        autoComplete="current-password"
        value={password}
        disabled={shut}
        onChange={(e) => { onPassword(e.target.value); }}
      />
      {needsCode && (
        <>
          <label htmlFor="mfa-code" className="text-sm font-medium">6-digit code from your app</label>
          <Input
            id="mfa-code"
            name="one-time-code"
            className="h-11 font-mono"
            inputMode="numeric"
            autoComplete="one-time-code"
            spellCheck={false}
            value={code}
            disabled={shut}
            onChange={(e) => { onCode(e.target.value); }}
          />
        </>
      )}
      <div role="alert" className="empty:hidden">
        {error !== null && <p className="text-sm text-destructive" data-testid="mfa-error">{error}</p>}
        {waitLeft > 0 && <p className="text-sm text-destructive">Try again in {String(waitLeft)} seconds.</p>}
        {lockLeft > 0 && <p className="text-sm text-destructive">Too many wrong passwords. Try again in {String(lockLeft)} seconds.</p>}
      </div>
      <div className="flex gap-2">
        <Button type="submit" className="min-h-11" disabled={busy || shut || !ready}>{busy ? "Working…" : SUBMIT[mode]}</Button>
        <Button type="button" variant="outline" className="min-h-11" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
