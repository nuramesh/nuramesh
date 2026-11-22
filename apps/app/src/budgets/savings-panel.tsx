import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import { describeProblem } from "@/api/problems";
import { formatSaved, formatSavingShare, type TokenGroupBy, type TokenRow } from "@/api/savings";
import { NativeSelect } from "@/components/ui/native-select";

const GROUPS: { value: TokenGroupBy; label: string }[] = [
  { value: "none", label: "Everything together" },
  { value: "model", label: "By model" },
  { value: "provider", label: "By provider" },
  { value: "role", label: "By role" },
];

const count = (n: number): string => n.toLocaleString("en-US");

/** The calls a rate is computed over: those with a recorded usage block (n beside every rate). */
const withUsage = (r: TokenRow): number => r.calls - r.callsWithoutUsage;

function Notes({ row }: { row: TokenRow }) {
  const notes: string[] = [];
  if (row.unknownCalls > 0) notes.push(`${String(row.unknownCalls)} call${row.unknownCalls === 1 ? "" : "s"} not priced, excluded from the money figures`);
  if (row.unknownSavingCalls > 0) notes.push(`${String(row.unknownSavingCalls)} call${row.unknownSavingCalls === 1 ? "" : "s"} excluded from the saving: the saving cannot be known`);
  if (row.callsWithoutUsage > 0) notes.push(`${String(row.callsWithoutUsage)} call${row.callsWithoutUsage === 1 ? "" : "s"} without recorded tokens`);
  if (notes.length === 0) return <span className="text-muted-foreground">none</span>;
  return (
    <ul className="flex flex-col gap-0.5">
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

/**
 * Tokens by kind, the cache hit rate and what the cache saved (R1.34 Part A). It only reads: there
 * is no button that applies or changes anything. Figures come from the prices recorded on each call.
 */
export function SavingsPanel() {
  const api = useApi();
  const [groupBy, setGroupBy] = useState<TokenGroupBy>("model");
  const report = useQuery({
    queryKey: ["savings", "tokens", groupBy],
    queryFn: () => api.savings.tokens({ groupBy }),
    retry: (failures, error) => !(error instanceof NotAvailableError) && failures < 2,
  });
  const data = report.data;
  return (
    <section aria-labelledby="savings-title" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-0.5">
          <h2 id="savings-title" className="text-[15px] font-semibold">
            Tokens and cache
          </h2>
          <p className="text-[13px] text-muted-foreground">
            This month, from the ledger. The saving uses the prices recorded on each call, never today&apos;s. This view only shows figures; it changes nothing.
          </p>
        </div>
        <span className="flex-1" />
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Group
          <NativeSelect value={groupBy} onChange={(e) => { setGroupBy(e.target.value as TokenGroupBy); }} aria-label="Group by" className="min-h-11">
            {GROUPS.map((g) => (
              <option key={g.value} value={g.value}>
                {g.label}
              </option>
            ))}
          </NativeSelect>
        </label>
      </div>
      {report.isError ? (
        <p role="alert" className="rounded-xl border border-dashed p-4 text-sm">
          {report.error instanceof NotAvailableError ? "This data source can't show token figures yet." : `Couldn't load the token figures. ${describeProblem(report.error)}`}
        </p>
      ) : data === undefined ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading token figures…
        </p>
      ) : data.rows.length === 0 ? (
        <p role="status" className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
          No model calls were recorded in this period.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-panel border-card-border shadow-card">
          <table aria-label="Tokens and cache" className="w-full min-w-[56rem] text-left text-[13px]">
            <thead>
              <tr className="text-xs text-small">
                <th scope="col" className="px-3 py-2 font-medium">Group</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Calls</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Input</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Output</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Cached input</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Cache write</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Cache hit rate</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Saved by cache</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Share of cost</th>
                <th scope="col" className="px-3 py-2 font-medium">Not counted</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.group} data-group={r.group} className="border-t align-top">
                  <th scope="row" className="px-3 py-2 font-medium">{r.group === "all" ? "All calls" : r.group}</th>
                  <td className="px-3 py-2 text-right font-mono">{count(r.calls)}</td>
                  <td className="px-3 py-2 text-right font-mono">{count(r.inputTokens)}</td>
                  <td className="px-3 py-2 text-right font-mono">{count(r.outputTokens)}</td>
                  <td className="px-3 py-2 text-right font-mono">{count(r.cachedInputTokens)}</td>
                  <td className="px-3 py-2 text-right font-mono">{count(r.cacheWriteTokens)}</td>
                  <td className="px-3 py-2 text-right font-mono">
                    {r.cacheHitRate === null ? "no tokens" : formatSavingShare(r.cacheHitRate)} <span className="text-xs text-muted-foreground">n={count(withUsage(r))}</span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono">
                    {formatSaved(r.netCacheSaving)}
                    <span className="block text-xs text-muted-foreground">
                      {formatSaved(r.cacheSaving)} saved, {formatSaved(r.cacheWritePremium)} write premium
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono">
                    {formatSavingShare(r.cacheSavingShare)} <span className="text-xs text-muted-foreground">n={count(r.calls)}</span>
                  </td>
                  <td className="px-3 py-2 text-xs">
                    <Notes row={r} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
