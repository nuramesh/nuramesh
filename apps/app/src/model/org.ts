/**
 * Organization model: roles, agents and reporting lines.
 *
 * Mirrors `nuramesh.core.org` (Python) field for field, with the same snake_case JSON keys, so an
 * org built here is accepted by `OrgGraph`. `parseOrg` applies the same defaults and field
 * constraints as the pydantic models; graph rules (one root, no cycles, ...) live in validate.ts.
 */

/** Stable identifier: lower-case letters, digits, `-` and `_`; 1–63 characters. */
export const SLUG_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/;

export const NAME_MAX = 120;
export const DESCRIPTION_MAX = 2_000;
export const INSTRUCTIONS_MAX = 50_000;
export const MODEL_MAX = 200;
export const CHECKS_MAX = 200;

/** What a role is for. Exactly one agent, the root, has the Chief of Staff role. */
export type RoleKind = "chief_of_staff" | "worker" | "verifier";
export const ROLE_KINDS: readonly RoleKind[] = ["chief_of_staff", "worker", "verifier"];

/** A job description shared by one or more agents. */
export interface Role {
  id: string;
  name: string;
  kind: RoleKind;
  description: string;
  instructions: string;
  tools: readonly string[];
  default_model: string | null;
}

/**
 * One member of the organization. `reports_to` is its manager's id (`null` for the root): the
 * management line. `checks` lists the agents whose work this agent verifies: the verification
 * line, a separate field on purpose (a verifier is never in the reporting chain of what it checks).
 */
export interface Agent {
  id: string;
  name: string;
  role: string;
  reports_to: string | null;
  model: string | null;
  checks: readonly string[];
}

/** An organization: its roles and the agents holding them. */
export interface Org {
  id: string;
  name: string;
  roles: readonly Role[];
  agents: readonly Agent[];
}

/** A problem with one field, at a JSON path such as `agents[2].reports_to`. */
export interface FieldError {
  path: string;
  message: string;
}

export type ParseResult<T> = { ok: true; value: T } | { ok: false; errors: FieldError[] };

/**
 * The whitespace pydantic's `strip_whitespace` removes. pydantic-core strips with Rust's
 * `str::trim()` (Unicode White_Space), which differs from both JavaScript's `trim()` (that also
 * strips U+FEFF) and Python's `str.strip()` (that also strips U+001C-U+001F). Checked against
 * pydantic in spec/org-fields/cases.json.
 */
const PY_SPACE = "\\t\\n\\v\\f\\r \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");

/** Strips a name exactly as the pydantic `Name` type does. */
export function pyStrip(value: string): string {
  return value.replace(PY_STRIP, "");
}

/** Number of characters as Python counts them (code points, not UTF-16 units). */
function charLength(value: string): number {
  const pairs = value.match(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g);
  return value.length - (pairs?.length ?? 0);
}

/** Field rules shared by parsing and the builder's inline validation. Each returns a message or null. */
export const fieldRules = {
  slug(value: string): string | null {
    return SLUG_PATTERN.test(value)
      ? null
      : "Use 1–63 lower-case letters, digits, '-' or '_', starting with a letter or digit";
  },
  name(value: string): string | null {
    const length = charLength(pyStrip(value));
    if (length === 0) return "Name can't be empty";
    if (length > NAME_MAX) return `Name can be at most ${String(NAME_MAX)} characters`;
    return null;
  },
  maxLength(value: string, max: number): string | null {
    return charLength(value) > max ? `At most ${String(max)} characters` : null;
  },
  model(value: string | null): string | null {
    if (value === null) return null;
    const length = charLength(value);
    if (length === 0) return "Leave empty for none, or name a model";
    return length > MODEL_MAX ? `At most ${String(MODEL_MAX)} characters` : null;
  },
} as const;

/** Every field problem in a role (empty list when valid). */
export function roleFieldErrors(role: Role, prefix = ""): FieldError[] {
  const errors: FieldError[] = [];
  const push = (field: string, message: string | null): void => {
    if (message !== null) errors.push({ path: `${prefix}${field}`, message });
  };
  push("id", fieldRules.slug(role.id));
  push("name", fieldRules.name(role.name));
  push("description", fieldRules.maxLength(role.description, DESCRIPTION_MAX));
  push("instructions", fieldRules.maxLength(role.instructions, INSTRUCTIONS_MAX));
  role.tools.forEach((tool, index) => {
    push(`tools[${String(index)}]`, fieldRules.slug(tool));
  });
  push("default_model", fieldRules.model(role.default_model));
  return errors;
}

/** Every field problem in an agent (empty list when valid). */
export function agentFieldErrors(agent: Agent, prefix = ""): FieldError[] {
  const errors: FieldError[] = [];
  const push = (field: string, message: string | null): void => {
    if (message !== null) errors.push({ path: `${prefix}${field}`, message });
  };
  push("id", fieldRules.slug(agent.id));
  push("name", fieldRules.name(agent.name));
  push("role", fieldRules.slug(agent.role));
  if (agent.reports_to !== null) push("reports_to", fieldRules.slug(agent.reports_to));
  push("model", fieldRules.model(agent.model));
  if (agent.checks.length > CHECKS_MAX) push("checks", `At most ${String(CHECKS_MAX)} agents`);
  agent.checks.forEach((id, index) => {
    push(`checks[${String(index)}]`, fieldRules.slug(id));
  });
  return errors;
}

// ---- Parsing untrusted JSON (imports, files) ------------------------------------------------

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

class Reader {
  readonly errors: FieldError[] = [];

  fail(path: string, message: string): void {
    this.errors.push({ path, message });
  }

  /** Rejects keys the Python model doesn't know (pydantic `extra="forbid"`). */
  known(obj: Json, path: string, allowed: readonly string[]): void {
    for (const key of Object.keys(obj)) {
      if (!allowed.includes(key)) this.fail(`${path}${key}`, "Unknown field");
    }
  }

  string(obj: Json, key: string, path: string, fallback?: string): string {
    const value = obj[key];
    if (value === undefined && fallback !== undefined) return fallback;
    if (typeof value !== "string") {
      this.fail(`${path}${key}`, value === undefined ? "Required" : "Must be a string");
      return "";
    }
    return value;
  }

  nullableString(obj: Json, key: string, path: string): string | null {
    const value = obj[key];
    if (value === undefined || value === null) return null;
    if (typeof value !== "string") {
      this.fail(`${path}${key}`, "Must be a string or null");
      return null;
    }
    return value;
  }

  /** An optional list of strings (absent means empty). */
  stringList(obj: Json, key: string, path: string): string[] {
    const value = obj[key];
    if (value === undefined) return [];
    if (!Array.isArray(value)) {
      this.fail(`${path}${key}`, "Must be a list");
      return [];
    }
    const out: string[] = [];
    value.forEach((item: unknown, index) => {
      if (typeof item === "string") out.push(item);
      else this.fail(`${path}${key}[${String(index)}]`, "Must be a string");
    });
    return out;
  }

  check(path: string, message: string | null): void {
    if (message !== null) this.fail(path, message);
  }
}

const ROLE_KEYS = [
  "id",
  "name",
  "kind",
  "description",
  "instructions",
  "tools",
  "default_model",
] as const;
const AGENT_KEYS = ["id", "name", "role", "reports_to", "model", "checks"] as const;
const ORG_KEYS = ["id", "name", "roles", "agents"] as const;

function readRole(r: Reader, raw: unknown, path: string): Role | null {
  if (!isObject(raw)) {
    r.fail(path, "Must be an object");
    return null;
  }
  const p = `${path}.`;
  r.known(raw, p, ROLE_KEYS);
  const id = r.string(raw, "id", p);
  const name = pyStrip(r.string(raw, "name", p));
  const kindRaw = raw.kind === undefined ? "worker" : raw.kind;
  let kind: RoleKind = "worker";
  if (ROLE_KINDS.includes(kindRaw as RoleKind)) kind = kindRaw as RoleKind;
  else r.fail(`${p}kind`, `Must be one of ${ROLE_KINDS.join(", ")}`);
  const description = r.string(raw, "description", p, "");
  const instructions = r.string(raw, "instructions", p, "");
  const toolsRaw = raw.tools === undefined ? [] : raw.tools;
  const tools: string[] = [];
  if (Array.isArray(toolsRaw)) {
    toolsRaw.forEach((tool: unknown, index) => {
      if (typeof tool === "string") tools.push(tool);
      else r.fail(`${p}tools[${String(index)}]`, "Must be a string");
    });
  } else {
    r.fail(`${p}tools`, "Must be a list");
  }
  const role: Role = {
    id,
    name,
    kind,
    description,
    instructions,
    tools,
    default_model: r.nullableString(raw, "default_model", p),
  };
  for (const error of roleFieldErrors(role, p)) {
    if (!r.errors.some((e) => e.path === error.path)) r.fail(error.path, error.message);
  }
  return role;
}

function readAgent(r: Reader, raw: unknown, path: string): Agent | null {
  if (!isObject(raw)) {
    r.fail(path, "Must be an object");
    return null;
  }
  const p = `${path}.`;
  r.known(raw, p, AGENT_KEYS);
  const agent: Agent = {
    id: r.string(raw, "id", p),
    name: pyStrip(r.string(raw, "name", p)),
    role: r.string(raw, "role", p),
    reports_to: r.nullableString(raw, "reports_to", p),
    model: r.nullableString(raw, "model", p),
    checks: r.stringList(raw, "checks", p),
  };
  for (const error of agentFieldErrors(agent, p)) {
    if (!r.errors.some((e) => e.path === error.path)) r.fail(error.path, error.message);
  }
  return agent;
}

export interface ParseOrgOptions {
  /** Prefix for error paths. */
  path?: string;
  /**
   * Accept an organization with no roles or no agents. Python's `OrgGraph` requires at least one
   * of each (default here too); the builder sets this for work-in-progress documents, where the
   * live validation already reports the empty org (`no_root`).
   */
  allowEmpty?: boolean;
}

/**
 * Parses untrusted JSON into an `Org` with the same defaults and field rules as the Python model
 * (`OrgFields`: strict slugs, stripped names, no unknown keys, at least one role and one agent).
 * Graph rules are not checked here (see `validateOrg`).
 */
export function parseOrg(raw: unknown, options: ParseOrgOptions = {}): ParseResult<Org> {
  const { path = "org", allowEmpty = false } = options;
  const r = new Reader();
  if (!isObject(raw)) return { ok: false, errors: [{ path, message: "Must be an object" }] };
  const p = `${path}.`;
  r.known(raw, p, ORG_KEYS);
  const id = r.string(raw, "id", p);
  if (typeof raw.id === "string") r.check(`${p}id`, fieldRules.slug(id));
  const name = pyStrip(r.string(raw, "name", p));
  if (typeof raw.name === "string") r.check(`${p}name`, fieldRules.name(name));

  const roles: Role[] = [];
  if (Array.isArray(raw.roles)) {
    raw.roles.forEach((item: unknown, index) => {
      const role = readRole(r, item, `${p}roles[${String(index)}]`);
      if (role !== null) roles.push(role);
    });
  } else {
    r.fail(`${p}roles`, raw.roles === undefined ? "Required" : "Must be a list");
  }
  if (!allowEmpty && Array.isArray(raw.roles) && raw.roles.length === 0) {
    r.fail(`${p}roles`, "At least one role is required");
  }

  const agents: Agent[] = [];
  if (Array.isArray(raw.agents)) {
    raw.agents.forEach((item: unknown, index) => {
      const agent = readAgent(r, item, `${p}agents[${String(index)}]`);
      if (agent !== null) agents.push(agent);
    });
  } else {
    r.fail(`${p}agents`, raw.agents === undefined ? "Required" : "Must be a list");
  }
  if (!allowEmpty && Array.isArray(raw.agents) && raw.agents.length === 0) {
    r.fail(`${p}agents`, "At least one agent is required");
  }

  if (r.errors.length > 0) return { ok: false, errors: r.errors };
  return { ok: true, value: { id, name, roles, agents } };
}

/** A role with every optional field at its default (the same defaults as the Python model). */
export function makeRole(fields: Pick<Role, "id" | "name"> & Partial<Role>): Role {
  return {
    kind: "worker",
    description: "",
    instructions: "",
    tools: [],
    default_model: null,
    ...fields,
  };
}

/** An agent with every optional field at its default. */
export function makeAgent(fields: Pick<Agent, "id" | "name" | "role"> & Partial<Agent>): Agent {
  return { reports_to: null, model: null, checks: [], ...fields };
}
