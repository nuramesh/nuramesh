import { ChevronDown } from "lucide-react";
import { useId, useState } from "react";

import type { OrgStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { cn } from "@/lib/utils";

import { pausedLine } from "./format";

/**
 * Per-role pause and resume (kill-switch scope: a role), a disclosure inside the status bar. Open
 * by default while any role is paused. Only a person (`canAct`) gets buttons; each row has one
 * button whose label changes, so keyboard focus stays on it when the role's state flips.
 */
export function RolesArea({ status, onPause, onResume }: { status: OrgStatus; onPause: (role: string) => void; onResume: (role: string) => void }) {
  const listId = useId();
  const pausedCount = status.roles.filter((r) => r.paused).length;
  const [chosen, setChosen] = useState<boolean | null>(null);
  // Open by default while a role is paused, and it stays as it is once opened (it does not snap
  // shut under the person's cursor when the last paused role is resumed).
  if (chosen === null && pausedCount > 0) setChosen(true);
  const open = chosen ?? false;
  return (
    <div className="border-t border-card-border px-4 py-1.5">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2 min-h-11 gap-1.5 text-[13px]"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => {
          setChosen(!open);
        }}
      >
        <ChevronDown aria-hidden="true" className={cn("transition-transform", open ? "" : "-rotate-90")} />
        Roles{pausedCount > 0 ? ` (${String(pausedCount)} paused)` : ""}
      </Button>
      <div id={listId} hidden={!open} className="pb-2">
        {open && (
          <>
            <ul aria-label="Roles" className="m-0 flex list-none flex-col p-0">
              {status.roles.map((r) => (
                <li key={r.role} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-card-border py-1.5 first:border-t-0">
                  <span className="min-w-24 text-[13.5px] font-medium">{r.role}</span>
                  <Chip tone={r.paused ? "warning" : "success"}>{r.paused ? "Paused" : "Running"}</Chip>
                  {r.paused && <span className="text-xs text-muted-foreground">{pausedLine(r)}</span>}
                  {!r.inConfig && <span className="text-xs text-muted-foreground">(removed from config)</span>}
                  {status.canAct && (r.paused || r.inConfig) ? (
                    <Button
                      variant="outline"
                      size="sm"
                      className="ml-auto min-h-11"
                      aria-label={`${r.paused ? "Resume" : "Pause"} role ${r.role}`}
                      onClick={() => {
                        if (r.paused) onResume(r.role);
                        else onPause(r.role);
                      }}
                    >
                      {r.paused ? "Resume" : "Pause"}
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
            {!status.canAct && <p className="pt-1 text-xs text-muted-foreground">Only a signed-in person can pause or resume a role.</p>}
          </>
        )}
      </div>
    </div>
  );
}
