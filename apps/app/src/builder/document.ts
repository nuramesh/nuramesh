/**
 * The builder document: an organization plus where its agents sit on the canvas.
 *
 * `org` is exactly the Python `OrgGraph` JSON (snake_case keys), so it can be handed to the core
 * unchanged; `config` holds the rest of the core's configuration (permissions, budgets, presets,
 * eval requirements). `ui` is builder-only (people on the canvas, node appearance). `layout.positions`
 * holds only positions the user chose (dragged or dropped); every other agent is placed by
 * automatic layout, which is derived and never stored. A document saved before `config` and `ui`
 * existed reads as one with both empty.
 */
import { type BuilderUi, emptyConfig, emptyUi, type OrgConfig, parseConfig, parseUi } from "@/model/config";
import { type FieldError, type Org, parseOrg } from "@/model/org";

export const SCHEMA_VERSION = 1;

export interface XY {
  x: number;
  y: number;
}

export interface BuilderDocument {
  schema_version: typeof SCHEMA_VERSION;
  org: Org;
  config: OrgConfig;
  ui: BuilderUi;
  layout: { positions: Record<string, XY> };
}

/** A new, empty organization (no roles or agents yet; the palette offers predefined roles). */
export function blankDocument(): BuilderDocument {
  return {
    schema_version: SCHEMA_VERSION,
    org: { id: "my-org", name: "My organization", roles: [], agents: [] },
    config: emptyConfig(),
    ui: emptyUi(),
    layout: { positions: {} },
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Ids used more than once (the canvas needs unique ids, so imports with duplicates are refused). */
function duplicates(ids: readonly string[]): string[] {
  const seen = new Set<string>();
  const dupes = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) dupes.add(id);
    seen.add(id);
  }
  return [...dupes];
}

/**
 * Parses untrusted JSON (an imported file, saved template or draft) into a document.
 * Accepts a full document or a bare organization (as the Python core and config files use).
 */
export function parseDocument(raw: unknown): { ok: true; value: BuilderDocument } | {
  ok: false;
  errors: FieldError[];
} {
  if (!isObject(raw)) return { ok: false, errors: [{ path: "(file)", message: "Must be a JSON object" }] };

  const isBareOrg = !("schema_version" in raw) && "roles" in raw && "agents" in raw;
  const errors: FieldError[] = [];
  if (!isBareOrg) {
    for (const key of Object.keys(raw)) {
      if (!["schema_version", "org", "config", "ui", "layout"].includes(key)) {
        errors.push({ path: key, message: "Unknown field" });
      }
    }
    if (raw.schema_version !== SCHEMA_VERSION) {
      errors.push({
        path: "schema_version",
        message:
          raw.schema_version === undefined
            ? `Missing schema_version; expected ${String(SCHEMA_VERSION)}`
            : `Unsupported schema version ${JSON.stringify(raw.schema_version)}; expected ${String(SCHEMA_VERSION)}`,
      });
    }
  }

  // A work-in-progress document may be empty; live validation reports it (no_root).
  const org = parseOrg(isBareOrg ? raw : raw.org, { path: "org", allowEmpty: true });
  if (!org.ok) errors.push(...org.errors);

  const config = parseConfig(isBareOrg ? undefined : raw.config);
  if (!config.ok) errors.push(...config.errors);
  const ui = parseUi(isBareOrg ? undefined : raw.ui);
  if (!ui.ok) errors.push(...ui.errors);

  // No prototype: an imported id like "__proto__" must be an ordinary key (and then fail the
  // "no agent has this id" check below), not change the object's prototype.
  const positions: Record<string, XY> = Object.create(null) as Record<string, XY>;
  const layout = isBareOrg || raw.layout === undefined ? { positions: {} } : raw.layout;
  if (!isObject(layout) || !isObject(layout.positions)) {
    errors.push({ path: "layout.positions", message: "Must be an object of agent id → {x, y}" });
  } else {
    for (const key of Object.keys(layout)) {
      if (key !== "positions") errors.push({ path: `layout.${key}`, message: "Unknown field" });
    }
    for (const [id, value] of Object.entries(layout.positions)) {
      if (isObject(value) && isFiniteNumber(value.x) && isFiniteNumber(value.y)) {
        positions[id] = { x: value.x, y: value.y };
      } else {
        errors.push({ path: `layout.positions.${id}`, message: "Must be {x, y} with finite numbers" });
      }
    }
  }

  if (org.ok) {
    for (const id of duplicates(org.value.roles.map((r) => r.id))) {
      errors.push({ path: "org.roles", message: `Role id '${id}' is used more than once` });
    }
    const agentIds = org.value.agents.map((a) => a.id);
    for (const id of duplicates(agentIds)) {
      errors.push({ path: "org.agents", message: `Agent id '${id}' is used more than once` });
    }
    // Positions belong to agents and to the people drawn on the canvas.
    const known = new Set([...agentIds, ...(ui.ok ? ui.value.people.map((p) => p.id) : [])]);
    for (const id of Object.keys(positions)) {
      if (!known.has(id)) {
        errors.push({ path: `layout.positions.${id}`, message: "No agent or person has this id" });
      }
    }
  }

  if (errors.length > 0 || !org.ok || !config.ok || !ui.ok) return { ok: false, errors };
  return {
    ok: true,
    value: { schema_version: SCHEMA_VERSION, org: org.value, config: config.value, ui: ui.value, layout: { positions } },
  };
}

/** Pretty JSON for export; keys in model order so a Git diff of an export stays readable. */
export function serializeDocument(doc: BuilderDocument): string {
  return `${JSON.stringify(doc, null, 2)}\n`;
}

/** Human-readable list of parse errors, one per line. */
export function formatErrors(errors: readonly FieldError[]): string[] {
  return errors.map((e) => `${e.path}: ${e.message}`);
}

const MAX_IMPORT_BYTES = 10 * 1024 * 1024;

/** Reads an imported file into a document, or the reasons it can't be one. */
export async function readDocumentFile(
  file: Blob,
): Promise<{ ok: true; value: BuilderDocument } | { ok: false; errors: string[] }> {
  if (file.size > MAX_IMPORT_BYTES) {
    return { ok: false as const, errors: [`The file is ${String(Math.round(file.size / 1024 / 1024))} MB; the limit is 10 MB`] };
  }
  let raw: unknown;
  try {
    raw = JSON.parse(await file.text());
  } catch (error) {
    return { ok: false as const, errors: [`Not valid JSON: ${error instanceof Error ? error.message : String(error)}`] };
  }
  const parsed = parseDocument(raw);
  return parsed.ok ? parsed : { ok: false as const, errors: formatErrors(parsed.errors) };
}

