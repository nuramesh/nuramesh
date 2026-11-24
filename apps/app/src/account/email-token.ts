/**
 * The email-verification token. It arrives in the address fragment (`#t=nme_...`, never sent to a server), is
 * removed from the address before anything else happens, and then lives only in this variable: never in a URL,
 * a storage API or a console message (the email verification notes section 7). A module variable so a
 * second render (React strict mode) still finds it.
 */
let captured: string | undefined;

/** Forgets the token (after it is used or found invalid; tests call it between cases). */
export function forgetEmailToken(): void {
  captured = undefined;
}

/** Reads `#t=` once and clears the fragment, keeping the path and query. */
export function takeEmailToken(): string | undefined {
  const match = /^#t=(nme_[A-Za-z0-9_-]{1,200})$/.exec(globalThis.location.hash);
  if (match?.[1] !== undefined) captured = match[1];
  if (globalThis.location.hash !== "") {
    globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
  }
  return captured;
}
