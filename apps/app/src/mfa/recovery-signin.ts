/**
 * "You signed in with a recovery code" (spec/mfa-web W-4): a flag in memory only (a module variable). It is not stored,
 * and the notice that reads it clears it at once, so it shows on the first page after sign-in and never again.
 */
let pending = false;

export const markRecoverySignIn = (): void => {
  pending = true;
};
export const recoverySignInPending = (): boolean => pending;
export const clearRecoverySignIn = (): void => {
  pending = false;
};
