import {
  type ApiClient,
  NotAvailableError,
  NotFoundError,
  type RunPage,
  type SourceInfo,
} from "./client";
import type { Invite, InviteRole } from "./invites";
import { browserTabSync, type TabSync } from "@/auth/tab-sync";
import { createServerAuth } from "@/auth/server-auth";
import { toMicros } from "./money";
import {
  type ApprovalsDashboard,
  type CostLatencyDashboard,
  dashboardPath,
  type VerificationDashboard,
  type VerifiersDashboard,
} from "./dashboards";
import { downgradeReport, type RawDowngradeReport } from "./reports";
import { tokenSavings, type RawTokenSavings } from "./savings";
import { createHttp, type FetchFn, type HttpClient } from "./server/http";
import { connectLive, type SocketFactory } from "./server/live";
import { createModelsApi, createSessionApi } from "./server/models";
import {
  approval,
  breakersView,
  budgetRow,
  chainStatus,
  divideExact,
  ledgerEntry,
  money,
  moneyOrNull,
  orgStatus,
  parseEntryId,
  proposalList,
  runLabel,
  runDetail,
  runEvent,
  runSummary,
  sumExact,
  windowEnd,
  windowOf,
  taskAudit,
} from "./server/map";
import type {
  RawApproval,
  RawBreakerDisable,
  RawBreakerEnable,
  RawBreakerReset,
  RawBreakers,
  RawBudget,
  RawHalt,
  RawProposals,
  RawHealth,
  RawLedgerPage,
  RawRunDetail,
  RawRunList,
  RawStopResponse,
  RawVerify,
  RawTaskAudit,
} from "./server/raw";
import type {
  Approval,
  BudgetSummary,
  ChainStatus,
  DisableReason,
  LedgerEntry,
  LedgerFilter,
  ModelSpend,
  RunStatus,
  RunSummary,
  AgentCredential,
  AgentScope,
  IssuedAgentCredential,
} from "./types";

export interface ServerClientOptions {
  /** "" = this origin (the dev server proxies /v1); otherwise the server's address. */
  baseUrl?: string;
  fetch?: FetchFn;
  /** Cross-tab sign-in signal (tests pass a fake; the default is the browser's). */
  tabSync?: TabSync;
  /** Opens a WebSocket; the default uses the browser's. */
  socket?: SocketFactory;
  now?: () => Date;
}

/** Server run states for the filter; `failed` also covers `aborted`, so it filters here instead. */
const SERVER_STATUS: Partial<Record<RunStatus, string>> = {
  running: "running",
  stopping: "stopping",
  waiting: "needs_human",
  done: "done",
  stopped: "stopped",
};

/** Writes the server honours an Idempotency-Key on (not the kill switch, not secret-bearing routes). */
const KEYED = { keyed: true } as const;
const LIST_LIMIT = 200;
const LEDGER_STREAMS = 12;
const LEDGER_PER_STREAM = 200;
const DETAIL_RUNS = 50;
const MAX_EVENT_PAGES = 40;

function wsUrl(base: string, path: string): string {
  if (base === "") {
    const here = globalThis.location;
    return `${here.protocol === "https:" ? "wss:" : "ws:"}//${here.host}${path}`;
  }
  return `${base.replace(/^http/, "ws")}${path}`;
}

/**
 * The client for `nuramesh serve` (the API notes): REST under /v1 and the per-run WebSocket. What
 * the server has no endpoint for (organization storage, templates, presets, starting or pausing a
 * run) rejects with `NotAvailableError`, so no screen ever shows invented data.
 */
/** A credential as the server sends it (the agent identity notes section 7). */
interface RawAgentCredential {
  id: string;
  agent: string;
  scopes: string[];
  active: boolean;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  issuer_erased: boolean;
  in_config: boolean;
}

function agentCredential(raw: RawAgentCredential): AgentCredential {
  return {
    id: raw.id,
    agent: raw.agent,
    scopes: raw.scopes.filter((x): x is AgentScope => x === "read" || x === "run"),
    state: raw.revoked_at !== null ? "revoked" : raw.active ? "active" : "expired",
    createdAt: raw.created_at,
    expiresAt: raw.expires_at,
    revokedAt: raw.revoked_at,
    issuerErased: raw.issuer_erased,
    inConfig: raw.in_config,
  };
}

function issuedCredential(raw: RawAgentCredential & { credential: string }): IssuedAgentCredential {
  return { ...agentCredential(raw), secret: raw.credential };
}

interface RawInvite {
  id: string;
  email: string | null;
  role: InviteRole;
  status: Invite["status"];
  created_by: string;
  created_at: string;
  expires_at: string;
  accepted_user_id: string | null;
  resent_count: number;
}

function invite(raw: RawInvite): Invite {
  return {
    id: raw.id,
    email: raw.email,
    role: raw.role,
    status: raw.status,
    createdBy: raw.created_by,
    createdAt: raw.created_at,
    expiresAt: raw.expires_at,
    acceptedUserId: raw.accepted_user_id,
    resentCount: raw.resent_count,
  };
}

export function createServerClient(options: ServerClientOptions = {}): ApiClient {
  const fetchFn: FetchFn = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
  const auth = createServerAuth({ baseUrl: options.baseUrl ?? "", fetch: fetchFn, sync: options.tabSync ?? browserTabSync() });
  const http: HttpClient = createHttp(options.baseUrl ?? "", fetchFn, { csrf: auth.csrf, recover: auth.recover, refreshCsrf: auth.refreshCsrf });
  const now = options.now ?? (() => new Date());
  const socket: SocketFactory =
    options.socket ??
    ((url) => new WebSocket(url) as unknown as ReturnType<SocketFactory>);
  const unavailable = (what: string) => (): Promise<never> => Promise.reject(new NotAvailableError(what));

  // Run details are re-read only when the run's head changed (the list carries the head).
  const detailCache = new Map<string, { head: string; raw: RawRunDetail }>();
  async function detailOf(id: string, head?: string): Promise<RawRunDetail> {
    const cached = detailCache.get(id);
    if (cached !== undefined && head !== undefined && cached.head === head) return cached.raw;
    const raw = await http.get<RawRunDetail>(`/v1/runs/${encodeURIComponent(id)}`);
    detailCache.set(id, { head: raw.head, raw });
    return raw;
  }

  async function runList(status?: string): Promise<RawRunList> {
    const query = `limit=${String(LIST_LIMIT)}${status === undefined ? "" : `&status=${status}`}`;
    return http.get<RawRunList>(`/v1/runs?${query}`);
  }

  async function allDetails(): Promise<RawRunDetail[]> {
    const list = await runList();
    return Promise.all(list.runs.slice(0, DETAIL_RUNS).map((r) => detailOf(r.run_id, r.head)));
  }

  async function approvalWithSpend(id: string): Promise<Approval> {
    const raw = await http.get<RawApproval>(`/v1/approvals/${encodeURIComponent(id)}`);
    let spend: Approval["runSpend"] = null;
    let startedByYou = false;
    try {
      const run = await detailOf(raw.run_id);
      const limit = run.budgets.find((b) => b.scope === "run" && b.scope_id === raw.run_id)?.limit;
      spend = { spent: money(run.spend.total), limit: moneyOrNull(limit) };
      const who = auth.state();
      startedByYou = who.kind === "signed-in" && run.started_by === who.user.id;
    } catch {
      spend = null; // the run's numbers are a courtesy; the request itself still shows
    }
    // The solo confirm dialog names the agent's role (SA-17); a courtesy too, read only for a sole decider.
    let role: string | null = null;
    if (raw.caller_terms?.mode === "solo") {
      try {
        const { agents } = await http.get<{ agents: { id: string; role: string }[] }>("/v1/agents");
        role = agents.find((a) => a.id === raw.agent_id)?.role ?? null;
      } catch {
        role = null;
      }
    }
    return { ...approval(raw, spend), role, startedByYou };
  }

  async function verifyAll(): Promise<ChainStatus> {
    let after = "";
    let merged: RawVerify | null = null;
    for (let page = 0; page < 20; page += 1) {
      const raw: RawVerify = await http.get(`/v1/ledger/verify?limit=100${after === "" ? "" : `&after=${encodeURIComponent(after)}`}`);
      merged =
        merged === null
          ? raw
          : { ...raw, ok: merged.ok && raw.ok, chain_ok: merged.chain_ok && raw.chain_ok, verified: merged.verified && raw.verified, streams: [...(merged.streams ?? []), ...(raw.streams ?? [])] };
      if (raw.truncated !== true || raw.next_after === null || raw.next_after === undefined) break;
      after = raw.next_after;
    }
    return chainStatus(merged ?? { ok: true, chain_ok: true, verified: false, streams: [] }, now());
  }

  async function ledgerEntries(): Promise<LedgerEntry[]> {
    const toc = await http.get<{ streams: { stream: string; seq: number; updated: string }[] }>("/v1/ledger");
    const newest = [...toc.streams].sort((a, b) => b.updated.localeCompare(a.updated)).slice(0, LEDGER_STREAMS);
    const pages = await Promise.all(
      newest.map((s) =>
        http.get<RawLedgerPage>(`/v1/ledger/${encodeURIComponent(s.stream)}?after_seq=${String(Math.max(0, s.seq - LEDGER_PER_STREAM))}&limit=${String(LEDGER_PER_STREAM)}`),
      ),
    );
    return pages
      .flatMap((p) => p.events.map(ledgerEntry))
      .sort((a, b) => b.at.localeCompare(a.at) || b.n.localeCompare(a.n, undefined, { numeric: true }));
  }

  async function listPage(filter?: { status?: RunStatus }): Promise<RunPage> {
    const wanted = filter?.status;
    const server = wanted === undefined ? undefined : SERVER_STATUS[wanted];
    const list = await runList(server);
    let runs: RunSummary[] = list.runs.map(runSummary);
    if (wanted !== undefined) runs = runs.filter((r) => r.status === wanted);
    return { runs, truncated: list.truncated };
  }

  const client: ApiClient = {
    source: "server",
    auth,
    orgs: { get: unavailable("orgs.get"), update: unavailable("orgs.update") },
    config: {
      validate: unavailable("config.validate"),
      export: unavailable("config.export"),
      import: unavailable("config.import"),
    },
    templates: {
      list: unavailable("templates.list"),
      get: unavailable("templates.get"),
      use: unavailable("templates.use"),
      saveAs: unavailable("templates.saveAs"),
      listVersions: unavailable("templates.listVersions"),
      versionYaml: unavailable("templates.versionYaml"),
    },
    policyPresets: {
      list: unavailable("policyPresets.list"),
      get: unavailable("policyPresets.get"),
      create: unavailable("policyPresets.create"),
      update: unavailable("policyPresets.update"),
      assign: unavailable("policyPresets.assign"),
    },
    runs: {
      list: async () => (await listPage()).runs,
      listPage,
      get: async (id) => runDetail(await detailOf(id), now()),
      reportUrl: (id) => `${http.baseUrl}/v1/runs/${encodeURIComponent(id)}/report`,
      taskAudit: async (runId, taskId) =>
        taskAudit(await http.get<RawTaskAudit>(`/v1/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/audit`)),
      start: unavailable("runs.start"),
      pause: unavailable("runs.pause"),
      stop: async (id, reason) => {
        const path = `/v1/runs/${encodeURIComponent(id)}/stop`;
        const trimmed = reason?.trim() ?? "";
        const answer = await http.post<RawStopResponse>(path, trimmed === "" ? {} : { reason: trimmed }, KEYED);
        // The stop is written. If reading the run back fails, the stop still happened: say so and let
        // the page refresh (never "couldn't reach the server" for a request that went through).
        try {
          const fresh = await http.get<RawRunDetail>(`/v1/runs/${encodeURIComponent(id)}`);
          detailCache.set(id, { head: fresh.head, raw: fresh });
          return { result: runSummary({ ...fresh, tasks: fresh.task_counts }), ledgerEntry: String(answer.ledger_seq) };
        } catch {
          const known = detailCache.get(id)?.raw;
          const base: RunSummary =
            known === undefined
              ? { id, label: runLabel(id), title: `Run ${id}`, template: "", status: "stopping", startedAt: null, startedBy: "unknown", tasks: { done: 0, total: 0 }, gates: { passed: 0, failed: 0, total: 0 }, spend: { spent: { amount: "0", currency: "USD" }, limit: null }, ended: false }
              : runSummary({ ...known, tasks: known.task_counts });
          return { result: { ...base, status: "stopping", ended: false, stopRequestedBy: answer.requested_by, ...(answer.stop_reason_withheld ? { stopReasonWithheld: answer.stop_reason_withheld } : {}) }, ledgerEntry: String(answer.ledger_seq) };
        }
      },
      events: async (id, afterSeq = 0) => {
        const out: ReturnType<typeof runEvent>[] = [];
        let cursor = afterSeq;
        for (let page = 0; page < MAX_EVENT_PAGES; page += 1) {
          const raw = await http.get<RawLedgerPage & { has_more: boolean }>(`/v1/runs/${encodeURIComponent(id)}/ledger?after_seq=${String(cursor)}&limit=500`);
          out.push(...raw.events.map(runEvent));
          cursor = Math.max(cursor, raw.next_after_seq);
          if (!raw.has_more) break;
        }
        return { events: out, nextSeq: cursor };
      },
      subscribe: (id, onEvent, subscribeOptions) =>
        connectLive({
          url: wsUrl(http.baseUrl, `/v1/runs/${encodeURIComponent(id)}/events`),
          afterSeq: subscribeOptions?.afterSeq ?? 0,
          onEvent,
          ...(subscribeOptions?.onStatus === undefined ? {} : { onStatus: subscribeOptions.onStatus }),
          socket,
          onUnauthorized: auth.recover,
        }),
    },
    approvals: {
      list: async () => (await http.get<RawApproval[]>("/v1/approvals")).map((a) => approval(a, null)),
      get: approvalWithSpend,
      decide: async (id, input) => {
        const approve = input.decision === "approve";
        await http.post(
          `/v1/approvals/${encodeURIComponent(id)}/${approve ? "approve" : "reject"}`,
          {
            reason: input.reason,
            // `solo` is set only by the solo approve dialog's confirm (SA-17), with the digest of what it showed (SA-5). A reject never carries it (SA-16).
            ...(approve && input.solo !== undefined ? { acknowledge_solo: true, action_digest: input.solo.actionDigest } : {}),
          },
          KEYED,
        );
        const result = await approvalWithSpend(id);
        return { result, ledgerEntry: result.decision === null ? null : (parseEntryId(result.decision.ledgerEntry)?.seq.toString() ?? null) };
      },
    },
    gates: {
      decide: async (runId, taskId, gateId, input) => {
        const path = `/v1/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/gates/${encodeURIComponent(gateId)}/decisions`;
        const raw = await http.post<{ outcome: string; already_recorded?: boolean }>(path, {
          attempt: input.attempt,
          submission: input.submission,
          findings: input.findings,
          // SA-19: the only solo term of a gate. No digest: a gate has none.
          ...(input.acknowledgeSolo === true ? { acknowledge_solo: true } : {}),
        }, KEYED);
        return { outcome: raw.outcome, alreadyRecorded: raw.already_recorded === true };
      },
    },
    autonomy: {
      proposals: async (role, agent) =>
        proposalList(await http.get<RawProposals>(`/v1/autonomy/proposals?role=${encodeURIComponent(role)}${agent === undefined ? "" : `&agent=${encodeURIComponent(agent)}`}`)),
      grant: async (input) => {
        await http.post("/v1/autonomy/grants", {
          subject: input.subject,
          rule_key: input.ruleKey,
          as_of: input.asOf,
          ledger_position: input.ledgerPosition,
          evidence_hash: input.evidenceHash,
          reason: input.reason,
          ...(input.expiresInDays === undefined ? {} : { expires_in_days: input.expiresInDays }),
        }, KEYED);
      },
    },
    budgets: {
      list: async () => (await http.get<RawBudget[]>("/v1/budgets")).map(budgetRow),
      summary: async () => {
        const [budgets, runs, details] = await Promise.all([
          http.get<RawBudget[]>("/v1/budgets"),
          runList(),
          allDetails(),
        ]);
        const org =
          budgets.find((b) => b.scope === "org" && windowOf(b.window) === "month") ??
          budgets.find((b) => b.scope === "org" && windowOf(b.window) === "day") ??
          budgets.find((b) => b.scope === "org");
        const tasks = details.flatMap((d) => d.tasks);
        const verified = tasks.filter((t) => t.status === "done");
        const unfinished = tasks.filter((t) => t.status !== "done");
        // Sums and the average use the server's exact amounts; only the end result is rounded to six places.
        const verifiedSpend = sumExact(verified.map((t) => t.spend.total));
        const average =
          verifiedSpend === null || verified.length === 0
            ? { amount: "0", currency: "USD" }
            : divideExact(toMicros(verifiedSpend), BigInt(verified.length), 6, verifiedSpend.currency);
        const blocked = budgets.find((b) => b.blocked);
        let breach: BudgetSummary["breach"] = null;
        if (blocked !== undefined) {
          const detail = await http.get<RawBudget>(`/v1/budgets/${encodeURIComponent(blocked.id)}`);
          const last = [...(detail.history ?? [])].reverse().find((h) => h.type === "budget.exceeded" || h.type === "budget.breached");
          const window = windowOf(blocked.window);
          const onBreach = blocked.on_breach === "reset" ? "reset" : "hold";
          breach = {
            budgetId: blocked.id,
            scope: budgetRow(blocked).scope,
            spent: money(blocked.spent),
            limit: money(blocked.limit),
            since: last?.at ?? null,
            ledgerEntry: last === undefined ? "" : String(last.ledger_seq),
            nextCall: null,
            window,
            onBreach,
            resetsAt: onBreach === "reset" ? windowEnd(window, blocked.window_start) : null,
          };
        }
        const statusCount = (...names: string[]): number => runs.runs.filter((r) => names.includes(r.status)).length;
        const orgRow = org === undefined ? null : budgetRow(org);
        return {
          orgMonth: orgRow === null ? null : { spent: orgRow.spent, limit: orgRow.limit },
          orgWindow: orgRow?.window ?? "none",
          costPerVerifiedTask: average,
          spentOnFailedAttempts: sumExact(unfinished.map((t) => t.spend.total)) ?? { amount: "0", currency: "USD" },
          verifiedTasks: verified.length,
          runs: {
            total: runs.runs.length,
            finished: statusCount("done", "stopped", "failed", "aborted", "needs_human"),
            running: statusCount("running", "stopping"),
            blocked: budgets.filter((b) => b.blocked).length,
          },
          unknownPriceRefusals: null,
          coverage: details.length < runs.runs.length || runs.truncated ? `the newest ${String(details.length)} runs` : `all ${String(details.length)} runs`,
          unknownCostCalls: details.reduce((n, d) => n + (d.spend.unknown_cost_calls ?? 0), 0),
          breach,
        } satisfies BudgetSummary;
      },
      unblock: async (budgetId, input) => {
        const path = `/v1/budgets/${encodeURIComponent(budgetId)}`;
        await http.post(`${path}/unblock`, { reason: input.reason, ...(input.newLimit === undefined ? {} : { new_limit: input.newLimit }) }, KEYED);
        const detail = await http.get<RawBudget>(path);
        const last = [...(detail.history ?? [])].reverse().find((h) => h.type === "budget.unblocked");
        return { result: budgetRow(detail), ledgerEntry: last === undefined ? null : String(last.ledger_seq) };
      },
    },
    spend: {
      byModel: async () => {
        const details = await allDetails();
        const totals = new Map<string, { amounts: { amount: string; currency: string }[]; calls: number }>();
        for (const d of details) {
          for (const m of d.spend.by_model ?? []) {
            const entry = totals.get(m.model) ?? { amounts: [], calls: 0 };
            totals.set(m.model, { amounts: [...entry.amounts, m.total], calls: entry.calls + m.calls });
          }
        }
        const priced = [...totals.entries()].map(([name, t]) => ({ name, calls: t.calls, cost: sumExact(t.amounts, t.amounts[0]?.currency ?? "USD") ?? { amount: "0", currency: "USD" } }));
        const sum = priced.reduce((n, p) => n + toMicros(p.cost), 0n);
        const rows: ModelSpend[] = priced
          .sort((a, b) => (toMicros(b.cost) > toMicros(a.cost) ? 1 : toMicros(b.cost) < toMicros(a.cost) ? -1 : 0))
          .map((p) => ({
            name: p.name,
            meta: `${String(p.calls)} call${p.calls === 1 ? "" : "s"}`,
            cost: p.cost,
            sharePercent: sum === 0n ? 0 : Number((toMicros(p.cost) * 100n) / sum),
            selfHosted: false,
          }));
        return rows;
      },
    },
    reports: {
      downgrade: async (options) =>
        downgradeReport(
          await http.get<RawDowngradeReport>(
            `/v1/reports/downgrade${options?.minSample === undefined ? "" : `?min_sample=${String(options.minSample)}`}`,
          ),
        ),
    },
    savings: {
      tokens: async (options) =>
        tokenSavings(await http.get<RawTokenSavings>(`/v1/savings/tokens${options?.groupBy === undefined || options.groupBy === "none" ? "" : `?group_by=${options.groupBy}`}`)),
    },
    dashboards: {
      verification: (w) => http.get<VerificationDashboard>(dashboardPath("verification", w)),
      verifiers: (w) => http.get<VerifiersDashboard>(dashboardPath("verifiers", w)),
      costLatency: (w) => http.get<CostLatencyDashboard>(dashboardPath("cost-latency", w)),
      approvals: (w) => http.get<ApprovalsDashboard>(dashboardPath("approvals", w)),
    },
    models: createModelsApi(http),
    session: createSessionApi(http),
    ledger: {
      list: async (filter?: LedgerFilter) => {
        const needle = filter?.search?.trim().toLowerCase() ?? "";
        return (await ledgerEntries()).filter(
          (e) =>
            (filter?.category === undefined || e.category === filter.category) &&
            (needle === "" || [e.n, e.actor, e.kind, e.summary, e.stream, e.taskId ?? ""].some((v) => v.toLowerCase().includes(needle))),
        );
      },
      get: async (n) => {
        const id = parseEntryId(n);
        if (id === null) throw new NotFoundError(`ledger entry ${n}`);
        const page = await http.get<RawLedgerPage>(`/v1/ledger/${encodeURIComponent(id.stream)}?after_seq=${String(id.seq - 1)}&limit=1`);
        const found = page.events.find((e) => e.seq === id.seq);
        if (found === undefined) throw new NotFoundError(`ledger entry ${n}`);
        return ledgerEntry(found);
      },
      verifyChain: verifyAll,
      chainStatus: verifyAll,
      verifyRun: async (runId) => chainStatus(await http.get<RawVerify>(`/v1/runs/${encodeURIComponent(runId)}/ledger/verify`), now()),
    },
    org: {
      status: async () => orgStatus(await http.get<RawHalt>("/v1/org/status")),
      halt: async () => {
        const raw = await http.post<RawHalt>("/v1/org/halt", {});
        return { ...orgStatus(raw), stoppedRuns: raw.stopped_runs ?? 0 };
      },
      resume: async () => orgStatus(await http.post<RawHalt>("/v1/org/resume", {}, KEYED)),
      pauseRole: async (role) => {
        const raw = await http.post<RawHalt>(`/v1/org/roles/${encodeURIComponent(role)}/pause`, {});
        return { ...orgStatus(raw), stoppedTasks: raw.stopped_tasks ?? 0 };
      },
      resumeRole: async (role) => orgStatus(await http.post<RawHalt>(`/v1/org/roles/${encodeURIComponent(role)}/resume`, {}, KEYED)),
    },
    breakers: {
      list: async () => breakersView(await http.get<RawBreakers>("/v1/org/breakers")),
      reset: async (name) => {
        const raw = await http.post<RawBreakerReset>(`/v1/org/breakers/${encodeURIComponent(name)}/reset`, {}, KEYED);
        return { name: raw.name, state: "ok", resetAt: raw.reset_at, ledgerSeq: raw.ledger_seq, orgHalted: raw.org_halted };
      },
      disable: async (name, reason) => {
        const raw = await http.post<RawBreakerDisable>(`/v1/org/breakers/${encodeURIComponent(name)}/disable`, { reason }, KEYED);
        return { name: raw.name, state: "disabled", disabledAt: raw.disabled_at, reason: raw.reason as DisableReason, ledgerSeq: raw.ledger_seq };
      },
      enable: async (name) => {
        const raw = await http.post<RawBreakerEnable>(`/v1/org/breakers/${encodeURIComponent(name)}/enable`, {}, KEYED);
        return { name: raw.name, state: "ok", enabledAt: raw.enabled_at, ledgerSeq: raw.ledger_seq };
      },
    },
    agentCredentials: {
      agents: async () =>
        (await http.get<{ agents: { id: string; name: string; role: string; active_credentials: number }[] }>("/v1/agents")).agents.map((a) => ({
          id: a.id,
          name: a.name,
          role: a.role,
          activeCredentials: a.active_credentials,
        })),
      callerRole: async () => {
        try {
          const role = (await http.get<{ user: { role: string } }>("/v1/auth/session")).user.role;
          return role === "owner" || role === "approver" || role === "viewer" ? role : null;
        } catch {
          return null;
        }
      },
      list: async (filter) => {
        const path =
          filter?.agent !== undefined
            ? `/v1/agents/${encodeURIComponent(filter.agent)}/credentials`
            : `/v1/agent-credentials${filter?.issuerErased === true ? "?issuer_erased=true" : ""}`;
        return (await http.get<{ credentials: RawAgentCredential[] }>(path)).credentials.map(agentCredential);
      },
      issue: async (agent, input) =>
        issuedCredential(await http.post<RawAgentCredential & { credential: string }>(`/v1/agents/${encodeURIComponent(agent)}/credentials`, { scopes: input.scopes, expires_in_days: input.days })),
      rotate: async (agent, id, input) =>
        issuedCredential(
          await http.post<RawAgentCredential & { credential: string }>(
            `/v1/agents/${encodeURIComponent(agent)}/credentials/${encodeURIComponent(id)}/rotate`,
            { expires_in_days: input.days, grace_seconds: input.graceMinutes * 60 },
          ),
        ),
      revoke: async (agent, id) =>
        agentCredential(await http.post<RawAgentCredential>(`/v1/agents/${encodeURIComponent(agent)}/credentials/${encodeURIComponent(id)}/revoke`, {}, KEYED)),
      revokeAll: async (agent) => (await http.post<{ revoked: number }>(`/v1/agents/${encodeURIComponent(agent)}/credentials/revoke-all`, {}, KEYED)).revoked,
    },
    emailChange: {
      start: async (input) => {
        await http.post<{ status: string }>("/v1/auth/me/email", { new_email: input.newEmail, password: input.password });
      },
      verify: async (token) => {
        await http.post<{ status: string }>("/v1/auth/email/verify", { token });
      },
    },
    firstRun: {
      runs: () => http.get<unknown>("/v1/runs?limit=1"),
      approvals: () => http.get<unknown>("/v1/approvals?limit=50"),
      invites: () => http.get<unknown>("/v1/auth/invites?limit=1"),
    },
    invites: {
      list: async () => (await http.get<{ invites: RawInvite[] }>("/v1/auth/invites")).invites.map(invite),
      create: async (input) => invite((await http.post<{ invite: RawInvite }>("/v1/auth/invites", input)).invite),
      revoke: async (id) =>
        invite((await http.post<{ invite: RawInvite }>(`/v1/auth/invites/${encodeURIComponent(id)}/revoke`, {})).invite),
      resend: async (id) =>
        invite((await http.post<{ invite: RawInvite }>(`/v1/auth/invites/${encodeURIComponent(id)}/resend`, {})).invite),
      inspect: async (token) => {
        const raw = await http.post<{ email: string; role: InviteRole; organization: string; expires_at: string }>(
          "/v1/auth/invites/inspect",
          { token },
        );
        return { email: raw.email, role: raw.role, organization: raw.organization, expiresAt: raw.expires_at };
      },
      accept: async (input) =>
        (
          await http.post<{ user: { id: string; role: InviteRole; email: string } }>("/v1/auth/invites/accept", {
            token: input.token,
            password: input.password,
            ...(input.displayName === undefined || input.displayName.trim() === "" ? {} : { display_name: input.displayName.trim() }),
          })
        ).user,
    },
    passwordReset: {
      request: async (email) => {
        await http.post("/v1/auth/password-reset/request", { email });
      },
      redeem: async (input) => {
        await http.post("/v1/auth/password-reset/redeem", { token: input.token, password: input.password });
      },
      revokeByLink: async (token) => {
        await http.post("/v1/auth/tokens/revoke-by-link", { token });
      },
    },
    info: async (): Promise<SourceInfo> => {
      const health = await http.get<RawHealth>("/v1/health");
      return {
        name: health.org ?? "NuraMesh server",
        detail: `${http.baseUrl === "" ? "this host" : http.baseUrl.replace(/^https?:\/\//, "")} · ${health.humans_enabled ? "people can act" : "read-only"}`,
      };
    },
  };
  return client;
}
