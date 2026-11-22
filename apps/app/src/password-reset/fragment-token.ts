/**
 * A link token that arrives in the address fragment (`#t=<prefix>...`, which a browser never sends to a server).
 * It is removed from the address before anything else happens and then lives only in a closure variable: never in
 * a URL, a storage API or a console message (spec/identity/password-reset.md section 9, A-P2, A-P3). A closure
 * variable (not state) so a second render of the same page, as React's strict mode does, still finds it.
 */
export interface FragmentToken {
  /** Reads `#t=` once and clears the fragment, keeping the path and query. */
  take(): string | undefined;
  /** Forgets the token (after it is used or found invalid; tests call it between cases). */
  forget(): void;
}

export function createFragmentToken(prefix: string): FragmentToken {
  let captured: string | undefined;
  const shape = new RegExp(`^#t=(${prefix}[A-Za-z0-9_-]{1,200})$`);
  return {
    take() {
      const match = shape.exec(globalThis.location.hash);
      if (match?.[1] !== undefined) {
        captured = match[1];
        globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
      } else if (globalThis.location.hash !== "") {
        globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
      }
      return captured;
    },
    forget() {
      captured = undefined;
    },
  };
}

export const resetLinkToken = createFragmentToken("nmr_");
export const revokeLinkToken = createFragmentToken("nmk_");
