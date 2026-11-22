/**
 * The cross-tab signal "the signed-in person changed" (sign in, sign out, a session that ended).
 * It carries no user id, token or personal data: only that something changed, so every other tab
 * asks the server again who is signed in. BroadcastChannel where the browser has it; otherwise the
 * `storage` event on one key that holds an opaque counter (nothing about the person).
 */
export interface TabSync {
  /** Tells the other tabs the identity changed. */
  notify(): void;
  /** Calls `onChange` when another tab says so. Returns the function that stops listening. */
  listen(onChange: () => void): () => void;
}

const CHANNEL = "nuramesh-auth";
const KEY = "nuramesh-auth-generation";

export function browserTabSync(): TabSync {
  const g = globalThis as unknown as { BroadcastChannel?: typeof BroadcastChannel };
  if (g.BroadcastChannel !== undefined) {
    const Channel = g.BroadcastChannel;
    // One channel object both sends and listens: a channel never hears its own messages, but a second
    // object of the same name in the same tab would, and the tab would then react to its own sign-in.
    const channel = new Channel(CHANNEL);
    return {
      notify: () => {
        channel.postMessage("changed");
      },
      listen(onChange) {
        channel.onmessage = () => {
          onChange();
        };
        return () => {
          channel.onmessage = null;
        };
      },
    };
  }
  return {
    notify() {
      try {
        // an opaque counter: a different value each time, so the other tabs get an event
        globalThis.localStorage.setItem(KEY, `${String(Date.now())}-${String(Math.random()).slice(2)}`);
      } catch {
        // storage blocked: the other tabs find out on their next call
      }
    },
    listen(onChange) {
      const handler = (event: StorageEvent): void => {
        if (event.key === KEY) onChange();
      };
      globalThis.addEventListener("storage", handler);
      return () => {
        globalThis.removeEventListener("storage", handler);
      };
    },
  };
}
