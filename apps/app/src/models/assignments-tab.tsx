import { useState } from "react";

import { useApi } from "@/api/context";
import type { AssignmentRow, CatalogEntry } from "@/api/models";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";

import { ActionDialog } from "./action-dialog";
import { Loading, ReadFailed, RowCard, type TabProps } from "./parts";
import { resultLines } from "./problem-text";
import { useAssignments, useCatalog, useRefreshModels } from "./use-models";

interface Change {
  row: AssignmentRow;
  model: string | null;
}

/** Where a model comes from, in words: the ledger beats the configuration, an agent beats its role. */
function fromWords(from: AssignmentRow["from"]): string {
  return from === "none" ? "no model" : from;
}

function Row({ row, approved, admin, change }: { row: AssignmentRow; approved: CatalogEntry[]; admin: boolean; change: (c: Change) => void }) {
  const current = row.ref;
  const known = current !== null && approved.some((e) => e.ref === current);
  return (
    <RowCard>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-mono text-[14px] font-semibold">{row.subject}</span>
        <span className="text-xs text-muted-foreground">role {row.role}</span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {admin ? (
          <NativeSelect
            aria-label={`Model for ${row.subject}`}
            className="min-h-11 min-w-0"
            value={current ?? ""}
            onChange={(e) => {
              if (e.target.value !== "" && e.target.value !== current) change({ row, model: e.target.value });
            }}
          >
            {current === null && <option value="">Choose a model…</option>}
            {current !== null && !known && (
              <option value={current} disabled>
                {current} (not approved)
              </option>
            )}
            {approved.map((e) => (
              <option key={e.ref} value={e.ref}>
                {e.ref}
              </option>
            ))}
          </NativeSelect>
        ) : (
          <span className="font-mono text-[13px]">{current ?? "no model"}</span>
        )}
        <span className="text-[13px] text-muted-foreground">{fromWords(row.from)}</span>
        {admin && row.from === "agent ledger" && (
          <Button
            variant="outline"
            size="sm"
            className="min-h-11"
            aria-label={`Clear the ledger assignment of ${row.subject}`}
            onClick={() => {
              change({ row, model: null });
            }}
          >
            Clear
          </Button>
        )}
      </div>
      {current === null && <p className="text-xs text-muted-foreground">{row.subject} has no model: it cannot make a model call.</p>}
    </RowCard>
  );
}

/** Which approved model each agent and role uses (spec 10.4); an admin changes it with a reason. */
export function AssignmentsTab({ admin, notify }: TabProps) {
  const api = useApi();
  const refresh = useRefreshModels();
  const assignments = useAssignments();
  const catalog = useCatalog();
  const [change, setChange] = useState<Change | null>(null);

  if (assignments.isPending || catalog.isPending) return <Loading what="the assignments" />;
  if (assignments.isError) return <ReadFailed what="the assignments" error={assignments.error} retry={() => void assignments.refetch()} />;
  if (catalog.isError) return <ReadFailed what="the model catalog" error={catalog.error} retry={() => void catalog.refetch()} />;
  const approved = catalog.data.entries.filter((e) => e.status === "approved");

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        An assignment written here beats the team configuration for that agent or role; an agent beats its role. It takes effect at the next run start or the agent&apos;s next model call. The picker offers approved models only.
      </p>
      <ul aria-label="Model assignments" className="flex flex-col gap-3">
        {assignments.data.map((row) => (
          <Row key={row.subject} row={row} approved={approved} admin={admin} change={setChange} />
        ))}
      </ul>
      {change !== null && (
        <ActionDialog
          title={change.model === null ? `Clear the assignment of ${change.row.subject}` : `Assign ${change.model} to ${change.row.subject}`}
          description={
            change.model === null
              ? "The team configuration applies again for this subject."
              : `${change.row.subject} will use ${change.model} from its next call. A verifier must still come from another family than the worker it checks.`
          }
          submit={change.model === null ? "Clear assignment" : "Assign"}
          run={(reason) => api.models.assign({ scope: change.row.scope, id: change.row.id, model: change.model, reason })}
          onDone={(r) => {
            notify(resultLines(r, change.model === null ? `Cleared the assignment of ${change.row.subject}` : `${change.row.subject} now uses ${change.model}`));
            setChange(null);
            void refresh();
          }}
          onClose={() => {
            setChange(null);
          }}
        />
      )}
    </div>
  );
}
