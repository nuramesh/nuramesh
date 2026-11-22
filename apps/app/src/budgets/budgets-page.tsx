import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { InvalidRequestError, NotAvailableError } from "@/api/client";
import { pyStrip } from "@/model/org";
import { useApi } from "@/api/context";
import { keys } from "@/api/hooks";
import { describeProblem } from "@/api/problems";
import { compareMoney, formatMoney, percentUsed, toMicros, usd } from "@/api/money";
import type { BudgetRow, BudgetStatus, BudgetSummary, ModelSpend } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Chip, type ChipTone } from "@/components/ui/chip";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { BreakersPanel } from "@/org-safety/breakers-panel";
import { useCanAct } from "@/org-safety/use-org-safety";
import { SavingsPanel } from "./savings-panel";
import { WINDOW_NAME, windowNote } from "./windows";
import { formatStarted } from "@/lib/time";

const STATUS: Record<BudgetStatus, { label: string; text: string; bar: string; tone: ChipTone }> = {
  ok: { label: "ok", text: "text-link", bar: "bg-link", tone: "success" },
  near: { label: "near limit", text: "text-warm", bar: "bg-warm", tone: "warning" },
  blocked: { label: "blocked", text: "text-danger", bar: "bg-danger", tone: "danger" },
};

const retry = (count: number, error: unknown): boolean => !(error instanceof NotAvailableError) && count < 2;

function Stat({ label, children, detail }: { label: string; children: React.ReactNode; detail?: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border bg-panel px-4 py-3.5 border-card-border shadow-card">
      <h2 className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">{label}</h2>
      <p className="text-[22px] font-semibold">{children}</p>
      {detail !== undefined && <p className="text-[12.5px] text-muted-foreground">{detail}</p>}
    </div>
  );
}

function Unit({ children }: { children: React.ReactNode }) {
  return <span className="text-sm font-normal text-muted-foreground">{children}</span>;
}

/** The breach card: the run stopped at its limit, and a person unblocks it with a reason (saved to the ledger). */
function BreachCard({ breach, onDone }: { breach: NonNullable<BudgetSummary["breach"]>; onDone: (text: string) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const canAct = useCanAct();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [newLimit, setNewLimit] = useState("");
  const [touched, setTouched] = useState(false);
  const reasonMissing = pyStrip(reason) === "";
  let limitProblem: string | null = null;
  if (newLimit.trim() !== "") {
    try {
      if (compareMoney(usd(newLimit.trim()), breach.limit) <= 0) limitProblem = `Must be more than ${formatMoney(breach.limit)}`;
    } catch {
      limitProblem = /\.\d{7,}/.test(newLimit) ? "At most 6 decimals" : "Use an amount such as 60.00, digits only";
    }
  }
  const unblock = useMutation({
    mutationFn: () => api.budgets.unblock(breach.budgetId, { reason, ...(newLimit.trim() === "" ? {} : { newLimit: newLimit.trim() }) }),
    onSuccess: (result) => {
      onDone(`${breach.scope} unblocked by You · ${result.ledgerEntry === null ? "no ledger entry number came back" : `reason recorded as ledger #${result.ledgerEntry}`}`);
      setOpen(false);
      setReason("");
      setNewLimit("");
      setTouched(false);
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.budgets }),
        queryClient.invalidateQueries({ queryKey: keys.runs }),
        queryClient.invalidateQueries({ queryKey: keys.ledger }),
      ]);
    },
  });
  const submit = (): void => {
    setTouched(true);
    if (reasonMissing || limitProblem !== null) return;
    unblock.mutate();
  };
  const serverProblem = unblock.error instanceof InvalidRequestError ? unblock.error.fields.map((f) => f.message).join(" ") || unblock.error.message : unblock.error === null ? undefined : describeProblem(unblock.error);
  return (
    <section aria-labelledby="breach-title" className="callout-danger flex flex-col gap-2.5 px-4 py-4">
      <div className="flex flex-wrap items-center gap-2.5">
        <h2 id="breach-title" className="text-[15px] font-semibold text-danger">
          {breach.scope} stopped at its {formatMoney(breach.limit)} {breach.window !== undefined && breach.window !== "none" ? `${WINDOW_NAME[breach.window].toLowerCase()} ` : ""}limit
        </h2>
        {breach.ledgerEntry !== "" && <span className="ml-auto font-mono text-xs text-foreground/75">budget.breach · ledger #{breach.ledgerEntry}</span>}
      </div>
      <p className="text-[13.5px] text-foreground/85">
        {breach.nextCall === null ? "The ledger doesn't say which call hit the limit. " : `The next call (${breach.nextCall.actor}, ${breach.nextCall.model}) would have reached ${formatMoney(breach.nextCall.wouldReach)}. `}
        Spending is blocked{breach.since === null ? "" : ` since ${formatStarted(breach.since)}`}.{" "}
        {windowNote({ window: breach.window ?? "none", onBreach: breach.onBreach ?? "hold", blocked: true, resetsAt: breach.resetsAt ?? null })}
      </p>
      {canAct && open && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label htmlFor="unblock-reason" className="text-[13px] font-semibold">
            Reason for unblocking <span className="font-normal text-foreground/75">(required, saved to the ledger as a hash)</span>
          </label>
          <Input
            id="unblock-reason"
            className="min-h-11"
            value={reason}
            placeholder="For example: raise the run limit to $60 for this migration"
            aria-invalid={touched && reasonMissing ? true : undefined}
            aria-describedby="unblock-error"
            onChange={(e) => {
              setReason(e.target.value);
              if (!unblock.isPending) unblock.reset();
            }}
          />
          <label htmlFor="unblock-limit" className="text-[13px] font-semibold">
            New limit in USD <span className="font-normal text-foreground/75">(optional; must be above {formatMoney(breach.limit)})</span>
          </label>
          <Input id="unblock-limit" inputMode="decimal" className="min-h-11 max-w-48 font-mono" value={newLimit} placeholder={formatMoney(breach.limit).replace(/[$,]/g, "")} onChange={(e) => { setNewLimit(e.target.value); if (!unblock.isPending) unblock.reset(); }} aria-invalid={limitProblem !== null ? true : undefined} aria-describedby="unblock-error" />
          <p id="unblock-error" role="alert" className="min-h-4 text-xs text-destructive">
            {touched && reasonMissing ? "Give a reason: it is saved with the decision" : (limitProblem ?? serverProblem ?? "")}
          </p>
        </form>
      )}
      {!canAct && <p className="text-xs text-muted-foreground">Only a signed-in person can unblock a budget.</p>}
      <div className="flex flex-wrap gap-2.5">
        {!canAct ? null : open ? (
          <>
            <Button className="min-h-11" disabled={reasonMissing || limitProblem !== null || unblock.isPending} onClick={submit}>
              Confirm unblock
            </Button>
            <Button
              variant="outline"
              className="min-h-11"
              onClick={() => {
                setOpen(false);
                setReason("");
                setNewLimit("");
                setTouched(false);
                unblock.reset();
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <Button
            className="min-h-11"
            onClick={() => {
              setOpen(true);
            }}
          >
            Unblock with a reason
          </Button>
        )}
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/runs">View runs</Link>
        </Button>
      </div>
    </section>
  );
}

function BudgetsTable({ rows }: { rows: BudgetRow[] }) {
  return (
    <section aria-labelledby="budgets-title" className="min-w-0 flex-[999_1_34rem] overflow-hidden rounded-xl border bg-panel border-card-border shadow-card">
      <h2 id="budgets-title" className="border-b px-4 py-3 text-sm font-semibold">
        Budgets
      </h2>
      {/* A scroll area must take focus so the keyboard can scroll it on a narrow screen. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div role="region" aria-label="Budgets table" tabIndex={0} className="overflow-x-auto focus-visible:outline-2 focus-visible:outline-ring">
        <table className="w-full min-w-[46rem] border-collapse text-[13.5px]">
          <caption className="sr-only">Budgets with limit, spend and status</caption>
          <thead>
            <tr className="table-head">
              {["Scope", "Period", "Limit", "Spent", "Use", "Status", "Window"].map((h) => (
                <th key={h} scope="col" className="px-3 py-2.5">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => {
              const style = STATUS[b.status];
              const pct = percentUsed(b.spent, b.limit);
              return (
                <tr key={b.id} data-status={b.status} className="border-t">
                  <th scope="row" className="min-h-11 px-3 py-3 text-left font-medium">
                    {b.scope}
                  </th>
                  <td className="px-3 text-muted-foreground">{b.period}</td>
                  <td className="px-3 font-mono text-xs">{formatMoney(b.limit)}</td>
                  <td className="px-3 font-mono text-xs">
                    {formatMoney(b.spent)}
                    {b.spentIsPeak && <span className="text-small"> peak</span>}
                  </td>
                  <td className="px-3">
                    <span role="progressbar" aria-label={`${b.scope} used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} className="block h-1.5 min-w-16 overflow-hidden rounded-full bg-track">
                      <span className={cn("block h-full", style.bar)} style={{ width: `${String(pct)}%` }} />
                    </span>
                  </td>
                  <td className="px-3">
                    <Chip tone={style.tone}>{style.label}</Chip>
                  </td>
                  <td className="max-w-64 px-3 py-2 text-xs text-muted-foreground" data-testid={`window-note-${b.id}`}>
                    {windowNote({ window: b.window, onBreach: b.onBreach, blocked: b.status === "blocked", resetsAt: b.resetsAt })}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** The line under the spend-by-model list about unknown prices (never guessed). */
function priceNote(s: BudgetSummary): string {
  if (s.unknownCostCalls !== undefined) {
    return `A cost that is not known is recorded as unknown, never guessed: ${String(s.unknownCostCalls)} call${s.unknownCostCalls === 1 ? " has" : "s have"} an unknown cost.`;
  }
  return `Unknown prices are blocked, never guessed: ${String(s.unknownPriceRefusals)} call${s.unknownPriceRefusals === 1 ? "" : "s"} refused this month.`;
}

function ByModel({ models, note }: { models: ModelSpend[]; note: string }) {
  return (
    <section aria-labelledby="models-title" className="min-w-0 flex-[1_1_22rem] rounded-xl border bg-panel pb-1.5 border-card-border shadow-card">
      <h2 id="models-title" className="border-b px-4 py-3 text-sm font-semibold">
        Spend by model
      </h2>
      <ul className="m-0 list-none p-0">
        {models.map((m) => (
          <li key={m.name} className="flex flex-col gap-1.5 border-t px-4 py-2.5 first:border-t-0">
            <div className="flex items-baseline gap-2">
              <span className="text-[13.5px] font-medium">{m.name}</span>
              <span className="text-xs text-small">{m.meta}</span>
              <span className="ml-auto font-mono text-xs">{m.selfHosted && toMicros(m.cost) === 0n ? "$0 (self-hosted)" : formatMoney(m.cost)}</span>
            </div>
            <span role="progressbar" aria-label={`${m.name} share of spend`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0, Math.min(100, m.sharePercent))} className="block h-[5px] overflow-hidden rounded-full bg-track">
              <span className="block h-full bg-link" style={{ width: `${String(Math.max(0, Math.min(100, m.sharePercent)))}%` }} />
            </span>
          </li>
        ))}
      </ul>
      <p className="mx-4 mt-2 mb-2.5 rounded-lg border border-dashed px-3 py-2.5 text-[12.5px] text-muted-foreground">
        {note}
      </p>
    </section>
  );
}

/** Budgets and spend (SPEC-app-v1 3.5). */
export function BudgetsPage() {
  const api = useApi();
  const summary = useQuery({ queryKey: keys.budgetSummary, queryFn: () => api.budgets.summary(), retry, refetchInterval: 30_000 });
  const rows = useQuery({ queryKey: keys.budgets, queryFn: () => api.budgets.list(), retry });
  const models = useQuery({ queryKey: ["spend", "byModel"], queryFn: () => api.spend.byModel(), retry });
  const [notice, setNotice] = useState<string | null>(null);
  const failed = summary.error ?? rows.error;
  const s = summary.data;
  return (
    <div className="flex flex-col">
      <title>Budgets and spend · NuraMesh</title>
      <header className="flex flex-wrap items-center gap-x-3.5 gap-y-2.5 border-b bg-panel px-6 py-4">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-[21px] font-semibold">Budgets and spend</h1>
          <p className="text-[13px] text-muted-foreground">Checked before every model call, recorded after. A limit stops the work; it never slips silently.</p>
        </div>
        <span className="flex-1" />
        <Button asChild variant="outline" className="min-h-11" title="Agent budgets are set in the org builder (Settings, Budget)">
          <Link to="/builder">New budget</Link>
        </Button>
      </header>
      <Tabs defaultValue="budgets" className="gap-0">
        <div className="border-b bg-panel px-6 py-2">
          <TabsList variant="line" aria-label="Budgets sections">
            <TabsTrigger value="budgets" className="min-h-11">Budgets</TabsTrigger>
            <TabsTrigger value="savings" className="min-h-11">Savings</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="savings" className="px-6 pt-5 pb-7">
          <SavingsPanel />
        </TabsContent>
        <TabsContent value="budgets" className="flex flex-col gap-5 px-6 pt-5 pb-7">
        <p role="status" aria-live="polite" className={notice === null ? "sr-only" : "callout-success px-4 py-3 text-sm"}>
          {notice}
        </p>
        {failed !== null ? (
          <p role="alert" className="rounded-xl border border-dashed p-4 text-sm">
            {failed instanceof NotAvailableError ? "This data source can't show budgets yet." : `Couldn't load the budgets. ${describeProblem(failed)}`}
          </p>
        ) : s === undefined || rows.data === undefined ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading budgets…
          </p>
        ) : (
          <>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(13rem,1fr))] gap-3.5">
              {s.orgMonth === null ? (
                <Stat label="Organization" detail="No organization budget is defined on this server">
                  none
                </Stat>
              ) : (
                <Stat
                  label={s.orgWindow === "day" ? "Org today" : s.orgWindow === "month" ? "Org this month" : "Org in total"}
                  detail={percentUsed(s.orgMonth.spent, s.orgMonth.limit) >= 100 ? "Limit reached" : percentUsed(s.orgMonth.spent, s.orgMonth.limit) >= 90 ? "Near the limit" : undefined}
                >
                  {formatMoney(s.orgMonth.spent)} <Unit>of {formatMoney(s.orgMonth.limit)}</Unit>
                  <span role="progressbar" aria-label={s.orgWindow === "day" ? "Org spend today" : "Org spend this month"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentUsed(s.orgMonth.spent, s.orgMonth.limit)} className="mt-1.5 block h-[5px] overflow-hidden rounded-full bg-track">
                    <span className="block h-full bg-link" style={{ width: `${String(percentUsed(s.orgMonth.spent, s.orgMonth.limit))}%` }} />
                  </span>
                </Stat>
              )}
              <Stat label="Cost per verified task" detail={`average over ${String(s.verifiedTasks)} tasks`}>
                {formatMoney(s.costPerVerifiedTask)}
              </Stat>
              <Stat label="Spent on failed attempts" detail="work that did not pass">
                {formatMoney(s.spentOnFailedAttempts)}
              </Stat>
              <Stat label="Runs" detail={`${String(s.runs.finished)} finished · ${String(s.runs.running)} running · ${String(s.runs.blocked)} blocked`}>
                {s.runs.total}
              </Stat>
            </div>
            {s.breach !== null && <BreachCard key={s.breach.budgetId} breach={s.breach} onDone={setNotice} />}
            <div className="flex flex-wrap items-start gap-5">
              <BudgetsTable rows={rows.data} />
              {models.data !== undefined ? (
                <ByModel models={models.data} note={priceNote(s)} />
              ) : (
                <p className="min-w-0 flex-[1_1_22rem] rounded-xl border border-dashed p-4 text-sm text-muted-foreground" role={models.isError ? "alert" : "status"}>
                  {models.isError ? "Couldn't load spend by model." : "Loading spend by model…"} {priceNote(s)}
                </p>
              )}
            </div>
          </>
        )}
        <BreakersPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
