import { ConflictError } from "@/api/client";

/**
 * Runs `action`; a 409 whose code is in `done` means the state already is what the person asked
 * for, so it counts as success and that code's message (if any) is returned. Anything else rejects.
 * `success` is the message left behind when the action itself worked.
 */
export async function orAlreadyDone(
  action: () => Promise<unknown>,
  done: Record<string, string | undefined>,
  success?: string,
): Promise<string | undefined> {
  try {
    await action();
    return success;
  } catch (error) {
    if (error instanceof ConflictError && Object.hasOwn(done, error.code)) return done[error.code];
    throw error;
  }
}
