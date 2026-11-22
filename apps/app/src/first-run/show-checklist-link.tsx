import { Button } from "@/components/ui/button";

import { COPY } from "./copy";
import { useFirstRun } from "./use-first-run";

/** Settings (FR-12): brings back a checklist the person hid. Shown only while it is hidden. */
export function ShowChecklist() {
  const fr = useFirstRun();
  if (!fr.visible || !fr.prefs.dismissed) return null;
  return (
    <div>
      <Button variant="outline" className="min-h-11" onClick={fr.show}>
        {COPY.show}
      </Button>
    </div>
  );
}
