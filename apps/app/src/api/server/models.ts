import { NotFoundError, ProblemError } from "../client";
import {
  assignment,
  catalog,
  catalogEvent,
  connectionHistoryRow,
  connection,
  keyState,
  type ModelsApi,
  type RawKeyState,
  type RawSetKey,
  resolution,
  type RawAssignment,
  type RawCatalog,
  type RawCatalogEvent,
  type RawConnectionHistoryRow,
  type RawConnection,
  type RawResolution,
  type RawSession,
  type RawTestResult,
  type RawWriteResult,
  session,
  type SessionApi,
  testResult,
  writeResult,
} from "../models";
import type { HttpClient } from "./http";

const enc = encodeURIComponent;
const C = "/v1/models/catalog";

/** Drops absent optional fields: the server's bodies reject unknown fields and `null` where text belongs. */
function body(fields: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined));
}

const keyPath = (t: { provider: string; endpointId?: string }): string =>
  `/v1/models/connections/${enc(t.provider)}/key${t.endpointId === undefined ? "" : `?endpoint_id=${enc(t.endpointId)}`}`;

/** The model screens' calls, one per route of spec/model-governance section 10.3. */
export function createModelsApi(http: HttpClient): ModelsApi {
  const write = async (path: string, fields: Record<string, unknown>) =>
    writeResult(await http.post<RawWriteResult>(path, body(fields)));
  // `endpoint_id` names the connection of an openai-compatible entry; absent (dropped) for every other provider.
  const ref = (i: { provider: string; endpointId?: string; model: string; reason: string }) => ({
    provider: i.provider,
    endpoint_id: i.endpointId,
    model: i.model,
    reason: i.reason,
  });
  return {
    catalog: async () => catalog(await http.get<RawCatalog>(C)),
    events: async (before) => {
      const raw = await http.get<{ events: RawCatalogEvent[]; next_before?: number | null }>(`${C}/events${before === undefined ? "" : `?before=${String(before)}`}`);
      return { events: raw.events.map(catalogEvent), nextBefore: raw.next_before ?? null };
    },
    connectionHistory: async (query) => {
      const params = new URLSearchParams();
      if (query.provider !== undefined) params.set("provider", query.provider);
      if (query.endpointId !== undefined) params.set("endpoint_id", query.endpointId);
      if (query.before !== undefined) params.set("before", String(query.before));
      if (query.limit !== undefined) params.set("limit", String(query.limit));
      const text = params.toString();
      const raw = await http.get<{ rows: RawConnectionHistoryRow[]; next_before?: number | null }>(`/v1/models/connections/events${text === "" ? "" : `?${text}`}`);
      return { rows: raw.rows.map(connectionHistoryRow), nextBefore: raw.next_before ?? null };
    },
    assignments: async () => (await http.get<{ assignments: RawAssignment[] }>("/v1/models/assignments")).assignments.map(assignment),
    connections: async () => (await http.get<{ connections: RawConnection[] }>("/v1/models/connections")).connections.map(connection),
    resolution: async (dataClass) => resolution(await http.get<RawResolution>(`/v1/models/resolution?data_class=${enc(dataClass)}`)),
    addEntry: (i) => write(`${C}/entries`, { ...ref(i), family: i.family, processing: i.processing }),
    editEntry: (i) => write(`${C}/entries/edit`, { ...ref(i), family: i.family, processing: i.processing }),
    approve: (i) => write(`${C}/entries/approve`, ref(i)),
    pause: (i) => write(`${C}/entries/pause`, ref(i)),
    retire: (i) => write(`${C}/entries/retire`, ref(i)),
    remove: (i) => write(`${C}/entries/remove`, ref(i)),
    setPrice: (i) =>
      write(`${C}/prices`, {
        ...ref(i),
        input_per_million: i.inputPerMillion,
        output_per_million: i.outputPerMillion,
        cached_input_per_million: i.cachedInputPerMillion,
        cache_write_per_million: i.cacheWritePerMillion,
        effective_from: i.effectiveFrom,
        source: i.source,
      }),
    setRegions: async (i) => writeResult(await http.put<RawWriteResult>(`${C}/regions`, body({ customer_regions: i.customerRegions, reason: i.reason }))),
    // scope and id are in the path of the upsert; `model: null` clears the assignment.
    assign: async (i) => writeResult(await http.put<RawWriteResult>(`/v1/models/assignments/${enc(i.scope)}/${enc(i.id)}`, body({ model: i.model, reason: i.reason }))),
    putConnection: async (provider, input) => {
      const raw = await http.put<{ repaused?: string[] }>(
        `/v1/models/connections/${enc(provider)}`,
        body({ endpoint: input.endpoint, status: input.status, reason: input.reason, endpoint_id: input.endpointId, auth: input.auth, key: input.key }),
      );
      return { repaused: raw.repaused ?? [] };
    },
    removeConnection: async (provider, reason, endpointId) => {
      await http.post(`/v1/models/connections/${enc(provider)}/remove${endpointId === undefined ? "" : `?endpoint_id=${enc(endpointId)}`}`, body({ reason }));
    },
    testConnection: async (provider, endpointId) =>
      testResult(await http.post<RawTestResult>(`/v1/models/connections/${enc(provider)}/test${endpointId === undefined ? "" : `?endpoint_id=${enc(endpointId)}`}`, {})),
    keyState: async (t) => keyState(await http.get<RawKeyState>(keyPath(t))),
    setKey: async (i) => {
      const raw = await http.put<RawSetKey>(keyPath(i), body({ value: i.value, grace_seconds: i.graceSeconds, reason: i.reason }));
      // 201 vs 200 is not visible through the http client; a first key is the one with version 1.
      return { keyId: raw.key_id, version: raw.version, state: raw.state, createdAt: raw.created_at, first: raw.version === 1 };
    },
    revokeKey: async (i) => {
      const raw = await http.delete<{ key_id: string; revoked_versions: number[] }>(keyPath(i), body({ version: i.version, reason: i.reason }));
      return { keyId: raw.key_id, revokedVersions: raw.revoked_versions };
    },
    keysHealth: async () => ((await http.get<{ store: string }>("/v1/models/keys/health")).store === "ok" ? "ok" : "unavailable"),
  };
}

/** `GET /v1/auth/session`: nobody signed in (or a server without sign-in) is `null`, never an error. */
export function createSessionApi(http: HttpClient): SessionApi {
  return {
    current: async () => {
      try {
        return session(await http.get<RawSession>("/v1/auth/session"));
      } catch (error) {
        if (error instanceof ProblemError && error.status !== 0) return null;
        if (error instanceof NotFoundError) return null;
        throw error;
      }
    },
  };
}
