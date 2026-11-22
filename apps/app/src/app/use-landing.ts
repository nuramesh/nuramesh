import { landingFor } from "@/auth/roles";
import { useAuthState } from "@/auth/use-session";

/** The role of the signed-in person, or null where nobody signs in (sample data, a server without accounts). */
export function useRole(): string | null {
  const auth = useAuthState();
  return auth.kind === "signed-in" ? auth.user.role : null;
}

/** The page the index route and the logo go to, by role (AE-1). Navigation only, never access control. */
export function useLanding(): string {
  return landingFor(useRole());
}
