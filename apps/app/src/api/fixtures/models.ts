/**
 * Sample data for the model screens, with the server's rules (spec/model-governance 2.6 to 2.8) so
 * every state can be tried without a server: refusals with problems, a widening write that pauses
 * an approved entry again, a repoint, a connection test. It is stateful, and a fresh client starts
 * from the seed. Nothing here is real: the sidebar says "Sample data" while it is in use.
 */
import { InvalidRequestError, NotFoundError, ProblemError } from "../client";
import {
  acceptedClasses,
  endpointHint,
  endpointIdProblem,
  isLoopbackEndpoint,
  isZeroRate,
  keyNameProblem,
  openAiEndpointHint,
  parseRegions,
  reasonProblem,
  regionsWiden,
  splitRef,
  validProcessing,
  validRegionId,
  widens,
} from "../model-rules";
import { FREE_SOURCE, modelRef, STORED_KEY_PROVIDERS } from "../models";
import type {
  AssignmentRow,
  Catalog,
  CatalogEntry,
  CatalogEvent,
  ConnectionHistoryRow,
  Connection,
  DataClass,
  EntryRef,
  EntryStatus,
  ModelPrice,
  KeyState,
  KeyVersion,
  ModelsApi,
  Resolution,
  ResolutionBlock,
  Session,
  SessionApi,
  WriteResult,
} from "../models";

export type FixtureRole = Session["role"];

interface Stored {
  provider: string;
  /** Only an entry of an `openai-compatible` connection has one. */
  endpointId?: string;
  model: string;
  family: string;
  processing: string;
  status: EntryStatus;
  prices: ModelPrice[];
  origin: "seed" | "api";
  updatedBy: string;
  updatedAt: string;
}

const AGENTS = [
  { id: "chief", role: "cos", base: null },
  { id: "dana", role: "developer", base: null },
  { id: "theo", role: "tester", base: null },
  { id: "rhea", role: "reviewer", base: null },
  { id: "omar", role: "devops", base: null },
] as const;
const ROLE_DEFAULTS: Record<string, string> = {
  developer: "fake/dev-1",
  tester: "fake/dev-1",
  reviewer: "fake/reviewer-1",
  devops: "fake/dev-1",
};
const KEYLESS = ["azure-openai", "azure-ai"];
const OPENAI_COMPATIBLE = "openai-compatible";
// the server's fixed texts (core.models, core.endpoints, core.prices)
const U_ENDPOINT_NEEDED = "openai-compatible: this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)";
const U_ID = "openai-compatible: the endpoint id is not valid";
const REMOTE_RESIDENCY = "openai-compatible: a remote endpoint can only declare global processing";
const P_FREE = `: Value error, openai-compatible: a zero price must be declared free for a local connection (source "${FREE_SOURCE}" and a bound endpoint)`;
const P_LOOPBACK = "openai-compatible: a free price is only allowed for an active local (loopback) connection";
const PRIVATE_REFUSED = "openai-compatible: refused: the host is a private, loopback or link-local address (or its name resolves to one); set NURAMESH_ALLOW_PRIVATE_NETWORK=1 on the server to allow a model on your own network";
const isLoopback = (endpoint: string): boolean => /^http:\/\/(127\.0\.0\.1|\[::1\])/i.test(endpoint);
const SERVER_REASON = "a reason (3 to 500 characters) is required";
const ME = "human:u_sample";
const SEED = "system:catalog-seed";
const EPOCH = "2026-10-24T12:00:00Z";

const price = (input: string, output: string, source: string): ModelPrice => ({
  inputPerMillion: input,
  outputPerMillion: output,
  cachedInputPerMillion: null,
  effectiveFrom: "2026-03-01T00:00:00+00:00",
  source,
  provisional: false,
});

function seedEntries(): Stored[] {
  const e = (
    provider: string,
    model: string,
    family: string,
    processing: string,
    status: EntryStatus,
    prices: ModelPrice[],
  ): Stored => ({ provider, model, family, processing, status, prices, origin: "seed", updatedBy: SEED, updatedAt: EPOCH });
  return [
    e("fake", "dev-1", "fake-a", "canada", "approved", [price("3", "15", "demo price list (fake model)")]),
    e("fake", "reviewer-1", "fake-b", "canada", "approved", [price("3", "15", "demo price list (fake model)")]),
    e("azure-openai", "gpt-5.4-mini", "openai", "global", "approved", [price("0.75", "4.50", "vendor price list")]),
    e("azure-openai", "gpt-5.4-ca", "openai", "canada", "approved", [price("0.75", "4.50", "vendor price list")]),
    e("azure-ai", "Llama-4-Maverick", "meta", "global", "paused", []),
    e("azure-ai", "Mistral-Large", "mistral", "region:eu-west", "approved", [price("2", "6", "vendor price list")]),
  ];
}

function fail(status: number, title: string, message: string): never {
  throw new ProblemError(message, status, title);
}
function invalid(path: string, message: string): never {
  throw new InvalidRequestError(message, [{ path, message }]);
}

export interface FixtureModelsOptions {
  /** The server's `NURAMESH_ALLOW_PRIVATE_NETWORK`: false (the default) refuses a test of a local endpoint. */
  allowPrivateNetwork?: boolean;
  role?: FixtureRole;
  now?: () => string;
}

/** The model screens' sample API and session, one per fixture client. */
export function createFixtureModels(options: FixtureModelsOptions = {}): { models: ModelsApi; session: SessionApi } {
  const role = options.role ?? "owner";
  let tick = 0;
  const now = options.now ?? ((): string => new Date(Date.parse(EPOCH) + (tick += 1) * 1000).toISOString());
  let seq = 10;
  let regions: string[] = [];
  let entries = seedEntries();
  const assignments = new Map<string, string>();
  let connections: Connection[] = [];
  const history: ConnectionHistoryRow[] = [];
  /** Stored-key versions per connection: numbers and states only, the value is dropped on arrival. */
  const keys = new Map<string, KeyVersion[]>();
  const PAGE = 50;
  const events: CatalogEvent[] = [
    { seq: 1, type: "model.catalog_seeded", actor: SEED, at: EPOCH, ref: null, summary: "Catalog seeded from the team configuration", reason: null },
  ];

  const refOf = (e: EntryRef): string => modelRef(e);
  const is = (e: Stored, i: EntryRef): boolean => e.provider === i.provider && e.endpointId === i.endpointId && e.model === i.model;
  /** The server's check of the endpoint id (engine.openai_compatible `endpoint_id_problem`): needed for one provider, refused for the others. */
  const named = (i: EntryRef): void => {
    if (i.provider !== OPENAI_COMPATIBLE) {
      if (i.endpointId !== undefined) invalid("$.endpoint_id", "endpoint_id_not_allowed");
    } else if (i.endpointId === undefined) invalid("$.endpoint_id", U_ENDPOINT_NEEDED);
    else if (endpointIdProblem(i.endpointId) !== null) invalid("$.endpoint_id", U_ID);
  };
  const find = (i: EntryRef): Stored => {
    named(i);
    const found = entries.find((e) => is(e, i));
    if (found === undefined) fail(404, "not_found", `'${refOf(i)}' is not in the model catalog`);
    return found;
  };
  const lookup = (ref: string): Stored | undefined => {
    const parts = splitRef(ref);
    return parts === null ? undefined : entries.find((e) => is(e, parts));
  };
  const linkOf = (e: EntryRef): Connection | undefined => connections.find((c) => c.provider === e.provider && c.endpointId === e.endpointId);
  /** Spec openai-compatible 10: an entry of a remote connection takes only `global`; a connection that does not exist yet judges nothing. */
  const residency = (e: EntryRef, processing: string): void => {
    const link = e.provider === OPENAI_COMPATIBLE ? linkOf(e) : undefined;
    if (link !== undefined && !isLoopbackEndpoint(link.endpoint) && processing !== "global") invalid("$.processing", REMOTE_RESIDENCY);
  };
  const record = (type: string, summary: string, reason: string | null, ref: string | null = null): number => {
    seq += 1;
    events.push({ seq, type, actor: ME, at: now(), ref, summary, reason: reason?.trim() ?? null });
    return seq;
  };
  const needReason = (reason: string): string => {
    if (reasonProblem(reason) !== null) invalid("$.reason", SERVER_REASON);
    return reason.trim();
  };
  const stamp = (e: Stored): void => {
    e.updatedBy = ME;
    e.updatedAt = now();
  };
  const priceNow = (e: Stored): ModelPrice | null => {
    const at = Date.parse(now());
    const valid = e.prices.filter((p) => Date.parse(p.effectiveFrom) <= at);
    return valid.length === 0 ? null : (valid.reduce((a, b) => (Date.parse(b.effectiveFrom) > Date.parse(a.effectiveFrom) ? b : a)) satisfies ModelPrice);
  };
  const same = (c: Connection, provider: string, endpointId: string | undefined): boolean => c.provider === provider && c.endpointId === endpointId;
  const connected = (e: EntryRef): boolean => linkOf(e)?.status === "active";
  const effective = (agent: (typeof AGENTS)[number]): { ref: string | null; from: AssignmentRow["from"] } => {
    const own = assignments.get(`agent:${agent.id}`);
    if (own !== undefined) return { ref: own, from: "agent ledger" };
    const viaRole = assignments.get(`role:${agent.role}`);
    if (viaRole !== undefined) return { ref: viaRole, from: "role ledger" };
    const base = ROLE_DEFAULTS[agent.role];
    return base === undefined ? { ref: null, from: "none" } : { ref: base, from: "role config" };
  };
  const users = (ref: string): string[] =>
    AGENTS.filter((a) => effective(a).ref === ref).map((a) => `agent=${a.id}`);

  const result = (over: Partial<WriteResult> = {}, ledgerSeq: number | null = null): WriteResult => ({
    already: false,
    ledgerSeq,
    repaused: [],
    entryRepaused: false,
    pausedBecause: null,
    affected: [],
    ...over,
  });
  const nothing = (): WriteResult => result({ already: true });

  const setStatus = (e: Stored, status: EntryStatus, type: string, verb: string, reason: string): number => {
    e.status = status;
    stamp(e);
    return record(type, `${verb} ${refOf(e)}`, reason, refOf(e));
  };

  const catalog = (): Catalog => ({
    seeded: true,
    configDrift: false,
    ignoredEvents: 0,
    customerRegions: [...regions],
    entries: entries.map(
      (e): CatalogEntry => ({
        ref: refOf(e),
        provider: e.provider,
        ...(e.endpointId === undefined ? {} : { endpointId: e.endpointId }),
        model: e.model,
        family: e.family,
        processing: e.processing,
        status: e.status,
        dataClasses: acceptedClasses(e.processing, regions),
        price: priceNow(e),
        connected: connected(e),
        origin: e.origin,
        updatedBy: e.updatedBy,
        updatedAt: e.updatedAt,
      }),
    ),
  });

  const keyName = (t: { provider: string; endpointId?: string }): string => `${t.provider}/${t.endpointId ?? ""}`;
  const keyTarget = (provider: string): void => {
    if (!STORED_KEY_PROVIDERS.includes(provider)) invalid("$.provider", "this provider takes no stored key");
  };
  const admin = (): void => {
    if (role !== "owner") fail(403, "forbidden", "your role doesn't allow this");
  };
  const reasonOf = (r: string): string => {
    admin();
    return needReason(r);
  };

  const resolve = (dataClass: DataClass): Resolution => {
    const blocks: ResolutionBlock[] = [];
    const rows: Resolution["resolutions"] = [];
    for (const agent of AGENTS) {
      const { ref } = effective(agent);
      if (ref === null) continue;
      const subject = `agent=${agent.id}`;
      const block = (reason: string, detail: string): void => {
        blocks.push({ subject, reason, detail, text: `model blocked [${reason}] ${subject}: ${detail}` });
      };
      const entry = lookup(ref);
      const link = entry === undefined ? undefined : linkOf(entry);
      const provider = entry?.provider ?? "";
      const who = entry?.endpointId === undefined ? `provider '${provider}'` : `'${provider}/${entry.endpointId}'`;
      if (entry === undefined) block("model_not_in_catalog", `'${ref}' is not in the approved catalog`);
      else if (entry.status !== "approved") block(entry.status === "paused" ? "model_paused" : "model_retired", `'${ref}' is ${entry.status} in the approved catalog`);
      else if (entry.provider !== "fake" && entry.provider !== OPENAI_COMPATIBLE && !KEYLESS.includes(entry.provider)) block("provider_unsupported", `provider '${entry.provider}' has no adapter in this version (keyless providers: ${KEYLESS.join(", ")})`);
      else if (entry.provider !== "fake" && !acceptedClasses(entry.processing, regions).includes(dataClass)) block("residency_refused", `'${ref}' is processed in ${entry.processing} and may not receive ${dataClass} data`);
      else if (entry.provider !== "fake" && link === undefined) block("no_connection", `no connection for ${who}: an admin connects it (PUT /v1/models/connections/${entry.provider}, or the Models screen in the app)`);
      else if (entry.provider !== "fake" && link?.status === "disabled") block("connection_disabled", `the connection for ${who} is disabled`);
      else if (priceNow(entry) === null) block("unknown_price", `'${ref}' has no price in effect at ${now()}; a price is never guessed`);
      else {
        rows.push({ subject, ref, family: entry.family, processing: entry.processing, endpoint: link?.endpoint ?? null, connectionSeq: link?.seq ?? null, priceBasis: "book" });
      }
    }
    return { ok: blocks.length === 0, blocks, resolutions: blocks.length === 0 ? rows : [] };
  };

  const models: ModelsApi = {
    catalog: () => Promise.resolve(catalog()),
    events: (before) => {
      admin();
      return Promise.resolve({ events: structuredClone(events.filter((e) => before === undefined || e.seq < before)), nextBefore: null });
    },
    connectionHistory: (query) => {
      admin();
      const matching = history.filter(
        (r) =>
          (query.provider === undefined || r.provider === query.provider) &&
          (query.endpointId === undefined || r.endpointId === query.endpointId) &&
          (query.before === undefined || r.seq < query.before),
      );
      const size = query.limit !== undefined && query.limit > 0 ? Math.min(query.limit, PAGE) : PAGE;
      const rows = matching.slice(-size);
      return Promise.resolve({ rows: structuredClone(rows), nextBefore: matching.length > rows.length ? (rows[0]?.seq ?? null) : null });
    },
    assignments: () =>
      Promise.resolve([
        ...AGENTS.map((a): AssignmentRow => {
          const { ref, from } = effective(a);
          return { subject: `agent=${a.id}`, scope: "agent", id: a.id, role: a.role, ref, from };
        }),
      ]),
    connections: () => {
      admin();
      return Promise.resolve(structuredClone(connections));
    },
    resolution: (dataClass) => {
      admin();
      return Promise.resolve(resolve(dataClass));
    },
    addEntry: (i) => {
      const reason = reasonOf(i.reason);
      named(i);
      if (!validProcessing(i.processing)) invalid("$.processing", `processing scope '${i.processing}' is not canada, global or region:<id>`);
      residency(i, i.processing);
      if (entries.some((e) => is(e, i))) fail(409, "already_exists", `'${refOf(i)}' is already in the model catalog`);
      entries.push({ provider: i.provider, ...(i.endpointId === undefined ? {} : { endpointId: i.endpointId }), model: i.model, family: i.family, processing: i.processing, status: "paused", prices: [], origin: "api", updatedBy: ME, updatedAt: now() });
      return Promise.resolve(result({}, record("model.catalog_entry_added", `Added ${refOf(i)} (paused until approved)`, reason, refOf(i))));
    },
    editEntry: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (i.family === undefined && i.processing === undefined) invalid("$", "give a family, a processing scope or both");
      if (i.processing !== undefined && !validProcessing(i.processing)) invalid("$.processing", `processing scope '${i.processing}' is not canada, global or region:<id>`);
      if (i.processing !== undefined) residency(e, i.processing);
      const widen = i.processing !== undefined && widens(e.processing, i.processing, regions);
      e.family = i.family ?? e.family;
      e.processing = i.processing ?? e.processing;
      stamp(e);
      let at = record("model.catalog_entry_edited", `Edited ${refOf(e)}`, reason, refOf(e));
      const paused = widen && e.status === "approved";
      if (paused) {
        e.status = "paused";
        at = record("model.catalog_paused", `Paused ${refOf(e)} (processing widened)`, reason, refOf(e));
      }
      return Promise.resolve(result({ entryRepaused: paused, pausedBecause: paused ? "processing_widened" : null }, at));
    },
    approve: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (e.status === "retired") fail(409, "retired", `'${refOf(e)}' is retired`);
      if (e.status === "approved") return Promise.resolve(nothing());
      if (e.prices.length === 0) invalid("$.model", `'${refOf(e)}' is approved but has no price: add it to the price book (a price is never guessed) or pause the entry`);
      return Promise.resolve(result({}, setStatus(e, "approved", "model.catalog_approved", "Approved", reason)));
    },
    pause: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (e.status === "retired") fail(409, "retired", `'${refOf(e)}' is retired`);
      if (e.status === "paused") return Promise.resolve(nothing());
      return Promise.resolve(result({ affected: users(refOf(e)) }, setStatus(e, "paused", "model.catalog_paused", "Paused", reason)));
    },
    retire: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (e.status === "retired") return Promise.resolve(nothing());
      return Promise.resolve(result({ affected: users(refOf(e)) }, setStatus(e, "retired", "model.catalog_retired", "Retired", reason)));
    },
    remove: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (e.status === "approved") fail(409, "not_paused", `'${refOf(e)}' must be paused or retired before it is removed`);
      const using = users(refOf(e));
      if (using.length > 0) fail(409, "in_use", `'${refOf(e)}' is still assigned to ${using.join(", ")}; reassign them first`);
      entries = entries.filter((x) => x !== e);
      return Promise.resolve(result({}, record("model.catalog_entry_removed", `Removed ${refOf(e)}`, reason, refOf(e))));
    },
    setPrice: (i) => {
      const reason = reasonOf(i.reason);
      const e = find(i);
      if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(i.effectiveFrom)) invalid("$.effective_from", "effective_from must be an ISO 8601 UTC instant like 2026-03-01T00:00:00Z");
      for (const [path, value] of [["$.input_per_million", i.inputPerMillion], ["$.output_per_million", i.outputPerMillion]] as const) {
        if (!/^\d+(\.\d+)?$/.test(value)) invalid(path, "rates are decimal text such as 0.75, not negative");
      }
      if (i.source.trim().length < 3) invalid("$.source", "a source of 3 to 500 characters is required");
      // no silent zero (spec openai-compatible 12.2): both rates 0 only as a declared-free price of an active local connection
      if (isZeroRate(i.inputPerMillion) && isZeroRate(i.outputPerMillion)) {
        const link = e.provider === OPENAI_COMPATIBLE ? linkOf(e) : undefined;
        if (link?.status !== "active" || !isLoopbackEndpoint(link.endpoint)) invalid("$", e.provider === OPENAI_COMPATIBLE ? P_LOOPBACK : P_FREE);
        if (i.source !== FREE_SOURCE) invalid("$", P_FREE);
      }
      const stored = i.effectiveFrom.replace("Z", "+00:00");
      if (e.prices.some((p) => p.effectiveFrom === stored)) fail(409, "price_version_exists", `a price version for '${refOf(e)}' effective from ${i.effectiveFrom} already exists: prices are added, never edited; add a version with a later effective_from`);
      e.prices.push({ inputPerMillion: i.inputPerMillion, outputPerMillion: i.outputPerMillion, cachedInputPerMillion: i.cachedInputPerMillion ?? null, effectiveFrom: stored, source: i.source, provisional: false });
      stamp(e);
      return Promise.resolve(result({}, record("model.catalog_price_set", `Price set for ${refOf(e)}`, reason, refOf(e))));
    },
    setRegions: (i) => {
      const reason = reasonOf(i.reason);
      const next = parseRegions(i.customerRegions.join(" "));
      next.forEach((r, n) => {
        if (!validRegionId(r)) invalid(`$.customer_regions[${String(n)}]`, `'${r}' is not a lower-case region id`);
      });
      if (JSON.stringify(next) === JSON.stringify(regions)) return Promise.resolve(nothing());
      const widened = regionsWiden(entries.map((e) => ({ ref: refOf(e), processing: e.processing, status: e.status })), regions, next);
      regions = next;
      let at = record("model.catalog_regions_set", "Customer regions set", reason);
      for (const ref of widened) {
        const e = lookup(ref);
        if (e !== undefined) at = setStatus(e, "paused", "model.catalog_paused", "Paused", reason);
      }
      return Promise.resolve(result({ repaused: widened, pausedBecause: widened.length > 0 ? "regions_widened" : null }, at));
    },
    assign: (i) => {
      const reason = reasonOf(i.reason);
      const known = i.scope === "agent" ? AGENTS.some((a) => a.id === i.id) : AGENTS.some((a) => a.role === i.id);
      if (!known) invalid("$.id", `no ${i.scope} '${i.id}' in the organization`);
      const key = `${i.scope}:${i.id}`;
      if (i.model !== null) {
        const e = lookup(i.model);
        if (e === undefined) invalid("$.model", `model '${i.model}' is not in the approved catalog`);
        if (e.status !== "approved") invalid("$.model", `model '${i.model}' is ${e.status} in the approved catalog`);
      }
      if ((assignments.get(key) ?? null) === i.model) return Promise.resolve(nothing());
      if (i.model === null) assignments.delete(key);
      else assignments.set(key, i.model);
      return Promise.resolve(result({}, record("model.assignment_set", `${i.scope} ${i.id} now uses ${i.model ?? "its configured model"}`, reason)));
    },
    putConnection: (provider, input) => {
      admin();
      const compatible = provider === OPENAI_COMPATIBLE;
      if (!compatible && !KEYLESS.includes(provider)) throw new InvalidRequestError("this provider needs a stored key, which is not available", [{ path: "$.provider", message: "this provider needs a stored key, which is not available" }]);
      if (!compatible && (input.endpointId !== undefined || input.auth !== undefined)) invalid("$.endpoint_id", "an endpoint id is only allowed for the openai-compatible provider");
      if (compatible) {
        const idProblem = input.endpointId === undefined ? "an endpoint id is required" : endpointIdProblem(input.endpointId);
        if (idProblem !== null) invalid("$.endpoint_id", idProblem);
        if (input.auth !== "none" && input.auth !== "key") invalid("$.auth", "auth must be none or key");
        if (input.auth === "none" && input.key !== undefined) invalid("$.key", "auth none takes no key name");
        if (input.auth === "key" && (input.key === undefined || keyNameProblem(input.key) !== null)) invalid("$.key", "auth key needs a valid key name");
      }
      const hint = compatible ? openAiEndpointHint(input.endpoint, input.auth === "key") : endpointHint(input.endpoint);
      if (hint !== null) invalid("$.endpoint", hint);
      const endpoint = compatible ? input.endpoint.trim().replace(/\/$/, "") : input.endpoint.trim().replace(/\/$/, "").toLowerCase();
      const status = input.status ?? "active";
      const before = connections.find((c) => same(c, provider, input.endpointId));
      const repoint = before !== undefined && before.endpoint !== endpoint;
      if (repoint && reasonProblem(input.reason ?? "") !== null) invalid("$.reason", "a reason (3 to 500 characters) is required to repoint a connection");
      if (input.reason !== undefined && input.reason !== "" && reasonProblem(input.reason) !== null) invalid("$.reason", SERVER_REASON);
      const auth = compatible ? (input.auth ?? "none") : "entra";
      if (before?.endpoint === endpoint && before.status === status && before.auth === auth && before.keyName === input.key) return Promise.resolve({ repaused: [] });
      seq += 1;
      const next: Connection = { provider, auth, ...(input.key === undefined ? {} : { keyName: input.key }), ...(input.endpointId === undefined ? {} : { endpointId: input.endpointId }), endpoint, status, setBy: ME, setAt: now(), seq };
      history.push({ seq, type: before === undefined ? "model.connection_created" : "model.connection_updated", actor: ME, at: next.setAt, provider, ...(input.endpointId === undefined ? {} : { endpointId: input.endpointId }), valid: true, endpoint, status, auth, repoint, reason: input.reason?.trim() ?? null });
      connections = [...connections.filter((c) => !same(c, provider, input.endpointId)), next].sort((a, b) => `${a.provider}/${a.endpointId ?? ""}`.localeCompare(`${b.provider}/${b.endpointId ?? ""}`));
      const repaused: string[] = [];
      if (repoint) {
        for (const e of entries.filter((x) => x.provider === provider && x.endpointId === input.endpointId && x.status === "approved")) {
          e.status = "paused";
          stamp(e);
          record("model.catalog_paused", `Paused ${refOf(e)} (endpoint changed)`, input.reason ?? null, refOf(e));
          repaused.push(refOf(e));
        }
      }
      return Promise.resolve({ repaused });
    },
    removeConnection: (provider, _reason, endpointId) => {
      admin();
      if (!connections.some((c) => same(c, provider, endpointId))) throw new NotFoundError("connection");
      connections = connections.filter((c) => !same(c, provider, endpointId));
      seq += 1;
      history.push({ seq, type: "model.connection_removed", actor: ME, at: now(), provider, valid: true, reason: null });
      return Promise.resolve();
    },
    keyState: (t) => {
      const versions = keys.get(keyName(t));
      if (versions === undefined) throw new NotFoundError("key");
      return Promise.resolve({ versions: structuredClone(versions), keyId: `key_${keyName(t).replace(/\W+/g, "_")}` } satisfies KeyState);
    },
    setKey: (i) => {
      admin();
      keyTarget(i.provider);
      const first = !keys.has(keyName(i));
      if (first && reasonProblem(i.reason ?? "") !== null) fail(422, "reason_required", "a reason (3 to 500 characters) is required for the first key");
      if (i.value.trim() === "" || i.value.length > 2000) fail(422, "key_shape", "the key does not have the shape this provider uses");
      const list = keys.get(keyName(i)) ?? [];
      const at = now();
      for (const v of list) if (v.state === "current") v.state = "previous";
      const version = list.length + 1;
      list.push({ version, state: "current", createdAt: at, activatedAt: at, graceUntil: null });
      keys.set(keyName(i), list);
      seq += 1;
      return Promise.resolve({ keyId: `key_${keyName(i).replace(/\W+/g, "_")}`, version, state: "current", createdAt: at, first });
    },
    revokeKey: (i) => {
      admin();
      keyTarget(i.provider);
      const list = keys.get(keyName(i));
      if (list === undefined) throw new NotFoundError("key");
      if (reasonProblem(i.reason) !== null) fail(422, "reason_required", "a reason (3 to 500 characters) is required");
      const hit = list.filter((v) => v.state !== "revoked" && (i.version === undefined || v.version === i.version));
      for (const v of hit) v.state = "revoked";
      if (hit.length > 0) seq += 1;
      return Promise.resolve({ keyId: `key_${keyName(i).replace(/\W+/g, "_")}`, revokedVersions: hit.map((v) => v.version) });
    },
    keysHealth: () => Promise.resolve("ok" as const),
    testConnection: (provider, endpointId) => {
      admin();
      const link = connections.find((c) => same(c, provider, endpointId));
      if (link === undefined) throw new NotFoundError("connection");
      seq += 1;
      const refused = provider === OPENAI_COMPATIBLE && isLoopback(link.endpoint) && options.allowPrivateNetwork !== true;
      const ok = !refused && !link.endpoint.includes("down");
      const code = refused ? "refused" : ok ? "ok" : "unreachable";
      history.push({ seq, type: "model.connection_tested", actor: ME, at: now(), provider, valid: true, ok, code, statusClass: null });
      return Promise.resolve({
        provider,
        ok,
        code,
        text: refused ? PRIVATE_REFUSED : ok ? (provider === OPENAI_COMPATIBLE ? "the endpoint answered" : "the endpoint answered and accepted the Entra login") : "the endpoint could not be reached (connection failed or timed out after 10 seconds)",
        latencyMs: refused ? 0 : ok ? 42 : 10_000,
        ledgerSeq: seq,
      });
    },
  };

  const session: SessionApi = {
    current: () => Promise.resolve({ id: role === "owner" ? ME : "human:u_sample_other", role, name: role === "owner" ? "Sample admin" : "Sample viewer" }),
  };
  return { models, session };
}
