/**
 * The one interface every screen talks to (SPEC-app-v1 §4). There are two implementations:
 *
 * - the **fixture client** (`fixture-client.ts`): in memory, seeded from the design data, stateful
 *   (deciding an approval really changes it), for development, tests and the screenshots;
 * - the **server client** (`server-client.ts`): the REST API and WebSocket of `nuramesh serve`,
 *   wired endpoint by endpoint as the server track lands (see README.md).
 *
 * Both must pass the same contract tests (`contract.ts`), so switching between them never needs a
 * UI change. Every method rejects with an `ApiError` subclass; screens branch on the class.
 */
import type {
  GateDecideInput,
  GateDecision,
  GrantInput,
  ProposalList,
  AgentCredential,
  AgentSummary,
  CallerRole,
  AgentScope,
  IssuedAgentCredential,
  Approval,
  BreakerDisableResult,
  BreakerEnableResult,
  BreakerResetResult,
  BreakersView,
  BudgetRow,
  BudgetSummary,
  ChainStatus,
  DecideInput,
  DisableReason,
  ExportFormat,
  LedgerEntry,
  LedgerFilter,
  ModelSpend,
  Mutation,
  OrgSnapshot,
  OrgStatus,
  PolicyPreset,
  GatePreset,
  LiveStatus,
  PresetSet,
  RunDetail,
  TaskAudit,
  RunEvent,
  RunStatus,
  RunSummary,
  StartRunInput,
  TemplateDetail,
  TemplateSummary,
  TemplateVersionInfo,
  ValidationResult,
} from "./types";
import type { ModelsApi, SessionApi } from "./models";
import type { DashboardsApi } from "./dashboards";
import type { EmailChangeApi } from "./email-change";
import type { InvitesApi } from "./invites";
import type { PasswordResetApi } from "./password-reset";
import type { ReportsApi } from "./reports";
import type { SavingsApi } from "./savings";
import type { AuthApi } from "@/auth/types";
import type { Org } from "@/model/org";

/** Base class for everything the client rejects with. Calls never throw synchronously. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = new.target.name;
  }
}

/** The thing asked for doesn't exist. */
export class NotFoundError extends ApiError {
  constructor(what: string) {
    super(`${what} was not found`, "not_found");
  }
}

/** The request was refused as invalid (a missing reason, a bad id...). `fields` says which. */
export class InvalidRequestError extends ApiError {
  constructor(
    message: string,
    readonly fields: { path: string; message: string }[] = [],
  ) {
    super(message, "invalid_request");
  }
}

/** The thing changed since it was read (optimistic concurrency); reload and try again. */
export class ConflictError extends ApiError {
  /** `code` is the server's state code when it gives one (`already_halted`, `not_tripped`...). */
  constructor(message: string, code = "conflict") {
    super(message, code);
  }
}

/** Not allowed in the current state (already decided, a planned template, ...). */
export class NotAllowedError extends ApiError {
  /** `code` is the server's code when it gives one (`human_required`, `forbidden`). */
  constructor(message: string, code = "not_allowed") {
    super(message, code);
  }
}

/**
 * The server refused with a problem the screens report plainly. `title` is the server's code
 * (`run_finished`, `content_store_unavailable`, `authentication_required`...);
 * `status` is the HTTP status, or 0 when the server could not be reached at all (`unreachable`).
 */
export class ProblemError extends ApiError {
  constructor(
    message: string,
    readonly status: number,
    readonly title: string,
    /** Whole seconds the server asked the caller to wait (`Retry-After` on a 429), if it said. */
    readonly retryAfter?: number,
  ) {
    super(message, title);
  }
}

/** This client doesn't do that yet. The server client throws it for endpoints not wired. */
export class NotAvailableError extends ApiError {
  constructor(what: string) {
    super(`${what} isn't available from this data source yet`, "not_available");
  }
}

export type Unsubscribe = () => void;

/** Options for a live subscription. */
export interface SubscribeOptions {
  /** Replay everything after this entry number first (a reconnect passes the last one it saw). */
  afterSeq?: number;
  /** Told when the connection comes up, drops, retries or is closed for good. */
  onStatus?: (status: LiveStatus) => void;
}

/** What the sidebar says about the data source. */
export interface SourceInfo {
  /** The organization's name. */
  name: string;
  /** A short note under the name ("main", "127.0.0.1:8765"). */
  detail: string;
}

export interface OrgsApi {
  get(): Promise<OrgSnapshot>;
  /** Replaces the organization. `expectedHash` is the `versionHash` the edit started from. */
  update(org: Org, expectedHash: string): Promise<Mutation<OrgSnapshot>>;
}

export interface ConfigApi {
  validate(org: Org): Promise<ValidationResult>;
  /** The organization as text. YAML needs the server or the builder's YAML support. */
  export(format: ExportFormat): Promise<string>;
  import(text: string, format: ExportFormat): Promise<Mutation<OrgSnapshot>>;
}

export interface TemplatesApi {
  list(): Promise<TemplateSummary[]>;
  /** One template with its saved versions (none for the built-in ones). */
  get(id: string): Promise<TemplateDetail>;
  /** The org a template makes (the latest version unless one is given). */
  use(id: string, version?: number): Promise<Org>;
  /** Saves the current organization as the next version of a template (a new id for a new one). */
  saveAs(input: { id: string; name: string; description: string }): Promise<
    Mutation<TemplateVersionInfo>
  >;
  listVersions(id: string): Promise<TemplateVersionInfo[]>;
  /** The YAML of one saved version, for "Preview YAML" and Git export. */
  versionYaml(id: string, version: number): Promise<string>;
}

export interface PolicyPresetsApi {
  list(): Promise<PresetSet>;
  get(id: string): Promise<PolicyPreset | GatePreset>;
  create(preset: Omit<PolicyPreset, "usedByRoles">): Promise<Mutation<PolicyPreset>>;
  /** Editing a preset changes every agent whose role uses it; the ledger records it. */
  update(
    id: string,
    patch: Partial<Pick<PolicyPreset, "name" | "description" | "rules">>,
  ): Promise<Mutation<PolicyPreset>>;
  /** Applies a preset to a role (so to its agents); `null` removes the role's preset of that kind. */
  assign(role: string, presetId: string | null, kind: "policy" | "gate"): Promise<Mutation<PresetSet>>;
}

/** A page of runs: `truncated` means a bound stopped the search before every run was found. */
export interface RunPage {
  runs: RunSummary[];
  truncated: boolean;
}

export interface RunsApi {
  list(): Promise<RunSummary[]>;
  /** The runs, most recently active first, optionally only those in one status. */
  listPage(filter?: { status?: RunStatus }): Promise<RunPage>;
  get(id: string): Promise<RunDetail>;
  /** Where `GET /v1/runs/{run_id}/report` can be opened (U10); absent on a source with no server (sample data). */
  reportUrl?(id: string): string;
  /** Everything that happened to one task, in order, with evidence (R1.27); read only. */
  taskAudit(runId: string, taskId: string): Promise<TaskAudit>;
  start(input: StartRunInput): Promise<Mutation<RunSummary>>;
  pause(id: string): Promise<Mutation<RunSummary>>;
  /**
   * Asks the run to stop. The reason (optional) is kept in the content store, the ledger holds its
   * hash. An in-flight model call finishes and is charged; the run is `stopping` until it ends.
   */
  stop(id: string, reason?: string): Promise<Mutation<RunSummary>>;
  /** Events after `afterSeq` (oldest first), for polling and for catching up. */
  events(id: string, afterSeq?: number): Promise<{ events: RunEvent[]; nextSeq: number }>;
  /** Live events as they happen (the server's WebSocket); returns the way to stop listening. */
  subscribe(id: string, onEvent: (event: RunEvent) => void, options?: SubscribeOptions): Unsubscribe;
}

export interface ApprovalsApi {
  list(): Promise<Approval[]>;
  get(id: string): Promise<Approval>;
  /** A reason is required. The requesting agent can never decide its own request. */
  decide(id: string, input: DecideInput): Promise<Mutation<Approval>>;
}

export interface GatesApi {
  /** A person's verdict on a human gate. Never sends a digest: a gate has none (SA-19). */
  decide(runId: string, taskId: string, gateId: string, input: GateDecideInput): Promise<GateDecision>;
}

export interface AutonomyApi {
  proposals(role: string, agent?: string): Promise<ProposalList>;
  /** Grants autonomy to the subject of a proposal item. A reason is required. */
  grant(input: GrantInput): Promise<void>;
}

export interface BudgetsApi {
  list(): Promise<BudgetRow[]>;
  summary(): Promise<BudgetSummary>;
  /** Lifts a block. A reason is required and goes to the ledger; `newLimit` raises the limit. */
  unblock(
    budgetId: string,
    input: { reason: string; newLimit?: string },
  ): Promise<Mutation<BudgetRow>>;
}

export interface SpendApi {
  byModel(): Promise<ModelSpend[]>;
}

export interface LedgerApi {
  /** Newest first. */
  list(filter?: LedgerFilter): Promise<LedgerEntry[]>;
  get(n: string): Promise<LedgerEntry>;
  /** Re-checks the hash chain now. */
  verifyChain(): Promise<ChainStatus>;
  chainStatus(): Promise<ChainStatus>;
  /** Chain and anchor check of one run's stream. */
  verifyRun(runId: string): Promise<ChainStatus>;
}

/**
 * The organization's kill switch and role pause (E19, ND106). Every write needs a signed-in person:
 * any other caller gets `NotAllowedError` (`human_required` or `forbidden`). Repeating a write
 * answers `ConflictError` with the server's code (`already_halted`, `already_running`,
 * `already_paused`, `not_paused`); the screens treat that as "already done" and refetch.
 */
export interface OrgSafetyApi {
  status(): Promise<OrgStatus>;
  /** Halts the whole organization; `stoppedRuns` is how many open runs were asked to stop. */
  halt(): Promise<OrgStatus & { stoppedRuns: number }>;
  resume(): Promise<OrgStatus>;
  /** Pauses one role; `stoppedTasks` is how many of its running tasks were asked to stop. */
  pauseRole(role: string): Promise<OrgStatus & { stoppedTasks: number }>;
  resumeRole(role: string): Promise<OrgStatus>;
}

/** Circuit breakers. Reset, disable and enable loosen protection, so they are a person's alone. */
export interface BreakersApi {
  list(): Promise<BreakersView>;
  /** `ConflictError` code: `not_tripped`. */
  reset(name: string): Promise<BreakerResetResult>;
  /** `ConflictError` codes: `already_disabled`, `breaker_tripped`. */
  disable(name: string, reason: DisableReason): Promise<BreakerDisableResult>;
  /** `ConflictError` code: `not_disabled`. */
  enable(name: string): Promise<BreakerEnableResult>;
}

/**
 * Per-agent credentials (E2, spec/agent-identity section 7). Owner only: any other caller gets
 * `NotAllowedError` (`forbidden`), and the screens show "Only an owner can manage agent access.".
 * Issue and rotate answer `ConflictError` `agent_suspended` while the organization is halted or the
 * agent's role is paused, and `credential_limit` at the maximum; revoke always works.
 */
export interface AgentCredentialsApi {
  /** The team's agents (owner only; the server refuses anyone else). */
  agents(): Promise<AgentSummary[]>;
  /** The signed-in person's role, or `null` when the app cannot tell (then the server decides). */
  callerRole(): Promise<CallerRole | null>;
  /** The organization's credentials; `issuerErased` keeps the live ones whose issuer was erased. */
  list(filter?: { agent?: string; issuerErased?: boolean }): Promise<AgentCredential[]>;
  issue(agent: string, input: { scopes: AgentScope[]; days: number }): Promise<IssuedAgentCredential>;
  rotate(agent: string, id: string, input: { days: number; graceMinutes: number }): Promise<IssuedAgentCredential>;
  revoke(agent: string, id: string): Promise<AgentCredential>;
  revokeAll(agent: string): Promise<number>;
}

/**
 * The three bounded reads of the first-run checklist (spec/first-run/checklist.md FR-4). Each returns the
 * server's raw body (the checklist parses it and treats any other shape as unknown); a failure rejects.
 */
export interface FirstRunReads {
  /** `GET /v1/runs?limit=1`. */
  runs(): Promise<unknown>;
  /** `GET /v1/approvals?limit=50`. */
  approvals(): Promise<unknown>;
  /** `GET /v1/auth/invites?limit=1`. */
  invites(): Promise<unknown>;
}

export interface ApiClient {
  /** "fixtures" or "server": shown in the UI so nobody mistakes sample data for real data. */
  readonly source: "fixtures" | "server";
  readonly orgs: OrgsApi;
  readonly config: ConfigApi;
  readonly templates: TemplatesApi;
  readonly policyPresets: PolicyPresetsApi;
  readonly runs: RunsApi;
  readonly approvals: ApprovalsApi;
  /** Decisions on human gates (SA-19). */
  readonly gates: GatesApi;
  /** Autonomy proposals and grants (SA-18 item 2). */
  readonly autonomy: AutonomyApi;
  readonly budgets: BudgetsApi;
  readonly spend: SpendApi;
  /** Read-only reports over the ledger (ND63). */
  readonly reports: ReportsApi;
  /** Token and cache figures over the ledger, read only (R1.34 Part A). */
  readonly savings: SavingsApi;
  /** The derived, read-only dashboards over the ledger (R2.11). */
  readonly dashboards: DashboardsApi;
  /** The model catalog, assignments, connections and resolution (L5). */
  readonly models: ModelsApi;
  /** Who is signed in (decides whether the model screens offer actions). */
  readonly session: SessionApi;
  readonly ledger: LedgerApi;
  /** Sign-in. Absent means this data source needs none (sample data). */
  readonly auth?: AuthApi;
  /** Halt, resume and role pause (E19). */
  readonly org: OrgSafetyApi;
  /** Circuit breakers (E19). */
  readonly breakers: BreakersApi;
  /** Agent credentials (E2). */
  readonly agentCredentials: AgentCredentialsApi;
  /** Change the sign-in email: ask, then open the mailed link (R1.40). */
  readonly emailChange: EmailChangeApi;
  /** Invitations: owners invite, the invitee accepts a link (R1.37). */
  readonly invites: InvitesApi;
  /** Password reset: ask for a link, set a new password, revoke an API token by link (R1.41). */
  readonly passwordReset: PasswordResetApi;
  /** Reads for the first-run checklist; absent where the source cannot answer them (sample data). */
  readonly firstRun?: FirstRunReads;
  /** Who the app is talking to (shown in the sidebar). */
  info(): Promise<SourceInfo>;
}
