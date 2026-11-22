import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { NotAvailableError, NotFoundError } from "@/api/client";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { describeProblem } from "@/api/problems";
import { formatMoney } from "@/api/money";
import type { ChainStatus, LedgerCategory, LedgerEntry } from "@/api/types";
import { Button } from "@/components/ui/button";
import { FilterPill } from "@/components/ui/chip";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { formatClock } from "@/lib/time";

import { ANCHOR_WORDS, describeChainProblem, summarizeChain } from "./anchor-words";
import { ChainChip } from "./anchor";
import { CATEGORIES, exportEntries, formatCount, kindTone, shortHash, TONE_CLASS } from "./format";

const PAGE = 200;

function ChainBanner({ status, onVerify, verifying }: { status: ChainStatus; onVerify: () => void; verifying: boolean }) {
  const summary = summarizeChain(status);
  return (
    <div
      role="status"
      data-testid="chain-status"
      className="flex flex-col gap-3 rounded-xl border bg-panel px-4 py-3 border-card-border shadow-card"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <ChainChip summary={summary} className="text-sm" />
        <span className="text-[13.5px] text-foreground/80">
          {formatCount(status.entries)} entries · {status.gaps === 0 ? "no gaps" : `${formatCount(status.gaps)} gap${status.gaps === 1 ? "" : "s"}`} · {summary.detail}
        </span>
        {status.anchorState === undefined && (
          <span className="text-[13.5px] text-foreground/80">
            {status.anchor === null ? (
              "Head not anchored in Git yet"
            ) : (
              <>
                Last anchored to Git commit <span className="font-mono text-foreground">{status.anchor.commit}</span> at {formatClock(status.anchor.at)}
              </>
            )}
          </span>
        )}
        <span className="flex-1" />
        <span className="font-mono text-xs text-foreground/70">checked {formatClock(status.checkedAt)}</span>
        <Button variant="outline" size="sm" className="min-h-11" disabled={verifying} onClick={onVerify}>
          {verifying ? "Verifying…" : "Verify chain"}
        </Button>
      </div>
      {status.streams !== undefined && status.streams.length > 0 && (
        <details className="text-[13px]">
          <summary className="min-h-11 cursor-pointer py-2.5 text-link">
            {status.streams.length} stream{status.streams.length === 1 ? "" : "s"} checked
            {status.truncated === true ? " (more exist than one check covers)" : ""}
          </summary>
          <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Streams checked">
            {status.streams.map((s) => (
              <li key={s.stream} data-testid="stream-check" data-anchor={s.anchor} className="flex flex-wrap gap-x-3 font-mono text-xs">
                <span className="text-foreground">{s.stream}</span>
                <span className="text-muted-foreground">{formatCount(s.entries)} entries</span>
                <span className={s.chainOk ? "text-muted-foreground" : "text-danger"}>{s.chainOk ? "chain ok" : "chain broken"}</span>
                <span className={s.anchor === "anchored" ? "text-link" : s.anchor === "problems" || s.anchor === "unreadable" ? "text-danger" : "text-warm"}>
                  {ANCHOR_WORDS[s.anchor]}
                  {s.unanchoredEvents > 0 ? ` (${formatCount(s.unanchoredEvents)} newer entries)` : ""}
                </span>
                {s.chainProblem !== null && <span className="w-full text-danger">{describeChainProblem(s.chainProblem)}</span>}
                {s.anchorProblems.map((p) => (
                  <span key={`${p.code}:${p.detail}`} className="w-full text-danger">
                    {p.code}: {p.detail}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function Detail({ entry }: { entry: LedgerEntry }) {
  return (
    <aside aria-label="Entry detail" className="flex min-w-0 flex-[1_1_20rem] flex-col gap-3.5 rounded-xl border bg-panel px-4 py-4 border-card-border shadow-card">
      <div className="flex items-baseline gap-2.5">
        <h2 className="text-base font-semibold">Entry #{entry.label}</h2>
        <span className={cn("font-mono text-xs", TONE_CLASS[kindTone(entry.kind)])}>{entry.kind}</span>
      </div>
      <dl className="m-0 flex flex-col gap-2 text-[13px]">
        <div className="flex gap-2.5">
          <dt className="w-24 shrink-0 text-muted-foreground">When</dt>
          <dd className="font-mono text-[12.5px] break-all">
            <time dateTime={entry.at}>
              {new Date(entry.at).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} · {formatClock(entry.at)}
            </time>
          </dd>
        </div>
        <div className="flex gap-2.5">
          <dt className="w-24 shrink-0 text-muted-foreground">Actor</dt>
          <dd className="font-mono text-[12.5px] break-all">{entry.actor}</dd>
        </div>
        <div className="flex gap-2.5">
          <dt className="w-24 shrink-0 text-muted-foreground">Stream</dt>
          <dd className="font-mono text-[12.5px] break-all">{entry.stream}</dd>
        </div>
        {entry.taskId !== null && (
          <div className="flex gap-2.5">
            <dt className="w-24 shrink-0 text-muted-foreground">Task</dt>
            <dd className="font-mono text-[12.5px] break-all">{entry.taskId}</dd>
          </div>
        )}
        {entry.cost !== null && (
          <div className="flex gap-2.5">
            <dt className="w-24 shrink-0 text-muted-foreground">Cost</dt>
            <dd className="font-mono text-[12.5px]">{formatMoney(entry.cost)}</dd>
          </div>
        )}
        {entry.fields.map((f) => (
          <div key={`${f.label}:${f.value}`} className="flex gap-2.5">
            <dt className="w-24 shrink-0 text-muted-foreground">{f.label}</dt>
            <dd className="font-mono text-[12.5px] break-all">{f.value}</dd>
          </div>
        ))}
      </dl>
      <section aria-labelledby="chain-title" className="flex flex-col gap-1.5 rounded-lg border bg-panel-2 p-3">
        <h3 id="chain-title" className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">
          Chain
        </h3>
        <div className="flex gap-2.5 text-[12.5px]">
          <span className="w-[4.5rem] text-muted-foreground">Previous</span>
          <span className="font-mono text-[11px] break-all">{entry.prevHash}</span>
        </div>
        <div className="flex gap-2.5 text-[12.5px]">
          <span className="w-[4.5rem] text-muted-foreground">This</span>
          <span className="font-mono text-[11px] break-all text-link">{entry.hash}</span>
        </div>
      </section>
      <p className="text-xs text-foreground/75">Prompts and outputs are not stored in the ledger. Commands and recipients appear as keyed hashes.</p>
    </aside>
  );
}

/** Columns left out below 768 px: the entry, kind, summary and cost are what a phone needs (ND65). */
const PHONE_HIDDEN = new Set(["Time", "Actor", "Hash"]);

/** The ledger (SPEC-app-v1 3.4): chain status, verify, search and filters, entries and one entry's detail. */
export function LedgerPage() {
  const api = useApi();
  const queryClient = useQueryClient();
  const { entry: selected } = useParams();
  const [category, setCategory] = useState<LedgerCategory | "all">("all");
  const [params] = useSearchParams();
  const urlSearch = params.get("q") ?? "";
  const [search, setSearch] = useState(urlSearch);
  // A later change of ?q= on the mounted page (a link from another page) is followed once.
  const [seenUrlSearch, setSeenUrlSearch] = useState(urlSearch);
  if (seenUrlSearch !== urlSearch) {
    setSeenUrlSearch(urlSearch);
    setSearch(urlSearch);
  }
  const [shown, setShown] = useState(PAGE);
  const deferred = useDeferredValue(search);
  const retry = (count: number, error: unknown): boolean => !(error instanceof NotAvailableError || error instanceof NotFoundError) && count < 2;

  const chain = useQuery({ queryKey: ["ledger", "chain"], queryFn: () => api.ledger.chainStatus(), retry });
  const list = useQuery({
    queryKey: [...keys.ledger, category, deferred],
    queryFn: () => api.ledger.list({ ...(category === "all" ? {} : { category }), search: deferred }),
    placeholderData: keepPreviousData,
    retry,
  });
  const wanted = selected ?? list.data?.[0]?.n;
  const detail = useQuery({
    queryKey: [...keys.ledger, "entry", wanted],
    queryFn: () => api.ledger.get(wanted ?? ""),
    enabled: wanted !== undefined,
    placeholderData: keepPreviousData,
    retry,
  });
  const verify = useMutation({
    mutationFn: () => api.ledger.verifyChain(),
    onSuccess: (status) => {
      queryClient.setQueryData(["ledger", "chain"], status);
    },
  });
  const entries = list.data ?? [];
  const visible = entries.slice(0, shown);

  return (
    <div className="flex flex-col">
      <title>{detail.data === undefined ? "Ledger · NuraMesh" : `Entry #${detail.data.label} · Ledger · NuraMesh`}</title>
      <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 border-b bg-panel px-6 py-4">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-[21px] font-semibold">Ledger</h1>
          <p className="text-[13px] text-muted-foreground">Every action, model call, cost, gate result and decision. Append-only and hash-chained.</p>
        </div>
        <span className="flex-1" />
        <Button
          variant="outline"
          className="min-h-11"
          disabled={entries.length === 0}
          onClick={() => {
            const url = URL.createObjectURL(new Blob([exportEntries(entries)], { type: "application/json" }));
            const link = document.createElement("a");
            link.href = url;
            link.download = "ledger.json";
            document.body.append(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
          }}
        >
          Export
        </Button>
      </header>
      <div className="flex flex-col gap-4 px-6 pt-4 pb-7">
        {chain.data !== undefined ? (
          <ChainBanner
            status={chain.data}
            verifying={verify.isPending}
            onVerify={() => {
              verify.mutate();
            }}
          />
        ) : chain.isError ? (
          <p role="alert" className="rounded-xl border border-dashed p-3 text-sm">
            {chain.error instanceof NotAvailableError ? "This data source can't check the chain yet." : `Couldn't check the chain. ${describeProblem(chain.error)}`}
          </p>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">
            Checking the chain…
          </p>
        )}
        {verify.isError && (
          <p role="alert" className="text-sm text-destructive">
            Couldn't verify the chain: {verify.error.message}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2.5">
          <label htmlFor="ledger-search" className="sr-only">
            Search the ledger
          </label>
          <Input
            id="ledger-search"
            type="search"
            className="min-h-11 flex-[1_1_17rem]"
            placeholder="Search by entry, agent, run or kind"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setShown(PAGE);
            }}
          />
          <div role="group" aria-label="Filter by kind of entry" className="flex flex-wrap gap-1.5">
            {CATEGORIES.map((c) => (
              <FilterPill
                key={c.id}
                pressed={category === c.id}
                onClick={() => {
                  setCategory(c.id);
                  setShown(PAGE);
                }}
              >
                {c.label}
              </FilterPill>
            ))}
          </div>
          <span data-testid="ledger-count" className="font-mono text-xs text-small">
            {list.data === undefined ? "" : `${formatCount(entries.length)} entr${entries.length === 1 ? "y" : "ies"}`}
          </span>
        </div>

        {list.isError ? (
          <p role="alert" className="rounded-xl border border-dashed p-4 text-sm">
            {list.error instanceof NotAvailableError ? "This data source can't list ledger entries yet." : `Couldn't load the ledger. ${describeProblem(list.error)}`}{" "}
            <button type="button" className="underline" onClick={() => void list.refetch()}>
              Try again
            </button>
          </p>
        ) : list.data === undefined ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading entries…
          </p>
        ) : (
          <div className="flex flex-wrap items-start gap-4">
            <section aria-label="Entries" className="min-w-0 flex-[999_1_34rem] overflow-hidden rounded-xl border bg-panel border-card-border shadow-card">
              {entries.length === 0 ? (
                <p className="p-6 text-sm text-muted-foreground">No entries match.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full border-collapse text-sm md:min-w-[44rem]">
                    <caption className="sr-only">Ledger entries, newest first</caption>
                    <thead>
                      <tr className="table-head border-b">
                        {["Entry", "Time", "Kind", "Actor", "Summary", "Cost", "Hash"].map((h) => (
                          <th key={h} scope="col" className={cn("px-3 py-2.5", h === "Cost" && "text-right", PHONE_HIDDEN.has(h) && "hidden md:table-cell")}>
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((e) => {
                        const current = e.n === (detail.data?.n ?? wanted);
                        return (
                          <tr key={e.n} data-entry={e.n} aria-selected={current} className={cn("border-b last:border-b-0 hover:bg-panel-2", current && "bg-panel-2 outline outline-1 -outline-offset-1 outline-border-strong")}>
                            <th scope="row" className="px-2 py-0 text-left font-normal md:px-3">
                              <Link to={`/ledger/${encodeURIComponent(e.n)}`} aria-current={current ? "true" : undefined} className="flex min-h-11 items-center font-mono text-muted-foreground no-underline hover:underline">
                                #{e.label}
                              </Link>
                            </th>
                            <td className="hidden px-3 font-mono text-xs text-small md:table-cell">{formatClock(e.at)}</td>
                            <td className={cn("max-w-40 px-2 font-mono text-xs break-all md:truncate md:px-3 md:break-normal", TONE_CLASS[kindTone(e.kind)])}>{e.kind}</td>
                            <td className="hidden max-w-32 truncate px-3 md:table-cell">{e.actor}</td>
                            <td className="max-w-80 px-2 py-2 text-muted-foreground md:px-3 md:truncate md:py-0" title={e.summary}>
                              {e.summary}
                            </td>
                            <td className="px-2 text-right font-mono text-xs md:px-3">{e.cost === null ? "" : formatMoney(e.cost)}</td>
                            <td className="hidden px-3 font-mono text-xs text-small md:table-cell">{shortHash(e.hash)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {entries.length > shown && (
                    <div className="border-t p-3">
                      <Button
                        variant="outline"
                        size="sm"
                        className="min-h-11"
                        onClick={() => {
                          setShown((n) => n + PAGE);
                        }}
                      >
                        Show {Math.min(PAGE, entries.length - shown)} more
                      </Button>
                    </div>
                  )}
                </div>
              )}
            </section>
            {detail.isError ? (
              <aside aria-label="Entry detail" className="flex-[1_1_20rem] rounded-xl border bg-panel p-4 border-card-border shadow-card">
                <p role="alert" className="text-sm">
                  {detail.error instanceof NotFoundError ? `There is no ledger entry ${wanted ?? ""}.` : "Couldn't load this entry."}{" "}
                  <Link to="/ledger" className="text-link underline">
                    Back to the ledger
                  </Link>
                </p>
              </aside>
            ) : detail.data !== undefined ? (
              <Detail entry={detail.data} />
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
