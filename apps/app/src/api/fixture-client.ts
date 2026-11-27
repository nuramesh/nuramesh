/**
 * The in-memory client: the design's sample data plus enough behaviour that every screen can be
 * built and tested without a server. It is stateful (deciding an approval, unblocking a budget,
 * saving a template all change what is read next) and every change appends a ledger entry and
 * returns its number, like the server will. Each client starts from a fresh copy of the seed.
 */
import { createFixtureAuth, type FixtureAuthOptions } from "@/auth/fixture-auth";
import { BUILT_IN_TEMPLATES } from "@/builder/templates";
import { blankDocument } from "@/builder/document";
import { SLUG_PATTERN, parseOrg, pyStrip, type Org } from "@/model/org";
import { validateOrg } from "@/model/validate";

import { taskAuditF1, taskAuditRun128 } from "./fixtures/task-audit";
import { taskAudit } from "./server/map";
import {
  type ApiClient,
  ConflictError,
  InvalidRequestError,
  NotAllowedError,
  NotAvailableError,
  NotFoundError,
  ProblemError,
} from "./client";
import {
  APPROVALS,
  BUDGETS,
  BUDGET_SUMMARY,
  FIXTURE_NOW,
  GATE_PRESETS,
  LEDGER,
  LEDGER_ANCHOR,
  LEDGER_CHECKED_AT,
  LEDGER_HEAD,
  MODEL_SPEND,
  POLICY_PRESETS,
  RUNS,
  RUN_128,
  RUN_EVENTS_128,
  TEMPLATE_SUMMARIES,
} from "./fixtures/seed";
import type { Invite } from "./invites";
import { DASHBOARD_FIXTURES } from "./fixtures/dashboards";
import { DOWNGRADE_REPORT } from "./fixtures/downgrade";
import { type OrgSafetyScenario, type OrgSafetyState, seedOrgSafety } from "./fixtures/org-safety";
import { compareMoney, toMicros, usd, percentUsed } from "./money";
import { downgradeReport } from "./reports";
import { tokenSavings } from "./savings";
import { tokenSavingsFixture } from "./fixtures/savings";
import { createFixtureModels, type FixtureRole } from "./fixtures/models";
import type {
  Approval,
  BreakerRow,
  BudgetRow,
  ChainStatus,
  EventCategory,
  LedgerCategory,
  LedgerEntry,
  Mutation,
  OrgRole,
  OrgSnapshot,
  PolicyPreset,
  PresetSet,
  RunDetail,
  RunEvent,
  RunSummary,
  TemplateVersionInfo,
  Tone,
  AgentCredential,
  AgentScope,
  IssuedAgentCredential,
} from "./types";

export interface FixtureOptions {
  /** The task audit view is not available (R1.27): `runs.taskAudit` rejects with `NotAvailableError`. */
  taskAuditUnavailable?: boolean;
  /** The clock; each change advances it a second from the sample "now" by default. */
  now?: () => string;
  /** Who the sample session is: an admin by default; `viewer` shows the read-only model screens. */
  role?: FixtureRole;
  /** Makes the sample app ask for a sign-in (tests and screenshots of the login screens). Without it there is none. */
  signIn?: FixtureAuthOptions;
  /** Which halt, role pause and breaker state to start from (default `running`; the organization safety notes section 12). */
  orgSafety?: OrgSafetyScenario;
  /**
   * Agent credentials (E2): the starting list and who is asking. A non-owner gets
   * `NotAllowedError` on every call, like the server (default `owner`, no credentials).
   */
  agentAccess?: { credentials?: AgentCredential[]; role?: "owner" | "approver" | "viewer" };
}

export type { OrgSafetyScenario } from "./fixtures/org-safety";

/** Runs `fn` and returns its result as a promise; anything it throws becomes a rejection. */
function settle<T>(fn: () => T): Promise<T> {
  try {
    return Promise.resolve(fn());
  } catch (error) {
    return Promise.reject(error instanceof Error ? error : new Error(String(error)));
  }
}

function copy<T>(value: T): T {
  return structuredClone(value);
}

/** A stable 64-hex-digit hash of any JSON value (the fixture stands in for the real SHA-256). */
function digest(value: unknown): string {
  const text = JSON.stringify(value);
  let h = 0x811c9dc5;
  let out = "";
  for (let round = 0; round < 8; round += 1) {
    for (let i = 0; i < text.length; i += 1) h = Math.imul(h ^ text.charCodeAt(i), 0x01000193) >>> 0;
    h = Math.imul(h ^ round, 0x01000193) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return out;
}

/** Plain data as YAML. JSON strings are valid YAML double-quoted scalars, so this is exact. */
function toYaml(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return (value as unknown[])
      .map((item) => {
        const inner = toYaml(item, indent + 1);
        return isFilled(item) ? `${pad}-\n${inner}` : `${pad}- ${inner}`;
      })
      .join("\n");
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return "{}";
    return entries
      .map(([key, item]) =>
        isFilled(item)
          ? `${pad}${key}:\n${toYaml(item, indent + 1)}`
          : `${pad}${key}: ${toYaml(item, indent + 1)}`,
      )
      .join("\n");
  }
  return JSON.stringify(value);
}

/** A non-empty array or object (printed on its own lines). */
function isFilled(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  return typeof value === "object" && value !== null && Object.keys(value).length > 0;
}

interface SavedTemplate {
  id: string;
  versions: { info: TemplateVersionInfo; org: Org }[];
}

/** A stand-in for the keyed hash the server stores for a reason: 64 hex characters, the same words give the same hash. */
function fakeHash(text: string): string {
  let h = 0x811c9dc5;
  let out = "";
  for (let i = 0; out.length < 64; i++) {
    h = Math.imul(h ^ (text.charCodeAt(i % Math.max(text.length, 1)) + i), 0x01000193) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return out.slice(0, 64);
}

export function createFixtureClient(options: FixtureOptions = {}): ApiClient {
  let clock = 0;
  const now =
    options.now ??
    ((): string => {
      clock += 1;
      return new Date(Date.parse(FIXTURE_NOW) + clock * 1000).toISOString().replace(".000Z", "Z");
    });

  // ---- state ----------------------------------------------------------------------------
  const first = BUILT_IN_TEMPLATES[0];
  if (first === undefined) throw new Error("no built-in templates");
  // the design's organization is called "example-org"
  let org: Org = { ...copy(first.document.org), id: "example-org", name: "example-org" };
  let orgHash = digest(org);
  const runs: RunDetail[] = copy(RUNS);
  const events = new Map<string, RunEvent[]>([["run-128", copy(RUN_EVENTS_128)]]);
  const listeners = new Map<string, Set<(event: RunEvent) => void>>();
  let approvals: Approval[] = copy(APPROVALS);
  let budgets: BudgetRow[] = copy(BUDGETS);
  let breach = copy(BUDGET_SUMMARY.breach);
  let runCounts = copy(BUDGET_SUMMARY.runs);
  let ledger: LedgerEntry[] = copy(LEDGER);
  let ledgerHead = LEDGER_HEAD;
  let checkedAt = LEDGER_CHECKED_AT;
  let policyPresets: PolicyPreset[] = copy(POLICY_PRESETS);
  const assignments = new Map<string, { policy: string[]; gate: string | null }>([
    ["developer", { policy: ["write-code-no-merge"], gate: "python-tests-sandbox" }],
    ["reviewer", { policy: ["read-only"], gate: null }],
  ]);
  const saved: SavedTemplate[] = [];
  let nextRun = 129;

  // organization safety (E19): the sample person who presses the buttons, and the scenario's state
  const PERSON = "human:ann";
  const safety: OrgSafetyState = seedOrgSafety(options.orgSafety);

  // agent credentials (E2)
  const credentials: AgentCredential[] = copy(options.agentAccess?.credentials ?? []);
  let nextCredential = 1;
  const accessRole = options.agentAccess?.role ?? "owner";
  const invites: Invite[] = [];
  const refreshInvite = (row: Invite): Invite => {
    if (row.status === "pending" && Date.parse(row.expiresAt) <= Date.parse(isoNow())) row.status = "expired";
    return row;
  };
  const findInvite = (id: string): Invite => {
    const row = invites.find((i) => i.id === id);
    if (row === undefined) throw new NotFoundError("that invitation");
    return refreshInvite(row);
  };
  function requireOwner(): void {
    if (accessRole !== "owner") throw new NotAllowedError("Only an owner can manage agent access", "forbidden");
  }
  /** The server's 409: halted, or this agent's role paused. */
  function requireNotSuspended(agent: string): void {
    const role = org.agents.find((a) => a.id === agent)?.role;
    if (safety.halt.halted || safety.halt.roles.some((r) => r.role === role && r.paused))
      throw new ConflictError("the organization is halted or this agent's role is paused; credentials can be revoked but not issued or renewed", "agent_suspended");
  }
  function refreshState(c: AgentCredential): AgentCredential {
    c.state = c.revokedAt !== null ? "revoked" : Date.parse(c.expiresAt) <= Date.parse(isoNow()) ? "expired" : "active";
    return c;
  }
  function makeCredential(agent: string, scopes: AgentScope[], days: number): IssuedAgentCredential {
    const created = isoNow();
    const id = `agc_${String(nextCredential).padStart(24, "0")}`;
    nextCredential += 1;
    const record: AgentCredential = {
      id, agent, scopes: [...scopes].sort(), state: "active", createdAt: created,
      expiresAt: new Date(Date.parse(created) + days * 86_400_000).toISOString().replace(".000Z", "Z"),
      revokedAt: null, issuerErased: false, inConfig: true,
    };
    credentials.push(record);
    return { ...record, secret: `nma_${"f".repeat(43)}` }; // obviously fake, one repeated character
  }

  // ---- helpers --------------------------------------------------------------------------
  /** The server's rule: only a signed-in person may halt, resume, pause, reset, disable or enable. */
  function requirePerson(): void {
    if (!safety.halt.canAct) throw new NotAllowedError("Only a signed-in person can do this", "human_required");
  }
  const isoNow = (): string => now().replace("Z", "+00:00");
  function findRole(name: string): OrgRole {
    const found = safety.halt.roles.find((r) => r.role === name);
    if (found === undefined) throw new NotFoundError(`Role ${name}`);
    return found;
  }
  function findRule(name: string): BreakerRow {
    const found = safety.breakers.find((b) => b.name === name);
    if (found === undefined) throw new NotFoundError(`Breaker ${name}`);
    return found;
  }
  function updateRule(name: string, patch: Partial<BreakerRow>): void {
    safety.breakers = safety.breakers.map((b) => (b.name === name ? { ...b, ...patch } : b));
  }
  /** A ledger entry for a safety action by the sample person; returns its number as the API's `ledger_seq`. */
  function recordSafety(kind: string, summary: string, fields: [string, string][] = []): number {
    return Number(record(kind, "decisions", PERSON, summary, fields));
  }

  function record(
    kind: string,
    category: LedgerCategory,
    actor: string,
    summary: string,
    fields: [string, string][] = [],
  ): string {
    ledgerHead += 1;
    const previous = ledger[0];
    const n = String(ledgerHead);
    const entry: LedgerEntry = {
      n,
      label: n,
      at: now(),
      kind,
      category,
      actor,
      summary,
      cost: null,
      hash: digest({ n, kind, summary, prev: previous?.hash }),
      prevHash: previous?.hash ?? "0".repeat(64),
      stream: `org:${org.id}`,
      taskId: null,
      fields: fields.map(([label, value]) => ({ label, value })),
    };
    ledger = [entry, ...ledger];
    return n;
  }

  function emit(runId: string, category: EventCategory, kind: string, text: string, tone: Tone): void {
    const list = events.get(runId) ?? [];
    const event: RunEvent = { seq: (list.at(-1)?.seq ?? 0) + 1, at: now(), kind, category, text, tone };
    events.set(runId, [...list, event]);
    listeners.get(runId)?.forEach((listener) => {
      listener(copy(event));
    });
  }

  function findRun(id: string): RunDetail {
    const run = runs.find((r) => r.id === id);
    if (run === undefined) throw new NotFoundError(`Run ${id}`);
    return run;
  }

  function summary(run: RunDetail): RunSummary {
    const { id, label, title, status, template, startedAt, startedBy, tasks, gates, spend } = run;
    return copy({ id, label, title, status, template, startedAt, startedBy, tasks, gates, spend });
  }

  function requireReason(reason: string): string {
    const trimmed = pyStrip(reason);
    if (Array.from(reason).length > 1_000) {
      // the server counts the raw text (it doesn't trim), so a padded reason is refused too
      throw new InvalidRequestError("The reason is too long", [
        { path: "reason", message: "At most 1,000 characters" },
      ]);
    }
    if (trimmed.length === 0) {
      throw new InvalidRequestError("A reason is required", [
        { path: "reason", message: "Say why: the reason is recorded in the ledger" },
      ]);
    }
    return trimmed;
  }

  function snapshot(): OrgSnapshot {
    return { org: copy(org), versionHash: orgHash, configFile: "nuramesh.yaml", gitRef: "main" };
  }

  function presetSet(): PresetSet {
    const roles = (id: string, kind: "policy" | "gate"): string[] =>
      [...assignments.entries()]
        .filter(([, a]) => (kind === "policy" ? a.policy.includes(id) : a.gate === id))
        .map(([role]) => role)
        .sort();
    return {
      policy: policyPresets.map((p) => ({ ...copy(p), usedByRoles: roles(p.id, "policy") })),
      gate: GATE_PRESETS.map((g) => ({ ...copy(g), usedByRoles: roles(g.id, "gate") })),
    };
  }

  /** Checks that every entry's previous hash is the hash of the entry just before it. */
  function brokenLinks(): number {
    let broken = 0;
    for (let i = 0; i + 1 < ledger.length; i += 1) {
      const newer = ledger[i];
      const older = ledger[i + 1];
      if (newer !== undefined && older !== undefined && Number(newer.n) === Number(older.n) + 1 && newer.prevHash !== older.hash) {
        broken += 1;
      }
    }
    return broken;
  }

  function chain(): ChainStatus {
    const broken = brokenLinks();
    return {
      verified: broken === 0,
      entries: ledgerHead,
      gaps: broken,
      anchor: copy(LEDGER_ANCHOR),
      checkedAt,
    };
  }

  function budgetStatus(spent: string, limit: string): Pick<BudgetRow, "status" | "percentUsed"> {
    const s = usd(spent);
    const l = usd(limit);
    const percent = percentUsed(s, l);
    return { percentUsed: percent, status: compareMoney(s, l) >= 0 ? "blocked" : percent >= 75 ? "near" : "ok" };
  }

  // ---- the client -----------------------------------------------------------------------
  const api: ApiClient = {
    source: "fixtures",
    ...(options.signIn === undefined ? {} : { auth: createFixtureAuth(options.signIn) }),

    orgs: {
      get: () => Promise.resolve(snapshot()),
      update: (next, expectedHash) => {
        if (expectedHash !== orgHash) {
          return Promise.reject(new ConflictError("The organization changed since you opened it"));
        }
        const issues = validateOrg(next.roles, next.agents).filter((i) => i.severity === "error");
        if (issues.length > 0) {
          return Promise.reject(
            new InvalidRequestError(
              "The organization has errors",
              issues.map((i) => ({ path: i.subject ?? "org", message: i.message })),
            ),
          );
        }
        org = copy(next);
        orgHash = digest(org);
        const entry = record("org.updated", "decisions", "You", `organization ${org.name} updated`);
        return Promise.resolve({ result: snapshot(), ledgerEntry: entry });
      },
    },

    config: {
      validate: (candidate) => {
        const issues = validateOrg(candidate.roles, candidate.agents).map((i) => ({
          path: i.subject ?? "org",
          message: i.message,
          severity: i.severity,
        }));
        return Promise.resolve({ valid: !issues.some((i) => i.severity === "error"), issues });
      },
      export: (format) =>
        format === "json"
          ? Promise.resolve(JSON.stringify(org, null, 2))
          : Promise.resolve(toYaml(org)),
      import: (text, format) => {
        if (format === "yaml") {
          return Promise.reject(new NotAvailableError("Importing YAML (needs the builder's YAML support or the server)"));
        }
        let raw: unknown;
        try {
          raw = JSON.parse(text);
        } catch {
          return Promise.reject(new InvalidRequestError("That isn't valid JSON"));
        }
        const parsed = parseOrg(raw);
        if (!parsed.ok) {
          return Promise.reject(new InvalidRequestError("That isn't a valid organization", parsed.errors));
        }
        org = copy(parsed.value);
        orgHash = digest(org);
        const entry = record("org.imported", "decisions", "You", `organization ${org.name} imported`);
        return Promise.resolve({ result: snapshot(), ledgerEntry: entry });
      },
    },

    templates: {
      list: () =>
        Promise.resolve([
          ...copy(TEMPLATE_SUMMARIES),
          ...saved.flatMap((t) => {
            const last = t.versions.at(-1);
            if (last === undefined) return [];
            const latest = last.info;
            return [{
              id: t.id,
              name: latest.name,
              description: latest.description,
              availability: "available" as const,
              origin: "mine" as const,
              roles: last.org.roles.map((r) => r.name),
              meta: `v${String(latest.version)}`,
              latestVersion: latest.version,
              updatedAt: latest.savedAt,
            }];
          }),
        ]),
      get: (id) => {
        const mine = saved.find((t) => t.id === id);
        const summary = TEMPLATE_SUMMARIES.find((t) => t.id === id);
        if (summary !== undefined) return Promise.resolve({ ...copy(summary), versions: [] });
        const last = mine?.versions.at(-1);
        if (mine === undefined || last === undefined) return Promise.reject(new NotFoundError(`Template ${id}`));
        return Promise.resolve({
          id,
          name: last.info.name,
          description: last.info.description,
          availability: "available" as const,
          origin: "mine" as const,
          roles: last.org.roles.map((r) => r.name),
          meta: `v${String(last.info.version)}`,
          latestVersion: last.info.version,
          updatedAt: last.info.savedAt,
          versions: mine.versions.map((v) => copy(v.info)),
        });
      },
      use: (id, version) => {
        const mine = saved.find((t) => t.id === id);
        if (mine !== undefined) {
          const found = version === undefined ? mine.versions.at(-1) : mine.versions.find((v) => v.info.version === version);
          if (found === undefined) return Promise.reject(new NotFoundError(`Version ${String(version)} of ${id}`));
          return Promise.resolve(copy(found.org));
        }
        const summary = TEMPLATE_SUMMARIES.find((t) => t.id === id);
        if (summary === undefined) return Promise.reject(new NotFoundError(`Template ${id}`));
        if (summary.availability === "planned") {
          return Promise.reject(new NotAllowedError(`${summary.name} is planned, not available yet`));
        }
        if (id === "blank") return Promise.resolve(copy(blankDocument().org));
        const builtIn = BUILT_IN_TEMPLATES.find((t) => t.id === id);
        if (builtIn === undefined) return Promise.reject(new NotFoundError(`Template ${id}`));
        return Promise.resolve(copy(builtIn.document.org));
      },
      saveAs: ({ id, name, description }) => {
        const cleaned = pyStrip(name);
        const errors: { path: string; message: string }[] = [];
        if (!SLUG_PATTERN.test(id)) errors.push({ path: "id", message: "Use lower-case letters, digits, - and _" });
        if (cleaned.length === 0 || cleaned.length > 120) errors.push({ path: "name", message: "1 to 120 characters" });
        if (TEMPLATE_SUMMARIES.some((t) => t.id === id)) errors.push({ path: "id", message: "That id belongs to a built-in template" });
        if (errors.length > 0) return Promise.reject(new InvalidRequestError("The template isn't valid", errors));
        let template = saved.find((t) => t.id === id);
        const latest = template?.versions.at(-1);
        const documentHash = digest(org);
        const note = description.trim();
        if (
          latest?.info.documentHash === documentHash &&
          latest.info.name === cleaned &&
          latest.info.description === note
        ) {
          return Promise.reject(new InvalidRequestError(`Nothing changed since version ${String(latest.info.version)}`));
        }
        const info: TemplateVersionInfo = {
          templateId: id,
          version: (latest?.info.version ?? 0) + 1,
          name: cleaned,
          description: note,
          savedAt: now(),
          documentHash,
        };
        if (template === undefined) {
          template = { id, versions: [] };
          saved.push(template);
        }
        template.versions.push({ info, org: copy(org) });
        const entry = record("template.saved", "decisions", "You", `template ${cleaned} saved as v${String(info.version)}`, [
          ["Template", id],
          ["Version", String(info.version)],
          ["Document hash", documentHash.slice(0, 12)],
        ]);
        return Promise.resolve({ result: copy(info), ledgerEntry: entry });
      },
      listVersions: (id) => {
        const template = saved.find((t) => t.id === id);
        if (template === undefined) return Promise.reject(new NotFoundError(`Template ${id}`));
        return Promise.resolve(template.versions.map((v) => copy(v.info)));
      },
      versionYaml: (id, version) => {
        const found = saved.find((t) => t.id === id)?.versions.find((v) => v.info.version === version);
        if (found === undefined) return Promise.reject(new NotFoundError(`Version ${String(version)} of ${id}`));
        return Promise.resolve(toYaml({ template: found.info, document: { org: found.org } }));
      },
    },

    policyPresets: {
      list: () => Promise.resolve(presetSet()),
      get: (id) => {
        const set = presetSet();
        const found = [...set.policy, ...set.gate].find((p) => p.id === id);
        return found === undefined ? Promise.reject(new NotFoundError(`Preset ${id}`)) : Promise.resolve(found);
      },
      create: (preset) => {
        const errors: { path: string; message: string }[] = [];
        if (!SLUG_PATTERN.test(preset.id)) errors.push({ path: "id", message: "Use lower-case letters, digits, - and _" });
        if (policyPresets.some((p) => p.id === preset.id)) errors.push({ path: "id", message: "That id is taken" });
        if (pyStrip(preset.name).length === 0) errors.push({ path: "name", message: "A name is required" });
        if (errors.length > 0) return Promise.reject(new InvalidRequestError("The preset isn't valid", errors));
        const created: PolicyPreset = { ...copy(preset), name: pyStrip(preset.name), usedByRoles: [] };
        policyPresets = [...policyPresets, created];
        const entry = record("preset.created", "decisions", "You", `policy preset ${created.name} created`, [["Preset", created.id]]);
        return Promise.resolve({ result: copy(created), ledgerEntry: entry });
      },
      update: (id, patch) => {
        const current = policyPresets.find((p) => p.id === id);
        if (current === undefined) return Promise.reject(new NotFoundError(`Preset ${id}`));
        const name = patch.name === undefined ? current.name : pyStrip(patch.name);
        if (name.length === 0 || name.length > 120) {
          return Promise.reject(new InvalidRequestError("The preset isn't valid", [{ path: "name", message: "1 to 120 characters" }]));
        }
        const updated: PolicyPreset = { ...current, name, description: patch.description ?? current.description, rules: patch.rules ? copy(patch.rules) : current.rules };
        policyPresets = policyPresets.map((p) => (p.id === id ? updated : p));
        const affected = presetSet().policy.find((p) => p.id === id)?.usedByRoles ?? [];
        const entry = record("preset.changed", "decisions", "You", `policy preset ${name} edited`, [
          ["Preset", id],
          ["Affects roles", affected.join(", ") || "none"],
        ]);
        return Promise.resolve({ result: { ...copy(updated), usedByRoles: affected }, ledgerEntry: entry });
      },
      assign: (role, presetId, kind) => {
        if (!org.roles.some((r) => r.id === role)) return Promise.reject(new NotFoundError(`Role ${role}`));
        const known = kind === "policy" ? policyPresets.some((p) => p.id === presetId) : GATE_PRESETS.some((g) => g.id === presetId);
        if (presetId !== null && !known) return Promise.reject(new NotFoundError(`Preset ${presetId}`));
        const current = assignments.get(role) ?? { policy: [], gate: null };
        if (kind === "policy") {
          current.policy = presetId === null ? [] : [...new Set([...current.policy, presetId])];
        } else {
          current.gate = presetId;
        }
        assignments.set(role, current);
        const entry = record("preset.assigned", "decisions", "You", `${kind} preset ${presetId ?? "none"} set for role ${role}`, [["Role", role]]);
        return Promise.resolve({ result: presetSet(), ledgerEntry: entry });
      },
    },

    runs: {
      list: () => Promise.resolve(runs.map(summary)),
      listPage: (filter) =>
        Promise.resolve({
          runs: runs.map(summary).filter((r) => filter?.status === undefined || r.status === filter.status),
          truncated: false,
        }),
      get: (id) => settle(() => copy(findRun(id))),
      taskAudit: (runId, taskId) => {
        if (options.taskAuditUnavailable === true) return Promise.reject(new NotAvailableError("runs.taskAudit"));
        const raw = runId === "r1" && taskId === "t1" ? taskAuditF1() : runId === "run-128" ? taskAuditRun128(taskId) : null;
        if (raw === null) return Promise.reject(new NotFoundError(`Task ${taskId}`));
        return settle(() => taskAudit(raw));
      },
      start: ({ template, budget }) => {
        if (toMicros(budget) <= 0n) {
          return Promise.reject(new InvalidRequestError("The run needs a budget above zero", [{ path: "budget", message: "Must be more than $0" }]));
        }
        const label = String(nextRun);
        nextRun += 1;
        const run: RunDetail = {
          ...copy(RUN_128),
          id: `run-${label}`,
          label: `#${label}`,
          title: `New ${template} run`,
          status: "running",
          template,
          startedAt: now(),
          tasks: { done: 0, total: 3 },
          gates: { passed: 0, failed: 0, total: 5 },
          spend: { spent: usd("0.00"), limit: copy(budget) },
          elapsedSeconds: 0,
          needsYou: 0,
          latestGate: null,
          graph: { nodes: copy(RUN_128.graph.nodes).map((n) => ({ ...n, status: "idle", statusText: "not started" })), edges: copy(RUN_128.graph.edges).map((e) => ({ ...e, state: "idle" })) },
        };
        runs.unshift(run);
        events.set(run.id, []);
        const entry = record("run.started", "tasks", "You", `run ${run.label} started`, [["Template", template]]);
        emit(run.id, "tasks", "run.started", `Run ${run.label} started by You`, "muted");
        return Promise.resolve({ result: summary(run), ledgerEntry: entry });
      },
      // Sample data has no runner: a plan cannot become a run here, and no run is invented.
      startPlan: () => Promise.reject(new NotAvailableError("runs.startPlan")),
      startStatus: () => Promise.reject(new NotAvailableError("runs.startStatus")),
      pause: (id) =>
        settle(() => {
          const run = findRun(id);
          if (run.status !== "running" && run.status !== "waiting") {
            throw new NotAllowedError(`A ${run.status} run can't be paused`);
          }
          run.status = "paused";
          const entry = record("run.paused", "tasks", "You", `run ${run.label} paused`);
          emit(id, "tasks", "run.paused", `Run ${run.label} paused by You`, "warm");
          return { result: summary(run), ledgerEntry: entry };
        }),
      stop: (id) =>
        settle(() => {
          const run = findRun(id);
          if (["done", "failed", "stopped"].includes(run.status)) {
            throw new NotAllowedError(`A ${run.status} run can't be stopped`);
          }
          run.status = "stopped";
          const entry = record("run.cancelled", "tasks", "You", `run ${run.label} stopped`);
          emit(id, "tasks", "run.cancelled", `Run ${run.label} stopped by You`, "warm");
          return { result: summary(run), ledgerEntry: entry };
        }),
      events: (id, afterSeq = 0) =>
        settle(() => {
          findRun(id);
          const list = (events.get(id) ?? []).filter((e) => e.seq > afterSeq);
          return { events: copy(list), nextSeq: list.at(-1)?.seq ?? afterSeq };
        }),
      subscribe: (id, onEvent) => {
        const set = listeners.get(id) ?? new Set();
        set.add(onEvent);
        listeners.set(id, set);
        return () => {
          set.delete(onEvent);
        };
      },
    },

    // Sample runs have no human gate to decide and no earned-autonomy history: both answer plainly.
    gates: {
      decide: (runId) => Promise.reject(new NotFoundError(`A human gate of run ${runId}`)),
    },
    autonomy: {
      // One sample proposal per role, so the page and the grant dialog can be tried on sample data.
      proposals: (role) =>
        Promise.resolve({
          items: [
            {
              subject: { kind: "role", id: role },
              ruleKey: 1,
              role,
              kind: "write_file",
              // the server's words: a cell that can be granted is `proposable` and has no reasons
              status: "proposable",
              reasons: [],
              approvedAsks: 4,
              solo: { approvedAsksSolo: 1, soloShare: "0.2500" },
              evidenceHash: "obviously-fake-sample-evidence-hash",
            },
          ],
          ledgerPosition: 0,
          asOf: isoNow(),
        }),
      grant: (input) => {
        try {
          requireReason(input.reason);
        } catch (error) {
          return Promise.reject(error instanceof Error ? error : new InvalidRequestError("a reason is required"));
        }
        return Promise.reject(new NotFoundError("An autonomy proposal"));
      },
    },
    approvals: {
      list: () => Promise.resolve(copy(approvals)),
      get: (id) => {
        const found = approvals.find((a) => a.id === id);
        return found === undefined ? Promise.reject(new NotFoundError(`Approval ${id}`)) : Promise.resolve(copy(found));
      },
      decide: (id, { decision, reason }) => {
        const found = approvals.find((a) => a.id === id);
        if (found === undefined) return Promise.reject(new NotFoundError(`Approval ${id}`));
        let why: string;
        try {
          why = requireReason(reason);
        } catch (error) {
          return Promise.reject(error instanceof Error ? error : new Error(String(error)));
        }
        if (found.state !== "pending") {
          return Promise.reject(new NotAllowedError("This request was already decided; decided requests are read-only"));
        }
        const state = decision === "approve" ? "approved" : "denied";
        const reasonHash = fakeHash(why); // like the server: the words are kept, only a hash is shown
        const entry = record("approval.decided", "decisions", "You", `${found.title}: ${state}`, [
          ["Approval", id],
          ["Outcome", state],
          ["Reason hash", reasonHash],
        ]);
        const decided: Approval = { ...found, state, decision: { by: "You", at: now(), reasonHash, ledgerEntry: entry } };
        approvals = approvals.map((a) => (a.id === id ? decided : a));
        const run = runs.find((r) => r.id === found.runId);
        if (run !== undefined && run.needsYou > 0) {
          run.needsYou -= 1;
          if (run.needsYou === 0) {
            const owner = run.graph.nodes.find((n) => n.isHuman);
            if (owner !== undefined) {
              owner.status = "done";
              owner.statusText = state;
            }
            // approved: the run goes on; denied: nothing is left to wait for, so it stops
            if (run.status === "waiting") run.status = state === "approved" ? "running" : "stopped";
          }
        }
        emit(found.runId, "approvals", "approval.decided", `${found.title} ${state} by You`, "accent");
        return Promise.resolve({ result: copy(decided), ledgerEntry: entry });
      },
    },

    budgets: {
      list: () => Promise.resolve(copy(budgets)),
      summary: () => Promise.resolve({ ...copy(BUDGET_SUMMARY), runs: copy(runCounts), breach: copy(breach) }),
      unblock: (budgetId, { reason, newLimit }) => {
        const found = budgets.find((b) => b.id === budgetId);
        if (found === undefined) return Promise.reject(new NotFoundError(`Budget ${budgetId}`));
        let why: string;
        try {
          why = requireReason(reason);
        } catch (error) {
          return Promise.reject(error instanceof Error ? error : new Error(String(error)));
        }
        if (found.status !== "blocked") return Promise.reject(new NotAllowedError("This budget isn't blocked"));
        let limit = found.limit;
        if (newLimit !== undefined) {
          try {
            limit = usd(newLimit);
          } catch {
            return Promise.reject(new InvalidRequestError("That isn't an amount", [{ path: "newLimit", message: "Use a number such as 60.00" }]));
          }
          if (compareMoney(limit, found.limit) <= 0) {
            return Promise.reject(new InvalidRequestError("The new limit must be above the current limit", [{ path: "newLimit", message: `Must be more than ${found.limit.amount}` }]));
          }
        }
        // unblocked with the same limit, the budget runs on at its limit (as the server does): it is
        // no longer blocked, and the next spend past the limit blocks it again
        const next: BudgetRow = { ...found, limit, ...budgetStatus(found.spent.amount, limit.amount) };
        if (next.status === "blocked") next.status = "near";
        const entry = record("budget.unblocked", "decisions", "You", `${found.scope} unblocked`, [
          ["Budget", budgetId],
          ["Reason hash", fakeHash(why)],
          ["New limit", limit.amount],
        ]);
        budgets = budgets.map((b) => (b.id === budgetId ? next : b));
        if (breach?.budgetId === budgetId) breach = null;
        const blockedRun = runs.find((r) => r.id === budgetId && r.status === "blocked");
        if (blockedRun !== undefined) {
          blockedRun.status = "running";
          runCounts = { ...runCounts, blocked: runCounts.blocked - 1, running: runCounts.running + 1 };
        }
        return Promise.resolve({ result: copy(next), ledgerEntry: entry } satisfies Mutation<BudgetRow>);
      },
    },

    spend: { byModel: () => Promise.resolve(copy(MODEL_SPEND)) },

    reports: { downgrade: () => Promise.resolve(downgradeReport(copy(DOWNGRADE_REPORT))) },

    savings: { tokens: (options) => Promise.resolve(tokenSavings(copy(tokenSavingsFixture(options?.groupBy ?? "none")))) },
    dashboards: {
      verification: () => Promise.resolve(copy(DASHBOARD_FIXTURES.verification)),
      verifiers: () => Promise.resolve(copy(DASHBOARD_FIXTURES.verifiers)),
      costLatency: () => Promise.resolve(copy(DASHBOARD_FIXTURES.costLatency)),
      approvals: () => Promise.resolve(copy(DASHBOARD_FIXTURES.approvals)),
    },

    ...createFixtureModels(options.role === undefined ? {} : { role: options.role }),

    ledger: {
      list: (filter) => {
        const needle = filter?.search?.trim().toLowerCase() ?? "";
        return Promise.resolve(
          copy(
            ledger.filter(
              (e) =>
                (filter?.category === undefined || e.category === filter.category) &&
                (needle === "" || `${e.kind} ${e.actor} ${e.summary} ${e.n}`.toLowerCase().includes(needle)),
            ),
          ),
        );
      },
      get: (n) => {
        const wanted = /^\d+$/.test(n) ? Number(n) : NaN;
        const found = ledger.find((e) => e.n === n || Number(e.n) === wanted);
        return found === undefined ? Promise.reject(new NotFoundError(`Ledger entry ${n}`)) : Promise.resolve(copy(found));
      },
      verifyChain: () => {
        checkedAt = now();
        return Promise.resolve(chain());
      },
      chainStatus: () => Promise.resolve(chain()),
      verifyRun: () => {
        checkedAt = now();
        return Promise.resolve(chain());
      },
    },
    org: {
      status: () => Promise.resolve(copy(safety.halt)),
      halt: () => {
        requirePerson();
        if (safety.halt.halted) return Promise.reject(new ConflictError("The organization is already halted", "already_halted"));
        const entry = recordSafety("org.halted", "Organization halted", [["Cause", "human"]]);
        const stopped = runs.filter((r) => r.status === "running").length;
        safety.halt = { ...safety.halt, halted: true, by: PERSON, at: isoNow(), cause: "human", rule: null, ledgerSeq: entry, phase: null, errorClass: null, failingRule: null, failedTicks: null };
        return Promise.resolve({ ...copy(safety.halt), stoppedRuns: stopped });
      },
      resume: () => {
        requirePerson();
        if (!safety.halt.halted) return Promise.reject(new ConflictError("The organization is not halted", "already_running"));
        recordSafety("org.resumed", "Organization resumed");
        safety.halt = { ...safety.halt, halted: false, by: null, at: null, cause: null, rule: null, ledgerSeq: null, phase: null, errorClass: null, failingRule: null, failedTicks: null };
        return Promise.resolve(copy(safety.halt));
      },
      pauseRole: (name) => {
        requirePerson();
        const found = findRole(name);
        if (found.paused) return Promise.reject(new ConflictError(`Role ${name} is already paused`, "already_paused"));
        const entry = recordSafety("org.role_paused", `Role ${name} paused`, [["Role", name]]);
        safety.halt = { ...safety.halt, roles: safety.halt.roles.map((r) => (r.role === name ? { ...r, paused: true, by: PERSON, at: isoNow(), cause: "human", rule: null, ledgerSeq: entry } : r)) };
        return Promise.resolve({ ...copy(safety.halt), stoppedTasks: 0 });
      },
      resumeRole: (name) => {
        requirePerson();
        const found = findRole(name);
        if (!found.paused) return Promise.reject(new ConflictError(`Role ${name} is not paused`, "not_paused"));
        recordSafety("org.role_resumed", `Role ${name} resumed`, [["Role", name]]);
        safety.halt = { ...safety.halt, roles: safety.halt.roles.map((r) => (r.role === name ? { ...r, paused: false, by: null, at: null, cause: null, rule: null, ledgerSeq: null } : r)) };
        return Promise.resolve(copy(safety.halt));
      },
    },

    breakers: {
      list: () =>
        Promise.resolve({
          org: safety.org,
          halt: copy(safety.halt),
          evaluation: {
            ...copy(safety.evaluation),
            // a sample "last checked a few seconds ago", whatever day it is
            lastEvaluatedAt: safety.evaluation.state === "not_configured" ? null : new Date(Date.now() - 4000).toISOString(),
          },
          breakers: copy(safety.breakers),
        }),
      reset: (name) => {
        requirePerson();
        const rule = findRule(name);
        if (rule.state !== "tripped") return Promise.reject(new ConflictError(`Breaker ${name} is not tripped`, "not_tripped"));
        const entry = recordSafety("breaker.reset", `Breaker ${name} reset`, [["Rule", name]]);
        const resetAt = isoNow();
        updateRule(name, { state: "ok", trippedAt: null, trippedSeq: null, trippedBy: null, manual: false, observedAs: "live", lastResetAt: resetAt });
        return Promise.resolve({ name, state: "ok" as const, resetAt, ledgerSeq: entry, orgHalted: safety.halt.halted });
      },
      disable: (name, reason) => {
        requirePerson();
        const rule = findRule(name);
        if (rule.state === "tripped") return Promise.reject(new ConflictError(`Breaker ${name} is tripped; reset it first`, "breaker_tripped"));
        if (rule.state === "disabled") return Promise.reject(new ConflictError(`Breaker ${name} is already disabled`, "already_disabled"));
        const entry = recordSafety("breaker.disabled", `Breaker ${name} disabled`, [["Rule", name], ["Reason", reason]]);
        const disabledAt = isoNow();
        updateRule(name, { state: "disabled", disabledAt, disabledBy: PERSON, disabledReason: reason });
        return Promise.resolve({ name, state: "disabled" as const, disabledAt, reason, ledgerSeq: entry });
      },
      enable: (name) => {
        requirePerson();
        const rule = findRule(name);
        if (rule.state !== "disabled") return Promise.reject(new ConflictError(`Breaker ${name} is not disabled`, "not_disabled"));
        const entry = recordSafety("breaker.enabled", `Breaker ${name} enabled`, [["Rule", name]]);
        updateRule(name, { state: "ok", disabledAt: null, disabledBy: null, disabledReason: null });
        return Promise.resolve({ name, state: "ok" as const, enabledAt: isoNow(), ledgerSeq: entry });
      },
    },
    agentCredentials: {
      agents: () =>
        settle(() => {
          requireOwner();
          return org.agents.map((a) => ({
            id: a.id,
            name: a.name,
            role: a.role,
            activeCredentials: credentials.filter((c) => c.agent === a.id && refreshState(c).state === "active").length,
          }));
        }),
      callerRole: () => settle(() => accessRole),
      list: (filter) =>
        settle(() => {
          requireOwner();
          return copy(credentials)
            .map(refreshState)
            .filter((c) => (filter?.agent === undefined || c.agent === filter.agent) && (filter?.issuerErased !== true || (c.issuerErased && c.state === "active")));
        }),
      issue: (agent, input) =>
        settle(() => {
          requireOwner();
          requireNotSuspended(agent);
          if (credentials.filter((c) => c.agent === agent && refreshState(c).state === "active").length >= 5)
            throw new ConflictError("this agent already has the maximum number of live credentials", "credential_limit");
          return makeCredential(agent, input.scopes, input.days);
        }),
      rotate: (agent, id, input) =>
        settle(() => {
          requireOwner();
          requireNotSuspended(agent);
          const old = credentials.find((c) => c.id === id && c.agent === agent);
          if (old === undefined) throw new NotFoundError(`credential ${id}`);
          const made = makeCredential(agent, old.scopes, input.days);
          if (input.graceMinutes === 0) old.revokedAt = isoNow();
          old.issuerErased = false;
          return made;
        }),
      revoke: (agent, id) =>
        settle(() => {
          requireOwner();
          const found = credentials.find((c) => c.id === id && c.agent === agent);
          if (found === undefined) throw new NotFoundError(`credential ${id}`);
          found.revokedAt ??= isoNow();
          return refreshState(copy(found));
        }),
      revokeAll: (agent) =>
        settle(() => {
          requireOwner();
          const live = credentials.filter((c) => c.agent === agent && refreshState(c).state === "active");
          live.forEach((c) => {
            c.revokedAt = isoNow();
          });
          return live.length;
        }),
    },
    emailChange: {
      start: (input) =>
        settle(() => {
          if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(input.newEmail.trim())) throw new InvalidRequestError("that is not an email address");
        }),
      // Sample data has no mail and so no link: every token is "not valid", as the server answers a wrong one.
      verify: () => Promise.reject(new ProblemError("this link is not valid", 403, "invalid_verification_token")),
    },
    invites: {
      list: () =>
        settle(() => {
          requireOwner();
          return copy(invites.map(refreshInvite)).reverse();
        }),
      create: (input) =>
        settle(() => {
          requireOwner();
          const email = input.email.trim().toLowerCase();
          if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new InvalidRequestError("that is not an email address");
          if (invites.map(refreshInvite).some((i) => i.status === "pending" && i.email === email))
            throw new ConflictError("an invitation to this address is already pending", "invite_pending");
          const at = isoNow();
          const row: Invite = {
            id: `inv_${String(invites.length + 1).padStart(20, "0")}`,
            email,
            role: input.role,
            status: "pending",
            createdBy: "human:u_fixture",
            createdAt: at,
            expiresAt: new Date(Date.parse(at) + 72 * 3_600_000).toISOString(),
            acceptedUserId: null,
            resentCount: 0,
          };
          invites.push(row);
          return copy(row);
        }),
      revoke: (id) =>
        settle(() => {
          requireOwner();
          const row = findInvite(id);
          if (row.status === "accepted") throw new ConflictError("this invitation was already accepted", "invite_not_pending");
          if (row.status === "pending") row.status = "revoked";
          return copy(row);
        }),
      resend: (id) =>
        settle(() => {
          requireOwner();
          const row = findInvite(id);
          if (row.status !== "pending") throw new ConflictError("only a pending invitation can be sent again", "invite_not_pending");
          if (row.resentCount >= 5) throw new ConflictError("this invitation was already sent again 5 times", "resend_limit");
          row.resentCount += 1;
          row.expiresAt = new Date(Date.parse(isoNow()) + 72 * 3_600_000).toISOString();
          return copy(row);
        }),
      // Sample data has no mail and so no link: every token is "not valid", as the server answers a wrong one.
      inspect: () => Promise.reject(new ProblemError("this invitation is not valid", 403, "invalid_invite")),
      accept: () => Promise.reject(new ProblemError("this invitation is not valid", 403, "invalid_invite")),
    },
    // Sample data has no mail and so no link: a request is accepted as the server does, every token is "not valid".
    passwordReset: {
      request: () => settle(() => undefined),
      redeem: () => Promise.reject(new ProblemError("this link is not valid", 403, "invalid_reset_token")),
      revokeByLink: () => Promise.reject(new ProblemError("this link is not valid", 403, "invalid_revoke_link")),
    },
    info: () => {
      const snap = snapshot();
      return Promise.resolve({ name: snap.org.name, detail: snap.gitRef });
    },
  };
  return guard(api);
}

/**
 * Every call returns a promise, whatever goes wrong inside: a throw becomes a rejection, and a
 * malformed amount (which the types allow as a string) is an `InvalidRequestError`.
 */
function guard(api: ApiClient): ApiClient {
  const wrap = <T extends object>(group: T): T => {
    const wrapped: Record<string, unknown> = {};
    for (const [name, member] of Object.entries(group)) {
      wrapped[name] =
        typeof member === "function" && name !== "subscribe"
          ? (...args: unknown[]): Promise<unknown> =>
              new Promise((resolve, reject) => {
                try {
                  resolve((member as (...a: unknown[]) => unknown)(...args));
                } catch (error) {
                  reject(
                    error instanceof RangeError
                      ? new InvalidRequestError(error.message)
                      : error instanceof Error
                        ? error
                        : new Error(String(error)),
                  );
                }
              }).catch((error: unknown) => {
                throw error instanceof RangeError ? new InvalidRequestError(error.message) : error;
              })
          : member;
    }
    return wrapped as T;
  };
  return {
    source: api.source,
    orgs: wrap(api.orgs),
    config: wrap(api.config),
    templates: wrap(api.templates),
    policyPresets: wrap(api.policyPresets),
    runs: wrap(api.runs),
    approvals: wrap(api.approvals),
    gates: wrap(api.gates),
    autonomy: wrap(api.autonomy),
    budgets: wrap(api.budgets),
    spend: wrap(api.spend),
    reports: wrap(api.reports),
    savings: wrap(api.savings),
    dashboards: wrap(api.dashboards),
    models: wrap(api.models),
    session: wrap(api.session),
    ledger: wrap(api.ledger),
    org: wrap(api.org),
    breakers: wrap(api.breakers),
    agentCredentials: wrap(api.agentCredentials),
    emailChange: wrap(api.emailChange),
    invites: wrap(api.invites),
    passwordReset: wrap(api.passwordReset),
    info: () => api.info(),
    ...(api.auth === undefined ? {} : { auth: api.auth }),
  };
}
