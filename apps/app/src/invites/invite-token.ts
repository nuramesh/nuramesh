/**
 * The link token. It arrives in the address fragment (`#t=nmi_...`, which a browser never sends to a server),
 * is removed from the address before anything else happens, and then lives only in this variable: never in a
 * URL, a storage API or a console message (spec/identity/invites.md section 8, A-I1..A-I3). A module variable
 * (not state) so a second render of the same page, as React's strict mode does, still finds it.
 */
let captured: string | undefined;

/** Forgets the token (after it is used or found invalid; tests call it between cases). */
export function forgetInviteToken(): void {
  captured = undefined;
}

/** Tests call this between cases. */
export const resetCapturedInviteToken = forgetInviteToken;

/** Reads `#t=` once and clears the fragment, keeping the path and query. */
export function takeToken(): string | undefined {
  const match = /^#t=(nmi_[A-Za-z0-9_-]{1,200})$/.exec(globalThis.location.hash);
  if (match?.[1] !== undefined) {
    captured = match[1];
    globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
  } else if (globalThis.location.hash !== "") {
    globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
  }
  return captured;
}

