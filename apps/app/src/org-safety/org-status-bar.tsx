import { useEffect, useId, useState } from "react";

import type { OrgStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { formatClock } from "@/lib/time";

import { haltedLine, soloRemovalLine } from "./format";
import { HaltDialog } from "./halt-dialog";
import { ResumeDialog } from "./resume-dialog";
import { RoleDialog } from "./role-dialog";
import { RolesArea } from "./roles-area";
import { useOrgStatus } from "./use-org-safety";

/** How long the halted banner keeps `role="alert"`: announced the first time it appears, plain text afterwards. */
const ALERT_MS = 8_000;

function RunningText({ canAct, noteId }: { canAct: boolean; noteId: string }) {
  return (
    <>
      <p className="flex items-center gap-2 text-[13.5px] font-medium">
        <span aria-hidden="true" className="size-2 shrink-0 rounded-full bg-link" />
        Organization running
      </p>
      {!canAct && (
        <p id={noteId} className="text-xs text-muted-foreground">
          Only a signed-in person can halt the organization.
        </p>
      )}
    </>
  );
}

function HaltedText({ status, noteId }: { status: OrgStatus; noteId: string }) {
  const [plain, setPlain] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      setPlain(true);
    }, ALERT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);
  return (
    <div {...(plain ? {} : { role: "alert" })} className="flex flex-col gap-1">
      <p className="text-[16px] font-semibold text-danger">Organization halted</p>
      <p className="text-[13.5px]">{haltedLine(status)}</p>
      {status.cause === "evaluation_unavailable" && <p className="text-[13.5px]">Fix the cause, restart the server, then resume.</p>}
      <p className="text-[13px] text-foreground/85">No new work or model calls start. Steps already running finish and are charged.</p>
      {!status.canAct && (
        <p id={noteId} className="text-xs text-muted-foreground">
          Only a signed-in person can resume it.
        </p>
      )}
    </div>
  );
}

/**
 * The organization's status bar (E19, the organization safety notes section 12): one slim row while running,
 * a banner while halted, with the Halt or Resume button for a person and the per-role pause
 * controls. It reads every 5 s; a failed read keeps the last good state and says it may be stale.
 * The Halt and Resume buttons are one element, so keyboard focus survives the state change.
 */
export function OrgStatusBar() {
  const query = useOrgStatus();
  const status = query.data;
  const noteId = useId();
  const [dialog, setDialog] = useState<"halt" | "resume" | null>(null);
  const [roleDialog, setRoleDialog] = useState<{ role: string; mode: "pause" | "resume" } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const said = (message: string | undefined): void => {
    setAnnouncement(message ?? "");
  };

  let body;
  if (status === undefined) {
    body =
      query.isError ? (
        <div className="flex flex-wrap items-center gap-3 px-4 py-2">
          <p className="text-[13.5px]">Could not read the organization status.</p>
          <Button
            variant="outline"
            size="sm"
            className="min-h-11"
            onClick={() => {
              void query.refetch();
            }}
          >
            Try again
          </Button>
        </div>
      ) : (
        <p role="status" className="px-4 py-2 text-[13.5px] text-muted-foreground">
          Checking organization status...
        </p>
      );
  } else {
    const stale = query.isError;
    body = (
      <>
        <div className={status.halted ? "callout-danger border-l-4 px-4 py-3" : "px-4 py-1.5"}>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="flex min-w-0 flex-1 basis-64 flex-col gap-0.5">
              {status.halted ? <HaltedText status={status} noteId={noteId} /> : <RunningText canAct={status.canAct} noteId={noteId} />}
              {soloRemovalLine(status.soloAfterRemoval) !== null && <p className="text-xs text-muted-foreground">{soloRemovalLine(status.soloAfterRemoval)}</p>}
              {stale ? <p className="text-xs text-muted-foreground">Status may be out of date (last read {formatClock(new Date(query.dataUpdatedAt).toISOString())})</p> : null}
            </div>
            {status.canAct ? (
              <Button
                variant={status.halted ? "default" : "destructive"}
                className="min-h-11"
                onClick={() => {
                  setDialog(status.halted ? "resume" : "halt");
                }}
              >
                {status.halted ? "Resume organization" : "Halt organization"}
              </Button>
            ) : null}
          </div>
        </div>
        {status.roles.length > 0 && (
          <RolesArea
            status={status}
            onPause={(role) => {
              setRoleDialog({ role, mode: "pause" });
            }}
            onResume={(role) => {
              setRoleDialog({ role, mode: "resume" });
            }}
          />
        )}
      </>
    );
  }

  return (
    <>
      <section
        aria-label="Organization status"
        {...(status !== undefined && !status.canAct ? { "aria-describedby": noteId } : {})}
        className="shrink-0 border-b border-card-border bg-panel"
      >
        {body}
      </section>
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>
      <HaltDialog
        open={dialog === "halt"}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
        onDone={said}
      />
      {status !== undefined && (
        <ResumeDialog
          open={dialog === "resume"}
          status={status}
          onOpenChange={(open) => {
            if (!open) setDialog(null);
          }}
          onDone={said}
        />
      )}
      <RoleDialog
        role={roleDialog?.role ?? null}
        mode={roleDialog?.mode ?? "pause"}
        onOpenChange={(open) => {
          if (!open) setRoleDialog(null);
        }}
        onDone={said}
      />
    </>
  );
}
