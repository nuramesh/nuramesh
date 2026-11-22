import { NotFoundError, ProblemError } from "@/api/client";
import { createHttp, type FetchFn } from "@/api/server/http";

import type { TabSync } from "./tab-sync";
import { type MfaEnrollment, type MfaStatus, RECOVERY_CODE_DISPLAY } from "./mfa-types";
import type { AuthApi, AuthState, AuthUser, LoginStep, MfaAnswer, MfaAuth, SetupInput } from "./types";

interface RawUser {
  id: string;
  org_id?: string;
  role: AuthUser["role"];
  email?: string;
  display_name?: string;
}
interface RawSession {
  user: RawUser;
  csrf_token: string | null;
}

/** The readable CSRF cookie: `__Host-nm_csrf` whenever it exists (the plain name is then ignored, as the server does), else the plain name. */
const HOST_CSRF = "__Host-nm_csrf";
const PLAIN_CSRF = "nm_csrf";

function cookieValue(names: readonly string[]): string | undefined {
  let text = "";
  try {
    text = globalThis.document.cookie;
  } catch {
    return undefined;
  }
  for (const part of text.split(";")) {
    const at = part.indexOf("=");
    if (at < 0) continue;
    if (names.includes(part.slice(0, at).trim())) return part.slice(at + 1).trim();
  }
  return undefined;
}

/** The answer of a secret route was not what the contract says (or came without `no-store`); the server has already acted. */
const unsafeAnswer = (): ProblemError => new ProblemError("the server answered unsafely", 200, "unsafe_answer");

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

/** `recovery_codes`: exactly ten strings in the display pattern, or the answer is refused. */
function codesOf(raw: unknown): string[] {
  const list = isRecord(raw) ? raw.recovery_codes : undefined;
  if (!Array.isArray(list) || list.length !== 10 || !list.every((c) => typeof c === "string" && RECOVERY_CODE_DISPLAY.test(c))) {
    throw unsafeAnswer();
  }
  return list as string[];
}

/** A body that is a session: a `user` the app can read, a `csrf_token` (string or null) and no `status` (spec/mfa-web AU-1). */
function sessionOf(raw: unknown): RawSession | null {
  if (!isRecord(raw) || "status" in raw || !isRecord(raw.user)) return null;
  const user = raw.user;
  if (typeof user.id !== "string" || (user.role !== "owner" && user.role !== "approver" && user.role !== "viewer")) return null;
  if (raw.csrf_token !== null && typeof raw.csrf_token !== "string") return null;
  return raw as unknown as RawSession;
}

const isSeconds = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 900;

const unexpectedAnswer = (): ProblemError =>
  new ProblemError("the server answered in a way this page does not understand", 200, "unexpected_answer");

/**
 * The closed union of a password login's answer, decided by `status` only (spec/mfa-web AU-1); anything else is refused
 * with `unexpected_answer` and nobody is signed in (M1, M2: never sign in on a guess).
 */
function readLogin(raw: unknown): { result: LoginStep; session?: RawSession } {
  if (!isRecord(raw)) throw unexpectedAnswer();
  if (!("status" in raw)) {
    const session = sessionOf(raw);
    if (session === null) throw unexpectedAnswer();
    return { result: { kind: "signed-in" }, session };
  }
  if ("user" in raw) throw unexpectedAnswer();
  const { status, challenge, expires_in: expiresIn, methods } = raw;
  if (typeof challenge !== "string" || !challenge.startsWith("nmc_") || !isSeconds(expiresIn)) throw unexpectedAnswer();
  if (status === "mfa_enroll_required") {
    return { result: { kind: "mfa-enroll", challenge, expiresIn } };
  }
  if (status !== "mfa_required") throw unexpectedAnswer();
  if (!Array.isArray(methods) || !methods.includes("totp") || !methods.every((m) => m === "totp" || m === "recovery_code")) {
    throw unexpectedAnswer();
  }
  return { result: { kind: "mfa", challenge, expiresIn, methods: methods as string[] } };
}


export type Recovery = "renewed" | "refused" | "failed";

export interface ServerAuthOptions {
  baseUrl: string;
  fetch: FetchFn;
  /** Runs `task` while no other tab refreshes (the Web Locks API); the default uses it when the browser has it. */
  lock?: <T>(task: () => Promise<T>) => Promise<T>;
  /** Waits `ms` (only used when the browser has no Web Locks); tests pass a fake. */
  pause?: (ms: number) => Promise<void>;
  /** Tells other tabs when the signed-in person changes, and hears them (default: none; the server client passes the browser's). */
  sync?: TabSync;
}

export interface ServerAuth extends Omit<AuthApi, keyof MfaAuth>, MfaAuth {
  /** The password again, as a fresh proof for a solo approval (SA-8). Always present on the server's sign-in. */
  confirmPassword(password: string): Promise<void>;
  /** The CSRF token for a write: the readable cookie (always the newest, even after another tab refreshed), else the last one sent. */
  readonly csrf: () => string | undefined;
  /**
   * Renews the session after a 401. `renewed` (here or by another tab); `refused` when the server says the session is over
   * (the person is signed out); `failed` for a temporary reason (unreachable, busy, 5xx): nothing is decided.
   */
  readonly recover: () => Promise<Recovery>;
  /** After a 403 `csrf_invalid`: fetches the current token. `true` if a newer one is known and the write may be repeated. */
  readonly refreshCsrf: () => Promise<boolean>;
}

function userOf(raw: RawUser): AuthUser {
  return { id: raw.id, orgId: raw.org_id ?? "", role: raw.role, email: raw.email ?? "", displayName: raw.display_name ?? "" };
}

/**
 * Across tabs the Web Locks API serializes refresh (ND80). A browser without it gets a short random
 * pause instead, so tabs that failed together stop sending in the same instant and the cookie check
 * after the pause can see a sibling's renewal. Smaller chance of a collision, not none (T97).
 */
const withBrowserLock = async <T>(task: () => Promise<T>, pause: ServerAuthOptions["pause"]): Promise<T> => {
  const locks = (globalThis.navigator as Navigator | undefined)?.locks;
  if (locks !== undefined) return locks.request("nuramesh-session-refresh", task);
  const ms = 50 + Math.floor(Math.random() * 200);
  await (pause ?? ((wait) => new Promise<void>((resolve) => setTimeout(resolve, wait))))(ms);
  return task();
};

/**
 * Sign-in for `nuramesh serve` (spec/auth/sessions.md). The browser keeps the tokens in HttpOnly
 * cookies the page cannot read; this module keeps only who is signed in and the CSRF token (which the
 * server puts in a readable cookie on purpose) in memory. Nothing is written to browser storage.
 */
export function createServerAuth(options: ServerAuthOptions): ServerAuth {
  let memoryCsrf: string | undefined;
  // A token the server refused is not offered again (a stale cookie must not shadow the fresh one).
  let rejected: string | undefined;
  const csrf = (): string | undefined => {
    const cookie = cookieValue([HOST_CSRF]) ?? cookieValue([PLAIN_CSRF]);
    return cookie !== undefined && cookie !== rejected ? cookie : memoryCsrf;
  };
  const plain = createHttp(options.baseUrl, options.fetch, { csrf });
  /** After a 403 `csrf_invalid`: asks the session for the current token. `true` if it differs from the refused one. */
  async function refreshCsrf(): Promise<boolean> {
    const refused = csrf();
    try {
      const raw = await plain.get<RawSession>("/v1/auth/session");
      if (raw.csrf_token === null || raw.csrf_token === refused) return false;
      rejected = refused;
      memoryCsrf = raw.csrf_token;
      return true;
    } catch {
      return false;
    }
  }
  const http = createHttp(options.baseUrl, options.fetch, { csrf, refreshCsrf });
  // The login-step calls (AU-2): no CSRF header, no repeat after a 403 (there is no session yet, and a code may be spent).
  const stepHttp = createHttp(options.baseUrl, options.fetch);
  const lock = options.lock ?? ((task) => withBrowserLock(task, options.pause));

  // Every change of who is signed in (a login, a logout, a signal from another tab, a newer load) starts a
  // new epoch; a load or refresh that finishes in an older epoch is stale and must not overwrite the newer answer.
  let epoch = 0;
  let hadSession = false;
  let state: AuthState = { kind: "checking" };
  const listeners = new Set<() => void>();
  const set = (next: AuthState): void => {
    state = next;
    for (const listener of [...listeners]) listener();
  };
  const signedIn = (raw: RawSession): void => {
    memoryCsrf = raw.csrf_token ?? memoryCsrf;
    set({ kind: "signed-in", user: userOf(raw.user) });
  };
  const signedOut = (reason?: "expired" | "logged-out"): void => {
    memoryCsrf = undefined;
    set(reason === undefined ? { kind: "signed-out" } : { kind: "signed-out", reason });
  };
  const announce = (): void => {
    options.sync?.notify();
  };

  // AU-6: while a screen shows secrets nothing may unmount it. Events that would change the state are remembered.
  let holds = 0;
  let pendingSync = false;

  /** One refresh at a time in this tab, and one at a time across tabs. */
  type Refreshed = "renewed" | "invalid" | "failed";
  let inFlight: Promise<Refreshed> | null = null;
  let inFlightEpoch = -1;
  function refresh(): Promise<Refreshed> {
    if (holds > 0) {
      // AU-6: a screen that shows secrets is open. The session may still be renewable, so ask; but on the way back
      // touch ONLY the CSRF token (no user, no state change). A refusal is remembered and acted on at release.
      return lock(async (): Promise<Refreshed> => {
        try {
          const raw = await http.post<RawSession>("/v1/auth/refresh", {});
          memoryCsrf = raw.csrf_token ?? memoryCsrf;
          return "renewed";
        } catch (error) {
          if (error instanceof ProblemError && error.status === 401) {
            pendingSync = true;
            return "invalid";
          }
          return "failed";
        }
      });
    }
    if (inFlight !== null && inFlightEpoch === epoch) return inFlight; // one in flight per person; a newer epoch asks again
    const seen = csrf();
    const mine = epoch; // an answer that finishes after the person changed (another tab) must not touch the state
    const run = lock(async (): Promise<Refreshed> => {
      // Another tab (or an earlier call) renewed the cookies while this waited: nothing to do but retry.
      const now = csrf();
      if (seen !== undefined && now !== undefined && now !== seen) {
        // Someone else renewed. They may also have signed in as another person: ask, and take the answer.
        try {
          const raw = await http.get<RawSession>("/v1/auth/session");
          if (mine === epoch && (state.kind !== "signed-in" || state.user.id !== raw.user.id)) {
            signedIn(raw);
            announce();
          }
        } catch {
          // keep what we know; the next call will say
        }
        return "renewed";
      }
      try {
        const raw = await http.post<RawSession>("/v1/auth/refresh", {});
        if (mine === epoch) signedIn(raw);
        return "renewed";
      } catch (error) {
        if (error instanceof ProblemError && error.status === 401) {
          if (mine !== epoch) return "invalid"; // stale: the state belongs to a newer answer
          if (state.kind === "signed-in") {
            signedOut("expired");
            announce();
          }
          else if (state.kind === "checking" && hadSession) signedOut("expired"); // another tab ended the session we showed
          else if (state.kind !== "signed-out") signedOut(); // after Sign out the reason stays
          return "invalid";
        }
        return "failed"; // unreachable, busy, rate limited: the original call reports its own error
      }
    });
    const mineFlight = run.finally(() => {
      if (inFlight === mineFlight) inFlight = null;
    });
    inFlight = mineFlight;
    inFlightEpoch = mine;
    return mineFlight;
  }


  async function loadSession(): Promise<void> {
    const mine = ++epoch;
    const live = (): boolean => mine === epoch;
    try {
      let raw: RawSession;
      try {
        raw = await http.get<RawSession>("/v1/auth/session");
      } catch (error) {
        if (!(error instanceof ProblemError) || error.status !== 401) throw error;
        // The 15-minute access token may simply have run out while the refresh token is still good.
        const renewed = await refresh();
        if (!live()) return;
        if (renewed === "invalid") {
          return;
        }
        if (renewed === "failed") {
          set({ kind: "unavailable", message: "The server could not renew your session. Try again in a moment." });
          return;
        }
        raw = await http.get<RawSession>("/v1/auth/session");
      }
      if (!live()) return;
      signedIn(raw);
    } catch (error) {
      if (!live()) return;
      if (error instanceof NotFoundError || (error instanceof ProblemError && error.status === 405)) {
        set({ kind: "open" }); // a server without accounts (local development): nothing to sign in to
      } else if (error instanceof ProblemError && error.status === 401) {
        signedOut();
      } else {
        set({ kind: "unavailable", message: error instanceof Error ? error.message : "The server could not be reached." });
      }
    }
  }

  const load = (): Promise<void> => {
    hadSession = false;
    return loadSession();
  };

  // Another tab changed who is signed in: show nothing of the old person at once, then ask the server.
  const onOtherTab = (): void => {
    hadSession =
      state.kind === "checking" ? hadSession : state.kind === "signed-in" || (state.kind === "signed-out" && state.reason === "expired"); // if the server now says no session, the person is told it ended
    memoryCsrf = undefined;
    epoch += 1; // any answer still on its way was asked for the old person
    set({ kind: "checking" });
    void loadSession();
  };
  options.sync?.listen(() => {
    if (holds > 0) {
      pendingSync = true;
      return;
    }
    onOtherTab();
  });

  return {
    state: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    load,
    hold() {
      holds += 1;
      for (const listener of [...listeners]) listener();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        holds -= 1;
        for (const listener of [...listeners]) listener();
        if (holds === 0 && pendingSync) {
          pendingSync = false;
          onOtherTab();
        }
      };
    },
    held: () => holds > 0,
    async mfaStatus(): Promise<MfaStatus> {
      const raw = await http.get<unknown>("/v1/auth/mfa");
      if (!isRecord(raw) || typeof raw.available !== "boolean" || typeof raw.enrolled !== "boolean") throw unsafeAnswer();
      const left = raw.recovery_codes_remaining;
      return {
        available: raw.available,
        enrolled: raw.enrolled,
        confirmedAt: typeof raw.confirmed_at === "string" ? raw.confirmed_at : null,
        recoveryCodesRemaining: typeof left === "number" && Number.isInteger(left) && left >= 0 ? left : 0,
        required: raw.required === true,
      };
    },
    async mfaEnroll(password) {
      const raw = await http.post<unknown>("/v1/auth/mfa/enroll", { password }, { noCsrfRepeat: true, requireNoStore: true });
      if (!isRecord(raw) || typeof raw.factor_id !== "string" || typeof raw.secret !== "string" || typeof raw.otpauth_uri !== "string") {
        throw unsafeAnswer();
      }
      return { factorId: raw.factor_id, secret: raw.secret, otpauthUri: raw.otpauth_uri };
    },
    async mfaConfirm(input) {
      const raw = await http.post<unknown>("/v1/auth/mfa/enroll/confirm", { factor_id: input.factorId, code: input.code }, { noCsrfRepeat: true, requireNoStore: true });
      return codesOf(raw);
    },
    async mfaRegenerate(input) {
      const raw = await http.post<unknown>("/v1/auth/mfa/recovery-codes", { password: input.password, code: input.code }, { noCsrfRepeat: true, requireNoStore: true });
      return codesOf(raw);
    },
    async mfaDisable(input) {
      await http.post("/v1/auth/mfa/disable", { password: input.password, code: input.code });
    },
    async confirmPassword(password) {
      await http.post("/v1/auth/password/confirm", { password });
    },
    csrf,
    refreshCsrf,
    recover: async () => {
      const result = await refresh();
      return result === "invalid" ? "refused" : result;
    },
    async login(email, password): Promise<LoginStep> {
      const { result, session } = readLogin(await http.post<unknown>("/v1/auth/login", { email, password }));
      if (session !== undefined) {
        epoch += 1;
        signedIn(session);
        announce();
      }
      return result;
    },
    async completeMfa(challenge: string, answer: MfaAnswer) {
      const body = "code" in answer ? { challenge, code: answer.code } : { challenge, recovery_code: answer.recoveryCode };
      const session = sessionOf(await http.post<unknown>("/v1/auth/login/mfa", body, { noCsrfRepeat: true }));
      if (session === null) throw unsafeAnswer(); // the server has already acted: the page runs its session check
      epoch += 1;
      signedIn(session);
      announce();
    },
    async loginEnroll(challenge): Promise<MfaEnrollment> {
      const raw = await stepHttp.post<unknown>("/v1/auth/login/mfa/enroll", { challenge }, { noCsrfRepeat: true, requireNoStore: true });
      if (!isRecord(raw) || typeof raw.factor_id !== "string" || typeof raw.secret !== "string" || typeof raw.otpauth_uri !== "string") {
        throw unsafeAnswer();
      }
      return { factorId: raw.factor_id, secret: raw.secret, otpauthUri: raw.otpauth_uri };
    },
    async loginEnrollConfirm(input) {
      const raw = await stepHttp.post<unknown>(
        "/v1/auth/login/mfa/enroll/confirm",
        { challenge: input.challenge, factor_id: input.factorId, code: input.code },
        { noCsrfRepeat: true, requireNoStore: true },
      );
      if (sessionOf(raw) === null) throw unsafeAnswer();
      const recoveryCodes = codesOf(raw);
      // Nothing is held but the ten codes. The state changes only on `apply`, which asks the server what is true then (AU-3).
      return {
        recoveryCodes,
        apply: async () => {
          await load();
          announce();
        },
      };
    },
    async logout() {
      try {
        await http.post("/v1/auth/logout", {}); // a stale CSRF token is renewed and the call repeated by the client
      } catch (error) {
        if (!(error instanceof ProblemError) || error.status !== 401) throw error; // 401: already signed out
      }
      epoch += 1;
      signedOut("logged-out");
      announce();
    },
    async setupStatus() {
      try {
        const raw = await plain.get<{ owner_missing?: unknown }>("/v1/auth/setup/status");
        return raw.owner_missing === true;
      } catch {
        return false; // an error, a limit or a server without accounts: do not offer the link
      }
    },
    async mailEnabled() {
      try {
        const raw = await plain.get<{ mail_enabled?: unknown }>("/v1/health");
        return raw.mail_enabled === true;
      } catch {
        return false; // an error, a limit or an older server: email is taken as off
      }
    },
    async redeemSetup(input: SetupInput) {
      await http.post("/v1/auth/setup/redeem", {
        token: input.token,
        email: input.email,
        ...(input.displayName.trim() === "" ? {} : { display_name: input.displayName.trim() }),
        password: input.password,
      });
    },
  };
}
