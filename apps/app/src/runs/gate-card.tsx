import { Check, X } from "lucide-react";
import { Link } from "react-router";

import { formatMoney } from "@/api/money";
import type { GateResultCard } from "@/api/types";
import { Chip } from "@/components/ui/chip";
import { entryHref, entryLabel } from "@/ledger/format";

/**
 * The latest gate result: the definition of done as a checklist, and what the verifier did and did
 * not see (independence, D.4). Shows ledger entries as links, never prompt or output content.
 */
export function GateCard({ gate }: { gate: GateResultCard | null }) {
  if (gate === null) {
    return <p className="p-4 text-sm text-muted-foreground">No gate has run yet in this run.</p>;
  }
  const passed = gate.verdict === "passed";
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2.5 border-b px-4 py-3">
        <h3 className="text-sm font-semibold">{gate.gate}</h3>
        <Chip tone={passed ? "success" : "danger"} className="ml-auto">
          {passed ? <Check aria-hidden="true" className="size-3.5" /> : <X aria-hidden="true" className="size-3.5" />}
          {passed ? "Passed" : "Failed"}
        </Chip>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(15rem,1fr))] gap-5 p-4">
        <div className="flex flex-col gap-2">
          <h4 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">Definition of done</h4>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {gate.criteria.map((c) => (
              <li key={c.text} className="flex gap-2 text-[13.5px] text-muted-foreground">
                <span aria-hidden="true" className={c.met ? "text-link" : "text-danger"}>
                  {c.met ? "✓" : "✗"}
                </span>
                <span>
                  <span className="sr-only">{c.met ? "Met: " : "Not met: "}</span>
                  {c.text}
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div className="flex flex-col gap-2 text-[13px]">
          <h4 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">Verifier</h4>
          <dl className="m-0 flex flex-col gap-2">
            {gate.checkedBy !== undefined && (
              <div className="flex gap-2">
                <dt className="text-muted-foreground">Checked by</dt>
                <dd className="ml-auto text-right font-mono text-xs">{gate.checkedBy}</dd>
              </div>
            )}
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Model</dt>
              <dd className="ml-auto text-right">
                {gate.verifierModel === null ? (
                  <span className="text-small">not recorded</span>
                ) : (
                  <>
                    {gate.verifierModel} {gate.differentFamily === null ? null : gate.differentFamily ? "(different family)" : <span className="font-medium text-danger">(same family as the worker)</span>}
                  </>
                )}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Saw</dt>
              <dd className="ml-auto text-right">{gate.saw === null ? <span className="text-small">not recorded</span> : gate.saw.join(", ")}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Not shared</dt>
              <dd className="ml-auto text-right text-warm">{gate.notShared === null ? <span className="text-small">not recorded</span> : gate.notShared.join(", ")}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Cost · ledger</dt>
              <dd className="ml-auto text-right font-mono text-xs">
                {gate.cost === null ? "cost not recorded" : formatMoney(gate.cost)} ·{" "}
                <Link to={entryHref(gate.ledgerEntry)} className="inline-flex min-h-11 items-center text-link underline">
                  #{entryLabel(gate.ledgerEntry)}
                </Link>
              </dd>
            </div>
          </dl>
        </div>
      </div>
    </div>
  );
}
