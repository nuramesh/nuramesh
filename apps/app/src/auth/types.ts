/** Who is signed in. Only what the server sends; no token or hash ever reaches the app. */
export interface AuthUser {
  id: string;
  /** The organization the person belongs to (scopes what the browser saves for them). */
  orgId: string;
  role: "owner" | "approver" | "viewer";
  email: string;
  displayName: string;
}

/**
 * Where sign-in stands:
 * - `checking`: the first answer from the server has not arrived;
 * - `open`: this source needs no sign-in (sample data, or a server without accounts);
 * - `signed-out`: nobody is signed in; `reason` says why when the person was signed in before;
 * - `signed-in`: a session exists;
 * - `unavailable`: the server could not be asked, so nothing is known (not the same as signed out).
 */
export type AuthState =
  | { kind: "checking" }
  | { kind: "open" }
  | { kind: "signed-out"; reason?: "expired" | "logged-out" }
  | { kind: "signed-in"; user: AuthUser }
  | { kind: "unavailable"; message: string };

export interface SetupInput {
  token: string;
  email: string;
  displayName: string;
  password: string;
}

/** What a right password led to. */
export type LoginStep =
  | { kind: "signed-in" }
  | { kind: "mfa"; challenge: string; methods: string[]; expiresIn: number }
  /** The organization requires a second step the person has not set up (`challenge` and `expiresIn` are set only when the server's answer carries a well-formed challenge: the login page then offers the enrolment screens). */
  | { kind: "mfa-enroll"; challenge?: string; expiresIn?: number };

export type MfaAnswer = { code: string } | { recoveryCode: string };

import type { LoginConfirmInput, MfaConfirmInput, MfaEnrollment, MfaPasswordCode, MfaStatus } from "./mfa-types";

export type Unsubscribe = () => void;

/**
 * Screens that show secrets and the multi-factor routes. Optional on `AuthApi` so a source
 * without them (and a small test stub) still type-checks; `hasMfa` narrows.
 */
export interface MfaAuth {
  /**
   * Keeps a screen that shows secrets (the setup key, recovery codes) mounted: while any hold is open, a
   * sign-in or sign-out announced by another tab, and a refresh that fails, are remembered and acted on
   * only when the last hold is released. Returns the function that releases this hold (once).
   */
  hold(): Unsubscribe;
  /** True while a hold is open (part of the `state()` snapshot so screens re-render on release). */
  held(): boolean;
  /** Multi-factor routes for the signed-in person. The answers that carry secrets are never cached or logged. */
  mfaStatus(): Promise<MfaStatus>;
  mfaEnroll(password: string): Promise<MfaEnrollment>;
  mfaConfirm(input: MfaConfirmInput): Promise<string[]>;
  mfaRegenerate(input: MfaPasswordCode): Promise<string[]>;
  mfaDisable(input: MfaPasswordCode): Promise<void>;
  /**
   * The enrolment an owner with the policy on and no factor must do before getting a session. It uses
   * its own client (no CSRF header). The code step of a sign-in is `AuthApi.completeMfa`.
   */
  loginEnroll(challenge: string): Promise<MfaEnrollment>;
  /** The ten codes, and `apply` that re-reads the session; nothing signs in until the person calls it (AU-3). */
  loginEnrollConfirm(input: LoginConfirmInput): Promise<{ recoveryCodes: string[]; apply: () => Promise<void> }>;
  /** Renews the session after a 401 (`failed` = a temporary reason: nothing is decided). */
  recover(): Promise<"renewed" | "refused" | "failed">;
}


export interface AuthApi extends Partial<MfaAuth> {
  /** The current state (for `useSyncExternalStore`: the same object until something changes). */
  state(): AuthState;
  subscribe(listener: () => void): Unsubscribe;
  /** Asks the server who is signed in (renewing the session once if only the access token ran out). */
  load(): Promise<void>;
  /**
   * Rejects with `ProblemError` (401 `invalid_credentials`, 429 `too_many_attempts` with `retryAfter`...).
   * Resolves `signed-in` (the state changed), or a second step: the password was right but no session exists yet.
   */
  login(email: string, password: string): Promise<LoginStep>;
  /** Answers a second step with a TOTP code or a recovery code; signs in. Rejects with `ProblemError` (401 `challenge_invalid` = start again). */
  completeMfa(challenge: string, answer: MfaAnswer): Promise<void>;
  /** Ends the session on the server, then here. */
  logout(): Promise<void>;
  /**
   * True only while the server says no owner has ever been made (`GET /v1/auth/setup/status`, a boolean, public).
   * Any other answer (an error, a limit, a source without accounts) is false: the setup link is then not offered.
   */
  setupStatus(): Promise<boolean>;
  /**
   * True only while the server says outgoing email is on (`mail_enabled`, one boolean on the public `GET /v1/health`).
   * Any other answer (an error, a limit, a missing field, a source without email) is false: the reset link is then not offered.
   */
  mailEnabled?(): Promise<boolean>;
  /** Makes the first owner from a setup token. It does not sign anyone in. */
  redeemSetup(input: SetupInput): Promise<void>;
  /**
   * The signed-in person enters their password again (`POST /v1/auth/password/confirm`, solo approval SA-8): a fresh
   * proof for a solo approval of a high-risk kind, for 600 s, on this session only. Nothing is returned or kept.
   * Rejects with `ProblemError` (401 `invalid_credentials`, 429 `too_many_attempts`...). Absent where the source has no such route.
   */
  confirmPassword?(password: string): Promise<void>;
}

/** True when this sign-in source has the multi-factor routes and holds. */
export function hasMfa(auth: AuthApi | undefined): auth is AuthApi & MfaAuth {
  return auth?.mfaStatus !== undefined && auth.hold !== undefined && auth.held !== undefined;
}
