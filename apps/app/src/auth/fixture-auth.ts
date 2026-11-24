import { ProblemError } from "@/api/client";

import type { AuthApi, AuthState, AuthUser, LoginStep, MfaAuth, SetupInput } from "./types";

export interface FixtureAccount {
  email: string;
  password: string;
  displayName?: string;
  role?: AuthUser["role"];
}

export interface FixtureAuthOptions {
  /** The one account that can sign in. */
  account: FixtureAccount;
  /** The setup token that makes an owner (omit for none: setup then answers as a used token). */
  setupToken?: string;
  /** The sample server has no owner yet: the sign-in screen offers the setup link. */
  ownerMissing?: boolean;
  /** The sample server has email on: the sign-in screen offers "Reset it by email" instead of "ask your administrator". */
  mailEnabled?: boolean;
  /** A right password then needs this code (or this recovery code) before the session exists. */
  mfa?: { code: string; recoveryCode: string };
  /** A right password answers "a second step is required but not set up". */
  enroll?: boolean;
  /** Start already signed in. */
  signedIn?: boolean;
  /** Failures in a row before a login is refused with 429. Default 5, like the server's account policy. */
  lockAfter?: number;
  /** Seconds a locked login is told to wait. Default 60. */
  lockSeconds?: number;
}

/**
 * An in-memory sign-in with the server's visible behaviour (same errors, same 429 with a wait),
 * for tests, screenshots and the sample-data app. No real credential is involved.
 */
export function createFixtureAuth(options: FixtureAuthOptions): AuthApi & MfaAuth {
  const { account } = options;
  const lockAfter = options.lockAfter ?? 5;
  const lockSeconds = options.lockSeconds ?? 60;
  const user: AuthUser = {
    id: "human:u_0a1b2c3d4e5f60718293",
    orgId: "fixture",
    role: account.role ?? "owner",
    email: account.email,
    displayName: account.displayName ?? "",
  };
  let state: AuthState = options.signedIn === true ? { kind: "signed-in", user } : { kind: "signed-out" };
  let failures = 0;
  let holds = 0;
  const unavailable = (): ProblemError => new ProblemError("multi-factor sign-in is not available", 503, "mfa_unavailable");
  let tokenLeft = options.setupToken !== undefined;
  const listeners = new Set<() => void>();
  const set = (next: AuthState): void => {
    state = next;
    for (const listener of [...listeners]) listener();
  };
  return {
    state: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    load: () => Promise.resolve(),
    login(email, password): Promise<LoginStep> {
      if (failures >= lockAfter) {
        return Promise.reject(
          new ProblemError("too many attempts, try again later", 429, "too_many_attempts", lockSeconds),
        );
      }
      if (email.trim().toLowerCase() !== account.email.toLowerCase() || password !== account.password) {
        failures += 1;
        return Promise.reject(
          new ProblemError("the email or password is not right", 401, "invalid_credentials"),
        );
      }
      failures = 0;
      if (options.enroll === true) return Promise.resolve({ kind: "mfa-enroll" });
      if (options.mfa !== undefined) {
        return Promise.resolve({ kind: "mfa", challenge: "nmc_obviously-fake-challenge", methods: ["totp", "recovery_code"], expiresIn: 300 });
      }
      set({ kind: "signed-in", user });
      return Promise.resolve({ kind: "signed-in" });
    },
    completeMfa(challenge, answer) {
      const mfa = options.mfa;
      const right = mfa !== undefined && challenge === "nmc_obviously-fake-challenge" && ("code" in answer ? answer.code === mfa.code : answer.recoveryCode === mfa.recoveryCode);
      if (!right) return Promise.reject(new ProblemError("the code is not right", 401, "mfa_invalid"));
      set({ kind: "signed-in", user });
      return Promise.resolve();
    },
    logout() {
      set({ kind: "signed-out", reason: "logged-out" });
      return Promise.resolve();
    },
    // The sample server has no MFA key: nothing can be set up, so the Security panel says so.
    hold() {
      holds += 1;
      for (const listener of [...listeners]) listener();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        holds -= 1;
        for (const listener of [...listeners]) listener();
      };
    },
    held: () => holds > 0,
    mfaStatus: () => Promise.resolve({ available: false, enrolled: false, confirmedAt: null, recoveryCodesRemaining: 0, required: false }),
    mfaEnroll: () => Promise.reject(unavailable()),
    mfaConfirm: () => Promise.reject(unavailable()),
    mfaRegenerate: () => Promise.reject(unavailable()),
    mfaDisable: () => Promise.reject(unavailable()),
    loginEnroll: () => Promise.reject(unavailable()),
    loginEnrollConfirm: () => Promise.reject(unavailable()),
    recover: () => Promise.resolve("refused" as const),
    setupStatus: () => Promise.resolve(options.ownerMissing === true && tokenLeft),
    mailEnabled: () => Promise.resolve(options.mailEnabled === true),
    redeemSetup(input: SetupInput) {
      if (!tokenLeft || input.token !== options.setupToken) {
        return Promise.reject(new ProblemError("that setup token can't be used", 403, "invalid_setup_token"));
      }
      tokenLeft = false;
      return Promise.resolve();
    },
  };
}
