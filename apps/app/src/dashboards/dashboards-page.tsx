import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Navigate, NavLink, useParams } from "react-router";

import { NotAvailableError } from "@/api/client";
import { useApi } from "@/api/context";
import {
  type ApprovalsDashboard,
  type CostLatencyDashboard,
  DASHBOARD_VIEWS,
  type DashboardEnvelope,
  type DashboardView,
  formatShare,
  type MoneyRow,
  type Tally,
  type VerificationDashboard,
  type VerifiersDashboard,
} from "@/api/dashboards";
import { describeProblem } from "@/api/problems";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** A figure the server could not give is an em dash with "no data", never "0%" or "0". */
function Figure({ value }: { value: string | number | null }) {
  if (value === null) {
    return (
      <span title="no data" aria-label="no data">
        {"—"}
      </span>
    );
  }
  return <span>{value}</span>;
}

function Rate({ rate }: { rate: string | null }) {
  return <Figure value={rate === null ? null : formatShare(rate)} />;
}

/** One small chart: horizontal bars of whole numbers, drawn with plain elements and labelled in text. */
function Bars({ label, items }: { label: string; items: { name: string; value: number }[] }) {
  const top = Math.max(1, ...items.map((i) => i.value));
  return (
    <figure className="flex flex-col gap-1" aria-label={label}>
      <figcaption className="text-xs text-muted-foreground">{label}</figcaption>
      <div role="img" aria-label={items.map((i) => `${i.name} ${String(i.value)}`).join(", ")} className="flex flex-col gap-1">
        {items.map((i) => (
          <div key={i.name} className="flex items-center gap-2 text-xs">
            <span className="w-24 shrink-0">{i.name}</span>
            <span className="h-3 rounded bg-primary" style={{ width: `${String(Math.round((i.value / top) * 100))}%`, minWidth: i.value > 0 ? "2px" : "0" }} />
            <span className="font-mono">{i.value}</span>
          </div>
        ))}
      </div>
    </figure>
  );
}

function Table({ caption, head, children }: { caption: string; head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border bg-panel border-card-border shadow-card">
      <table className="w-full min-w-[32rem] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="table-head border-b">
            {head.map((h, i) => (
              <th key={h} scope="col" className={cn("py-3 text-left", i === 0 ? "px-4" : "px-3")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

const Cell = ({ children }: { children: ReactNode }) => <td className="px-3 py-3">{children}</td>;
const Row = ({ name, children }: { name: string; children: ReactNode }) => (
  <tr className="border-b last:border-b-0">
    <th scope="row" className="px-4 py-3 text-left font-normal">
      <span className="font-mono text-xs">{name}</span>
    </th>
    {children}
  </tr>
);

function TallyCells({ t }: { t: Tally }) {
  return (
    <>
      <Cell>{t.passed}</Cell>
      <Cell>{t.failed}</Cell>
      <Cell>{t.error}</Cell>
      <Cell>
        <Rate rate={t.pass_rate} />
      </Cell>
    </>
  );
}

function Verification({ d }: { d: VerificationDashboard }) {
  const head = ["Passed", "Failed", "Errors", "Pass rate"];
  return (
    <>
      <p data-testid="headline">
        Pass rate <Rate rate={d.pass_rate} />: {d.passed} passed, {d.failed} failed, {d.error} errors (errors are not verdicts and are left out of the rate).
      </p>
      <Bars
        label="Gate results"
        items={[
          { name: "passed", value: d.passed },
          { name: "failed", value: d.failed },
          { name: "errors", value: d.error },
        ]}
      />
      <Table caption="Gate results by gate" head={["Gate", ...head]}>
        {d.gates.map((g) => (
          <Row key={g.gate} name={g.gate}>
            <TallyCells t={g} />
          </Row>
        ))}
      </Table>
      <Table caption="Gate results by role" head={["Role", ...head]}>
        {d.roles.map((r) => (
          <Row key={r.role ?? "none"} name={r.role ?? "no role"}>
            <TallyCells t={r} />
          </Row>
        ))}
      </Table>
    </>
  );
}

function Verifiers({ d }: { d: VerifiersDashboard }) {
  const rounds = (["1", "2", "3", "4+"] as const).map((k) => ({ name: `${k} round${k === "1" ? "" : "s"}`, value: d.rounds_to_pass[k] }));
  return (
    <>
      <p data-testid="headline">
        Verifier fail rate <Rate rate={d.catch_rate} />: {d.verifier_failed} failed and {d.verifier_passed} passed. This counts what verifiers decided; it does not show a verifier was right.
      </p>
      {d.unknown_kind_results > 0 && (
        <p data-testid="verifier-unknown">
          {d.unknown_kind_results} gate results have no recorded start, so the kind of gate (verifier or not) is unknown and they are not counted above. Ledgers written before gate starts were recorded show no verifier figures.
        </p>
      )}
      <Bars label="Rounds until the first passing verifier result" items={rounds} />
      <Table caption="Verifier results" head={["Figure", "Count"]}>
        <Row name="passed">
          <Cell>{d.verifier_passed}</Cell>
        </Row>
        <Row name="failed">
          <Cell>{d.verifier_failed}</Cell>
        </Row>
        <Row name="results of unknown kind">
          <Cell>{d.unknown_kind_results}</Cell>
        </Row>
        <Row name="done without a verifier pass">
          <Cell>{d.done_without_verifier_pass}</Cell>
        </Row>
      </Table>
    </>
  );
}

function money(m: MoneyRow, currency: string): string {
  return `${m.total} ${currency}`;
}

function CostLatency({ d }: { d: CostLatencyDashboard }) {
  const ms = (v: number | null) => <Figure value={v === null ? null : `${String(v)} ms`} />;
  return (
    <>
      <p data-testid="headline">
        Latency: p50 {ms(d.p50)}, p95 {ms(d.p95)}, max {ms(d.max)}
        {d.latency_skipped > 0 ? ` (${String(d.latency_skipped)} calls without a valid time)` : ""}.
      </p>
      <Bars
        label="Latency in milliseconds"
        items={[
          { name: "p50", value: d.p50 ?? 0 },
          { name: "p95", value: d.p95 ?? 0 },
          { name: "max", value: d.max ?? 0 },
        ]}
      />
      <Table caption="Cost by currency" head={["Currency", "Total", "Calls", "Without a price"]}>
        {Object.entries(d.currencies).map(([c, m]) => (
          <Row key={c} name={c}>
            <Cell>{m.total}</Cell>
            <Cell>{m.calls}</Cell>
            <Cell>{m.unknown_cost_calls}</Cell>
          </Row>
        ))}
      </Table>
      <Table caption="Cost by model" head={["Model", "Total", "Calls", "Without a price"]}>
        {d.models.map((m) => (
          <Row key={`${m.model}/${m.currency}`} name={m.model}>
            <Cell>{money(m, m.currency)}</Cell>
            <Cell>{m.calls}</Cell>
            <Cell>{m.unknown_cost_calls}</Cell>
          </Row>
        ))}
      </Table>
      <Table caption="Cost by role" head={["Role", "Total", "Calls", "Without a price"]}>
        {d.roles.map((r) => (
          <Row key={`${r.role ?? "none"}/${r.currency}`} name={r.role ?? "no role"}>
            <Cell>{money(r, r.currency)}</Cell>
            <Cell>{r.calls}</Cell>
            <Cell>{r.unknown_cost_calls}</Cell>
          </Row>
        ))}
      </Table>
    </>
  );
}

function Approvals({ d }: { d: ApprovalsDashboard }) {
  const actions = Object.entries(d.waiting_by_action).map(([name, value]) => ({ name, value }));
  return (
    <>
      <p data-testid="headline">
        {d.waiting} waiting now (as of {d.as_of}); oldest <Figure value={d.oldest_waiting_seconds === null ? null : `${String(d.oldest_waiting_seconds)} s`} />. In the window: {d.decided_approved} approved, {d.decided_denied} denied.
      </p>
      <Bars label="Waiting by action" items={actions} />
      <Table caption="Approvals" head={["Figure", "Count"]}>
        <Row name="waiting">
          <Cell>{d.waiting}</Cell>
        </Row>
        <Row name="approved in the window">
          <Cell>{d.decided_approved}</Cell>
        </Row>
        <Row name="denied in the window">
          <Cell>{d.decided_denied}</Cell>
        </Row>
      </Table>
    </>
  );
}

function useDashboard(view: DashboardView) {
  const api = useApi();
  return useQuery<DashboardEnvelope>({
    queryKey: ["dashboards", view],
    queryFn: (): Promise<DashboardEnvelope> => {
      switch (view) {
        case "verification":
          return api.dashboards.verification();
        case "verifiers":
          return api.dashboards.verifiers();
        case "cost-latency":
          return api.dashboards.costLatency();
        case "approvals":
          return api.dashboards.approvals();
      }
    },
    retry: (count, error) => !(error instanceof NotAvailableError) && count < 2,
  });
}

function Body({ view, data }: { view: DashboardView; data: DashboardEnvelope }) {
  switch (view) {
    case "verification":
      return <Verification d={data as VerificationDashboard} />;
    case "verifiers":
      return <Verifiers d={data as VerifiersDashboard} />;
    case "cost-latency":
      return <CostLatency d={data as CostLatencyDashboard} />;
    case "approvals":
      return <Approvals d={data as ApprovalsDashboard} />;
  }
}

/**
 * The four dashboards (R2.11): verification, verifiers, cost and latency, approvals waiting. Every
 * figure is the server's, computed from the ledger on read; this page only reads and shows the
 * window and the instant it is as of. It writes nothing.
 */
export function DashboardsPage() {
  const { view } = useParams();
  const known = DASHBOARD_VIEWS.find((v) => v.id === view);
  const id: DashboardView = known?.id ?? "verification";
  const query = useDashboard(id);
  if (known === undefined) return <Navigate to="/dashboards/verification" replace />;
  const data = query.data;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4 p-4 sm:p-6">
      <title>{`${known.label} dashboard · NuraMesh`}</title>
      <header className="flex flex-col gap-1">
        <h1 id="page-title" className="text-2xl font-semibold">
          Dashboards
        </h1>
        <p className="text-sm text-muted-foreground">
          Figures counted from the ledger when you open the page. Nothing here is stored, and no prompt, output or person is shown.
        </p>
      </header>
      <nav aria-label="Dashboards" className="flex flex-wrap gap-1.5">
        {DASHBOARD_VIEWS.map((v) => (
          <NavLink
            key={v.id}
            to={`/dashboards/${v.id}`}
            className={({ isActive }) =>
              cn(
                "flex min-h-11 items-center rounded-full border px-4 text-sm no-underline",
                isActive ? "border-pill-on-border bg-pill-on text-pill-on-text" : "border-pill-border bg-pill text-pill-text hover:bg-panel-3",
              )
            }
          >
            {v.label}
          </NavLink>
        ))}
      </nav>
      {query.isError ? (
        <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-dashed p-6">
          <p className="text-sm">
            {query.error instanceof NotAvailableError ? "This data source can't give this dashboard yet." : "Couldn't load the dashboard."}{" "}
            <span className="text-muted-foreground">{query.error instanceof NotAvailableError ? "" : describeProblem(query.error)}</span>
          </p>
          <Button
            variant="outline"
            className="min-h-11"
            onClick={() => {
              void query.refetch();
            }}
          >
            Try again
          </Button>
        </div>
      ) : data === undefined ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading the dashboard…
        </p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground" data-testid="dashboard-window">
            Window {data.from} to {data.to}, as of {data.as_of}, for {data.organization}. {data.scanned_events} ledger entries read.
          </p>
          {data.partial && (
            <p role="status" data-testid="dashboard-partial" className="callout-warning px-4 py-3 text-sm">
              Partial: the server stopped at its entry limit, so these figures cover only the first {data.scanned_events} entries of the window.
            </p>
          )}
          <Body view={id} data={data} />
        </>
      )}
    </section>
  );
}
