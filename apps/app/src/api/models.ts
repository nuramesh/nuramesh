/**
 * Model governance (L5, the model governance notes section 10): the approved catalog, assignments,
 * connections, resolution, and who is signed in. The server's answers are mapped here once; money
 * stays the server's decimal text, an unknown price stays `null`, and no field can hold a key (the
 * server accepts none: R1.14c, stored keys, is not built).
 */

export type EntryStatus = "approved" | "paused" | "retired";
export type DataClass = "synthetic" | "customer";
export type ConnectionStatus = "active" | "disabled";
/** `shipped`: the list that ships with the product; `operator`: set by the organization; `override`: an owner's provisional override. */
export type PriceOrigin = "shipped" | "operator" | "override";

// -- who is signed in -------------------------------------------------------------------------

/** The signed-in person, from `GET /v1/auth/session`. Only the `owner` role may change models. */
export interface Session {
  id: string;
  role: "owner" | "approver" | "viewer";
  name: string;
}

export interface SessionApi {
  /** The signed-in person, or `null` when nobody is (sign-in off, or no session yet). */
  current(): Promise<Session | null>;
}

/** The model screens offer actions only to an admin (the `owner` role; spec 10.3). */
export function isAdmin(session: Session | null | undefined): boolean {
  return session?.role === "owner";
}

// -- raw shapes (what the server sends) --------------------------------------------------------

export interface RawSession {
  user: { id: string; role: string; email: string | null; display_name: string | null };
  csrf_token?: string | null;
}

export interface RawPrice {
  currency: string;
  input_per_million: string;
  output_per_million: string;
  cached_input_per_million: string | null;
  effective_from: string;
  source: string;
  provisional: boolean;
  /** Where the price comes from (spec default-prices 6.2); absent from an older server. */
  origin?: PriceOrigin;
  /** The shipped list row's `retrieved_on` and age in days; null unless the price is a shipped one. */
  retrieved_on?: string | null;
  age_days?: number | null;
  stale?: boolean;
}

export interface RawCatalogEntry {
  provider: string;
  /** Only an entry of an `openai-compatible` connection has one. */
  endpoint_id?: string | null;
  model: string;
  family: string;
  processing: string;
  status: string;
  data_classes: string[];
  price: RawPrice | null;
  connected: boolean;
  origin: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

export interface RawCatalog {
  seeded: boolean;
  config_drift: boolean;
  ignored_events: number;
  customer_regions: string[];
  entries: RawCatalogEntry[];
}

export interface RawCatalogEvent {
  seq: number;
  type: string;
  actor: string;
  at: string;
  payload: Record<string, unknown>;
  reason: string | null;
}

/** One row of `GET /v1/models/connections/events`; a row with `valid: false` is a placeholder and carries nothing else that is to be shown. */
export interface RawConnectionHistoryRow {
  seq: number;
  type: string;
  actor: string;
  at: string;
  provider?: string;
  valid?: boolean;
  endpoint?: string;
  endpoint_id?: string | null;
  status?: string;
  auth?: string;
  key?: string | null;
  repoint?: boolean;
  reason?: string | null;
  ok?: boolean;
  code?: string;
  status_class?: string | null;
  /** A stored-key row (R1.14c spec 9): ids, versions, a fixed cause; never a fingerprint. */
  key_id?: string;
  version?: number | "all";
  previous_version?: number | null;
  grace_seconds?: number;
  cause?: string | null;
}

export interface RawAssignment {
  subject: string;
  scope: string;
  id: string;
  role: string;
  ref: string | null;
  level: string | null;
  source: string | null;
}

export interface RawConnection {
  provider: string;
  /** Only an `openai-compatible` connection has one. */
  endpoint_id?: string | null;
  auth: string;
  key?: string | null;
  endpoint: string;
  status: string;
  set_by: string;
  set_at: string;
  seq: number;
}

export interface RawResolution {
  ok: boolean;
  blocks: {
    subject: string;
    agent: string | null;
    role: string;
    provider: string;
    model: string;
    reason: string;
    detail: string;
    text: string;
  }[];
  resolutions: {
    subject: string;
    ref: string;
    family: string;
    processing: string;
    endpoint: string | null;
    connection_seq: number | null;
    set_by: string | null;
    set_at: string | null;
    price_basis: string | null;
  }[];
}

export interface RawWriteResult {
  already: boolean;
  ledger_seq: number | null;
  events: string[];
  entry?: { provider: string; endpoint_id?: string; model: string; family: string; processing: string; status: string };
  previous?: string;
  affected?: string[];
  repaused?: boolean | string[];
  paused_because?: string;
  customer_regions?: string[];
  assignment?: unknown;
}

export interface RawTestResult {
  provider: string;
  ok: boolean;
  code: string;
  text: string;
  latency_ms: number;
  ledger_seq: number;
}

// -- what the screens use ----------------------------------------------------------------------

export interface ModelPrice {
  inputPerMillion: string;
  outputPerMillion: string;
  cachedInputPerMillion: string | null;
  effectiveFrom: string;
  source: string;
  /** An owner override, not a book price. */
  provisional: boolean;
  origin?: PriceOrigin;
  /** The date the shipped list row was read from the provider's page (`YYYY-MM-DD`), or `null`. */
  retrievedOn?: string | null;
  /** Whole days from `retrievedOn` to now for a shipped price, or `null`. */
  ageDays?: number | null;
  /** A shipped price older than 90 days: still used, but the list needs a refresh. */
  stale?: boolean;
}

export interface CatalogEntry {
  /** `provider/model`, or `openai-compatible/<endpoint id>/<model>`: the text an assignment uses. */
  ref: string;
  provider: string;
  /** The `openai-compatible` connection this entry belongs to; absent for every other provider. */
  endpointId?: string;
  model: string;
  family: string;
  processing: string;
  status: EntryStatus;
  dataClasses: DataClass[];
  price: ModelPrice | null;
  /** The provider (for `openai-compatible`: this entry's connection) has an active connection. */
  connected: boolean;
  origin: "seed" | "api" | null;
  updatedBy: string | null;
  updatedAt: string | null;
}

export interface Catalog {
  /** A catalog exists in the ledger (false on a clean install: no real model can run). */
  seeded: boolean;
  /** The team file's `models:` part differs from the catalog in force: the ledger wins. */
  configDrift: boolean;
  ignoredEvents: number;
  customerRegions: string[];
  entries: CatalogEntry[];
}

export interface CatalogEvent {
  seq: number;
  type: string;
  actor: string;
  at: string;
  /** The model this event is about, when it names one. */
  ref: string | null;
  /** Plain words for the event. */
  summary: string;
  /** The reason a person gave; `null` once the content store has expired it. */
  reason: string | null;
}

export interface AssignmentRow {
  subject: string;
  scope: "agent" | "role";
  id: string;
  role: string;
  ref: string | null;
  /** Where the effective model comes from. */
  from: "agent ledger" | "agent config" | "role ledger" | "role config" | "none";
}

export interface Connection {
  provider: string;
  /** How it signs in: `entra`, `key_file`, `none`, `key` (a key binding) or `stored`. */
  auth: string;
  /** `auth` `key` only: the NAME of the operator's key binding (never a key and never a path). */
  keyName?: string;
  /** Names one `openai-compatible` connection; absent for every other provider. */
  endpointId?: string;
  endpoint: string;
  status: ConnectionStatus;
  setBy: string;
  setAt: string;
  seq: number;
}

/** A page of catalog history, newest page first; each page is in ascending `seq`. */
export interface CatalogEventPage {
  events: CatalogEvent[];
  /** Pass as `before` for the next older page; `null` when this is the oldest. */
  nextBefore: number | null;
}

/**
 * One row of a connection's history. A row that is not `valid` is a placeholder: the mapper keeps
 * only `seq`, `type`, `actor` and `at` for it, whatever else arrived, so nothing raw can be shown.
 * The key is only ever a key NAME.
 */
export interface ConnectionHistoryRow {
  seq: number;
  type: string;
  actor: string;
  at: string;
  provider: string;
  valid: boolean;
  endpoint?: string;
  endpointId?: string;
  status?: string;
  auth?: string;
  keyName?: string;
  repoint?: boolean;
  reason?: string | null;
  ok?: boolean;
  code?: string;
  statusClass?: string | null;
  /** A stored-key row: the key id (never a value), the version (`"all"` on a revoke of every version). */
  keyId?: string;
  version?: number | "all";
  previousVersion?: number;
  graceSeconds?: number;
  cause?: string | null;
}

export interface ConnectionHistoryPage {
  rows: ConnectionHistoryRow[];
  nextBefore: number | null;
}

export interface ConnectionHistoryQuery {
  provider?: string;
  /** One `openai-compatible` connection. */
  endpointId?: string;
  before?: number;
  /** Page size; the server's own default when absent. */
  limit?: number;
}

export interface ResolutionBlock {
  subject: string;
  reason: string;
  detail: string;
  text: string;
}

export interface ResolutionRow {
  subject: string;
  ref: string;
  family: string;
  processing: string;
  endpoint: string | null;
  connectionSeq: number | null;
  priceBasis: string | null;
}

export interface Resolution {
  ok: boolean;
  blocks: ResolutionBlock[];
  resolutions: ResolutionRow[];
}

/** What a write answered: nothing was written when `already`. */
export interface WriteResult {
  already: boolean;
  ledgerSeq: number | null;
  /** Approved entries that were paused again by this write and need a fresh approval. */
  repaused: string[];
  /** An edit that widened `processing` paused the (approved) entry it edited. */
  entryRepaused: boolean;
  pausedBecause: string | null;
  /** Agents and roles whose next call is now blocked (pause and retire). */
  affected: string[];
}

export interface TestResult {
  provider: string;
  ok: boolean;
  code: string;
  text: string;
  latencyMs: number;
  ledgerSeq: number;
}

// -- inputs ------------------------------------------------------------------------------------

export interface EntryRef {
  provider: string;
  /** Names the `openai-compatible` connection of the entry (the server requires it for that provider and refuses it for any other). */
  endpointId?: string;
  model: string;
}

export const OPENAI_COMPATIBLE = "openai-compatible";
/** The only source the server accepts for a price of zero (a model on the server's own machine, core.endpoints). */
export const FREE_SOURCE = "declared free (local)";

/** The model text of an entry, as the server writes it (`core.models.format_model_ref`). */
export function modelRef(entry: EntryRef): string {
  return entry.endpointId === undefined ? `${entry.provider}/${entry.model}` : `${entry.provider}/${entry.endpointId}/${entry.model}`;
}

/** The fields that name an entry in a write: the endpoint id only when the entry has one. */
export function entryRef(entry: EntryRef): EntryRef {
  return { provider: entry.provider, ...(entry.endpointId === undefined ? {} : { endpointId: entry.endpointId }), model: entry.model };
}
export interface Reasoned {
  reason: string;
}
export interface AddEntryInput extends EntryRef, Reasoned {
  family: string;
  processing: string;
}
export interface EditEntryInput extends EntryRef, Reasoned {
  family?: string;
  processing?: string;
}
export interface PriceInput extends EntryRef, Reasoned {
  inputPerMillion: string;
  outputPerMillion: string;
  cachedInputPerMillion?: string;
  cacheWritePerMillion?: string;
  effectiveFrom: string;
  source: string;
}
export interface AssignInput extends Reasoned {
  scope: "agent" | "role";
  id: string;
  /** `provider/model`, or `null` to clear the ledger assignment (the configuration applies again). */
  model: string | null;
}
export interface ConnectionInput {
  /** Names one `openai-compatible` connection (a slug); every other provider has none. */
  endpointId?: string;
  /** `openai-compatible` only: `none` (a server without a key) or `key` (with `key`, a binding name). */
  auth?: "none" | "key";
  /** The NAME of a key binding in the operator's binding file; never a key. Only with `auth` `key`. */
  key?: string;
  endpoint: string;
  status?: ConnectionStatus;
  /** Required by the server only to repoint an existing connection. */
  reason?: string;
}

/** Every call the model screens make (one per server route, spec 10.3). Reads of admin routes reject with a 403 `ProblemError` for anyone else. */
/** One stored version of a key: only its number and state, never a value or fingerprint. */
export interface KeyVersion {
  version: number;
  /** `current`, `previous` (in its grace period), `revoked`, `key_missing` (the store lost it) or a word the server adds later. */
  state: string;
  /** Only an admin's read has the times. */
  createdAt?: string;
  activatedAt?: string | null;
  graceUntil?: string | null;
}

export interface KeyState {
  versions: KeyVersion[];
  keyId?: string;
  ageDays?: number;
  /** Advice after 90 days, never a block. */
  rotateAdvised?: boolean;
}

export interface KeyTarget {
  provider: string;
  /** Names one `openai-compatible` connection. */
  endpointId?: string;
}

/** The write-only value is sent once and is not part of any answer. */
export interface SetKeyInput extends KeyTarget {
  value: string;
  graceSeconds?: number;
  reason?: string;
}

export interface SetKeyResult {
  keyId: string;
  version: number;
  state: string;
  createdAt: string;
  /** True for a first key (201), false for a rotation (200). */
  first: boolean;
}

export interface RevokeKeyInput extends KeyTarget {
  reason: string;
  version?: number;
}

export interface RevokeKeyResult {
  keyId: string;
  revokedVersions: number[];
}

/** Providers that take a stored key (R1.14c); the others are keyless. */
export const STORED_KEY_PROVIDERS: readonly string[] = ["anthropic"];

export interface RawKeyState {
  versions: { version: number; state: string; created_at?: string; activated_at?: string | null; grace_until?: string | null }[];
  key_id?: string;
  age_days?: number;
  rotate_advised?: boolean;
}

export interface RawSetKey {
  key_id: string;
  version: number;
  state: string;
  created_at: string;
}

export function keyState(raw: RawKeyState): KeyState {
  return {
    versions: raw.versions.map((v) => ({
      version: v.version,
      state: v.state,
      ...(v.created_at === undefined ? {} : { createdAt: v.created_at }),
      ...(v.activated_at === undefined ? {} : { activatedAt: v.activated_at }),
      ...(v.grace_until === undefined ? {} : { graceUntil: v.grace_until }),
    })),
    ...(raw.key_id === undefined ? {} : { keyId: raw.key_id }),
    ...(raw.age_days === undefined ? {} : { ageDays: raw.age_days }),
    ...(raw.rotate_advised === undefined ? {} : { rotateAdvised: raw.rotate_advised }),
  };
}

export interface ModelsApi {
  catalog(): Promise<Catalog>;
  /** Admin only: a page of the honored catalog and assignment events with their reasons (the newest page without `before`). */
  events(before?: number): Promise<CatalogEventPage>;
  /** Admin only: a page of one connection's change history, newest page first. */
  connectionHistory(query: ConnectionHistoryQuery): Promise<ConnectionHistoryPage>;
  assignments(): Promise<AssignmentRow[]>;
  /** Admin only. */
  connections(): Promise<Connection[]>;
  /** Admin only: the start-stage check of every agent and role for this data class. */
  resolution(dataClass: DataClass): Promise<Resolution>;
  addEntry(input: AddEntryInput): Promise<WriteResult>;
  editEntry(input: EditEntryInput): Promise<WriteResult>;
  approve(input: EntryRef & Reasoned): Promise<WriteResult>;
  pause(input: EntryRef & Reasoned): Promise<WriteResult>;
  retire(input: EntryRef & Reasoned): Promise<WriteResult>;
  remove(input: EntryRef & Reasoned): Promise<WriteResult>;
  setPrice(input: PriceInput): Promise<WriteResult>;
  setRegions(input: { customerRegions: string[] } & Reasoned): Promise<WriteResult>;
  assign(input: AssignInput): Promise<WriteResult>;
  /** Connect, enable, disable or repoint (a PUT). `repaused` lists the approved entries a repoint paused again. */
  putConnection(provider: string, input: ConnectionInput): Promise<{ repaused: string[] }>;
  removeConnection(provider: string, reason?: string, endpointId?: string): Promise<void>;
  testConnection(provider: string, endpointId?: string): Promise<TestResult>;
  /** The versions of a connection's stored key and their states (never a value). */
  keyState(target: KeyTarget): Promise<KeyState>;
  /** Owner only: set the first key or rotate it. The value is never returned and never kept by the app. */
  setKey(input: SetKeyInput): Promise<SetKeyResult>;
  /** Owner only: revoke one version or all of them. */
  revokeKey(input: RevokeKeyInput): Promise<RevokeKeyResult>;
  /** Admin: whether the server's secret store answers. */
  keysHealth(): Promise<"ok" | "unavailable">;
}

// -- mappers -----------------------------------------------------------------------------------

export function session(raw: RawSession): Session {
  const role = raw.user.role === "owner" || raw.user.role === "approver" ? raw.user.role : "viewer";
  return { id: raw.user.id, role, name: raw.user.display_name ?? raw.user.email ?? raw.user.id };
}

function price(raw: RawPrice | null): ModelPrice | null {
  if (raw === null) return null;
  return {
    inputPerMillion: raw.input_per_million,
    outputPerMillion: raw.output_per_million,
    cachedInputPerMillion: raw.cached_input_per_million,
    effectiveFrom: raw.effective_from,
    source: raw.source,
    provisional: raw.provisional,
    origin: raw.origin,
    retrievedOn: raw.retrieved_on ?? null,
    ageDays: raw.age_days ?? null,
    stale: raw.stale ?? false,
  };
}

const STATUSES: readonly string[] = ["approved", "paused", "retired"];

export function catalog(raw: RawCatalog): Catalog {
  return {
    seeded: raw.seeded,
    configDrift: raw.config_drift,
    ignoredEvents: raw.ignored_events,
    customerRegions: raw.customer_regions,
    entries: raw.entries.map((e) => ({
      ref: typeof e.endpoint_id === "string" ? `${e.provider}/${e.endpoint_id}/${e.model}` : `${e.provider}/${e.model}`,
      provider: e.provider,
      ...(typeof e.endpoint_id === "string" ? { endpointId: e.endpoint_id } : {}),
      model: e.model,
      family: e.family,
      processing: e.processing,
      // an unknown status is shown as paused: it must never look usable
      status: (STATUSES.includes(e.status) ? e.status : "paused") as EntryStatus,
      dataClasses: e.data_classes.filter((c): c is DataClass => c === "synthetic" || c === "customer"),
      price: price(e.price),
      connected: e.connected,
      origin: e.origin === "seed" || e.origin === "api" ? e.origin : null,
      updatedBy: e.updated_by,
      updatedAt: e.updated_at,
    })),
  };
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** One catalog or assignment event in plain words. */
export function catalogEvent(raw: RawCatalogEvent): CatalogEvent {
  const p = raw.payload;
  const provider = str(p.provider);
  const model = str(p.model);
  const endpointId = str(p.endpoint_id);
  const ref = provider !== null && model !== null ? modelRef({ provider, ...(endpointId === null || endpointId === "" ? {} : { endpointId }), model }) : null;
  const because = str(p.because);
  const text: Record<string, string> = {
    "model.catalog_seeded": "Catalog seeded from the team configuration",
    "model.catalog_entry_added": `Added ${ref ?? "an entry"} (paused until approved)`,
    "model.catalog_entry_edited": `Edited ${ref ?? "an entry"}`,
    "model.catalog_approved": `Approved ${ref ?? "an entry"}`,
    "model.catalog_paused": `Paused ${ref ?? "an entry"}${because === null ? "" : ` (${because.replaceAll("_", " ")})`}`,
    "model.catalog_retired": `Retired ${ref ?? "an entry"}`,
    "model.catalog_entry_removed": `Removed ${ref ?? "an entry"}`,
    "model.catalog_price_set": `Price set for ${ref ?? "an entry"}`,
    "model.catalog_regions_set": "Customer regions set",
    "model.assignment_set": `${str(p.scope) ?? "scope"} ${str(p.id) ?? ""} now uses ${str(p.model) ?? "its configured model"}`,
  };
  return {
    seq: raw.seq,
    type: raw.type,
    actor: raw.actor,
    at: raw.at,
    ref,
    summary: text[raw.type] ?? raw.type,
    reason: raw.reason,
  };
}

const FROM = ["agent ledger", "agent config", "role ledger", "role config"] as const;

export function assignment(raw: RawAssignment): AssignmentRow {
  const from = `${raw.level ?? ""} ${raw.source ?? ""}`.trim();
  return {
    subject: raw.subject,
    scope: raw.scope === "role" ? "role" : "agent",
    id: raw.id,
    role: raw.role,
    ref: raw.ref,
    from: (FROM as readonly string[]).includes(from) ? (from as AssignmentRow["from"]) : "none",
  };
}

const PLACEHOLDER = "(invalid)";

function opt<K extends string, V>(key: K, value: V | undefined | null): Partial<Record<K, V>> {
  return value === undefined || value === null ? {} : ({ [key]: value } as Partial<Record<K, V>>);
}

/** Maps one history row. Anything that is not a well-formed valid row becomes a placeholder with no raw value. */
export function connectionHistoryRow(raw: RawConnectionHistoryRow): ConnectionHistoryRow {
  const head = { seq: raw.seq, type: raw.type, actor: raw.actor, at: raw.at };
  if (raw.valid !== true || typeof raw.provider !== "string" || raw.provider === PLACEHOLDER) {
    return { ...head, provider: PLACEHOLDER, valid: false };
  }
  const text = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
  return {
    ...head,
    provider: raw.provider,
    valid: true,
    ...opt("endpoint", text(raw.endpoint)),
    ...opt("endpointId", text(raw.endpoint_id)),
    ...opt("status", text(raw.status)),
    ...opt("auth", text(raw.auth)),
    ...opt("keyName", text(raw.key)),
    ...(typeof raw.repoint === "boolean" ? { repoint: raw.repoint } : {}),
    ...(raw.reason === null || typeof raw.reason === "string" ? { reason: raw.reason } : {}),
    ...(typeof raw.ok === "boolean" ? { ok: raw.ok } : {}),
    ...opt("code", text(raw.code)),
    ...(raw.status_class === null || typeof raw.status_class === "string" ? { statusClass: raw.status_class } : {}),
    ...opt("keyId", text(raw.key_id)),
    ...(typeof raw.version === "number" || raw.version === "all" ? { version: raw.version } : {}),
    ...opt("previousVersion", typeof raw.previous_version === "number" ? raw.previous_version : undefined),
    ...opt("graceSeconds", typeof raw.grace_seconds === "number" ? raw.grace_seconds : undefined),
    ...(raw.cause === null || typeof raw.cause === "string" ? { cause: raw.cause } : {}),
  };
}

export function connection(raw: RawConnection): Connection {
  return {
    provider: raw.provider,
    auth: raw.auth,
    ...(typeof raw.key === "string" ? { keyName: raw.key } : {}),
    ...(typeof raw.endpoint_id === "string" ? { endpointId: raw.endpoint_id } : {}),
    endpoint: raw.endpoint,
    status: raw.status === "disabled" ? "disabled" : "active",
    setBy: raw.set_by,
    setAt: raw.set_at,
    seq: raw.seq,
  };
}

export function resolution(raw: RawResolution): Resolution {
  return {
    ok: raw.ok,
    blocks: raw.blocks.map((b) => ({ subject: b.subject, reason: b.reason, detail: b.detail, text: b.text })),
    resolutions: raw.resolutions.map((r) => ({
      subject: r.subject,
      ref: r.ref,
      family: r.family,
      processing: r.processing,
      endpoint: r.endpoint,
      connectionSeq: r.connection_seq,
      priceBasis: r.price_basis,
    })),
  };
}

export function writeResult(raw: RawWriteResult): WriteResult {
  const list = Array.isArray(raw.repaused) ? raw.repaused : [];
  return {
    already: raw.already,
    ledgerSeq: raw.ledger_seq ?? null,
    repaused: list,
    entryRepaused: raw.repaused === true,
    pausedBecause: raw.paused_because ?? null,
    affected: raw.affected ?? [],
  };
}

export function testResult(raw: RawTestResult): TestResult {
  return {
    provider: raw.provider,
    ok: raw.ok,
    code: raw.code,
    text: raw.text,
    latencyMs: raw.latency_ms,
    ledgerSeq: raw.ledger_seq,
  };
}
