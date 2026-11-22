import { Navigate } from "react-router";

import { useLanding } from "@/app/use-landing";
import { useFirstRun } from "./use-first-run";

/** The index route (FR-2): `/start` while the checklist is active for this person, else the landing page for the role (AE-1). */
export function IndexRedirect() {
  const fr = useFirstRun();
  const landing = useLanding();
  if (fr.visible && !fr.prefs.dismissed && fr.loading) {
    return (
      <p role="status" className="p-8 text-sm text-muted-foreground">
        Loading NuraMesh…
      </p>
    );
  }
  return <Navigate to={fr.active ? "/start" : landing} replace />;
}
