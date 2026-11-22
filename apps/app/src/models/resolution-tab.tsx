import { useState } from "react";

import type { DataClass } from "@/api/models";
import { NativeSelect } from "@/components/ui/native-select";

import { Loading, ReadFailed, RowCard } from "./parts";
import { useResolution } from "./use-models";

/**
 * What a run would resolve right now (`GET /v1/models/resolution`, the same check `models check`
 * runs at a run start). It reads and writes nothing. Admin only: it carries endpoints.
 */
export function ResolutionTab({ admin }: { admin: boolean }) {
  const [dataClass, setDataClass] = useState<DataClass>("synthetic");
  const report = useResolution(dataClass, admin);
  if (!admin) {
    return <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">Only an admin can see the resolution report: it carries connection endpoints and who set them.</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="data-class" className="text-[13px] font-semibold">
            Data class
          </label>
          <NativeSelect
            id="data-class"
            className="min-h-11"
            value={dataClass}
            onChange={(e) => {
              setDataClass(e.target.value === "customer" ? "customer" : "synthetic");
            }}
          >
            <option value="synthetic">synthetic (demos, evals, development)</option>
            <option value="customer">customer</option>
          </NativeSelect>
        </div>
        <p className="min-w-0 flex-1 basis-64 text-xs text-muted-foreground">
          The start-stage check of every agent and every role default, as a run would make it. Gate pairings (a verifier from the same family as its worker) are checked by <code>nuramesh models check</code>, when a run starts and at each call, not here: the server has no plan.
        </p>
      </div>
      {report.isPending && <Loading what="the resolution" />}
      {report.isError && <ReadFailed what="the resolution report" error={report.error} retry={() => void report.refetch()} />}
      {report.data?.ok === true && (
        <>
          <p role="status" className="callout-success px-4 py-3 text-sm">
            Every agent and role resolves for {dataClass} data.
          </p>
          <ul aria-label="Resolved models" className="flex flex-col gap-3">
            {report.data.resolutions.map((r) => (
              <RowCard key={r.subject}>
                <div className="flex flex-wrap items-center gap-x-3">
                  <span className="font-mono text-[14px] font-semibold">{r.subject}</span>
                  <span className="font-mono text-[13px]">{r.ref}</span>
                </div>
                <p className="text-[13px] text-foreground/85">
                  Family {r.family} · processed in <code>{r.processing}</code> · price {r.priceBasis ?? "n/a"}
                  {r.endpoint === null ? " · local model, no endpoint" : ` · ${r.endpoint} (connection #${String(r.connectionSeq)})`}
                </p>
              </RowCard>
            ))}
          </ul>
        </>
      )}
      {report.data?.ok === false && (
        <>
          <p role="alert" className="callout-danger px-4 py-3 text-sm">
            A run would be refused until these are fixed. Nothing is listed as resolved while any block exists, so a partial list cannot be read as a go-ahead.
          </p>
          <ul aria-label="Blocks" className="flex flex-col gap-3">
            {report.data.blocks.map((b) => (
              <RowCard key={`${b.subject}-${b.reason}`}>
                <span className="font-mono text-[13px] break-words">{b.text}</span>
              </RowCard>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
