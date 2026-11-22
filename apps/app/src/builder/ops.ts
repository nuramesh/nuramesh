/**
 * Pure edits on a builder document. Each returns a new document and shares everything it didn't
 * change (structural sharing), so history entries are cheap and React can compare by reference.
 */
import type { Agent, Org, Role } from "@/model/org";

import { removeAgentRefs, removeRoleRefs, renameAgentRefs, renameRoleRefs } from "./config-ops";
import type { BuilderDocument, XY } from "./document";
import { uniqueId } from "./ids";

export { uniqueId };

function withOrg(doc: BuilderDocument, org: Org): BuilderDocument {
  return { ...doc, org };
}

function mapAgents(doc: BuilderDocument, fn: (agent: Agent) => Agent): BuilderDocument {
  const agents = doc.org.agents.map(fn);
  return agents.some((agent, i) => agent !== doc.org.agents[i]) ? withOrg(doc, { ...doc.org, agents }) : doc;
}

function mapRoles(doc: BuilderDocument, fn: (role: Role) => Role): BuilderDocument {
  const roles = doc.org.roles.map(fn);
  return roles.some((role, i) => role !== doc.org.roles[i]) ? withOrg(doc, { ...doc.org, roles }) : doc;
}

function shallowEqual<T extends object>(a: T, patch: Partial<T>): boolean {
  return (Object.keys(patch) as (keyof T)[]).every((key) => {
    const left = a[key];
    const right = patch[key];
    if (Array.isArray(left) && Array.isArray(right)) {
      return left.length === right.length && left.every((v, i) => v === right[i]);
    }
    return left === right;
  });
}

// ---- Organization -----------------------------------------------------------------------------

export function updateOrg(doc: BuilderDocument, patch: Partial<Pick<Org, "id" | "name">>): BuilderDocument {
  if (shallowEqual(doc.org, patch)) return doc;
  return withOrg(doc, { ...doc.org, ...patch });
}

// ---- Agents -----------------------------------------------------------------------------------

export function agentIds(doc: BuilderDocument): Set<string> {
  return new Set(doc.org.agents.map((a) => a.id));
}

export interface NewAgent {
  role: Role;
  reportsTo?: string | null;
  position?: XY;
}

/**
 * Adds an agent holding `role` (adding the role too if the org doesn't have it yet).
 * A `position` pins it where the user dropped it; otherwise automatic layout places it.
 */
export function addAgent(doc: BuilderDocument, input: NewAgent): { doc: BuilderDocument; id: string } {
  let next = ensureRole(doc, input.role);
  const taken = agentIds(next);
  const id = uniqueId(input.role.id, taken);
  const sameRole = next.org.agents.filter((a) => a.role === input.role.id).length;
  const name = sameRole === 0 ? input.role.name : `${input.role.name} ${String(sameRole + 1)}`;
  const reportsTo = input.reportsTo !== undefined && input.reportsTo !== null && taken.has(input.reportsTo)
    ? input.reportsTo
    : null;
  const agent: Agent = { id, name, role: input.role.id, reports_to: reportsTo, model: null, checks: [] };
  next = withOrg(next, { ...next.org, agents: [...next.org.agents, agent] });
  if (input.position) next = setPositions(next, { [id]: input.position });
  return { doc: next, id };
}

export function updateAgent(
  doc: BuilderDocument,
  id: string,
  patch: Partial<Pick<Agent, "name" | "role" | "model">>,
): BuilderDocument {
  return mapAgents(doc, (agent) => (agent.id === id && !shallowEqual(agent, patch) ? { ...agent, ...patch } : agent));
}

/** True if making `managerId` the manager of `reportId` would close a reporting loop. */
export function wouldCreateCycle(doc: BuilderDocument, managerId: string, reportId: string): boolean {
  if (managerId === reportId) return true;
  const managerOf = new Map(doc.org.agents.map((a) => [a.id, a.reports_to]));
  const seen = new Set<string>();
  let current: string | null | undefined = managerId;
  while (current !== null && current !== undefined && !seen.has(current)) {
    if (current === reportId) return true;
    seen.add(current);
    current = managerOf.get(current);
  }
  return false;
}

/** Sets an agent's manager (`null` makes it a top agent). An agent has at most one manager. */
export function setManager(doc: BuilderDocument, id: string, managerId: string | null): BuilderDocument {
  return mapAgents(doc, (agent) =>
    agent.id === id && agent.reports_to !== managerId ? { ...agent, reports_to: managerId } : agent,
  );
}

/** Renames an agent and every reference to it (reports, layout). Caller checks the new id. */
export function renameAgent(doc: BuilderDocument, from: string, to: string): BuilderDocument {
  if (from === to) return doc;
  const next = mapAgents(doc, (agent) => {
    if (agent.id === from) return { ...agent, id: to, reports_to: agent.reports_to === from ? to : agent.reports_to };
    return agent.reports_to === from ? { ...agent, reports_to: to } : agent;
  });
  const renamed = renameAgentRefs(next, from, to);
  const positions = doc.layout.positions;
  if (!(from in positions)) return renamed;
  const { [from]: moved, ...rest } = positions;
  return { ...renamed, layout: { positions: moved ? { ...rest, [to]: moved } : rest } };
}

/**
 * Removes agents. Their reports move up to the nearest manager that stays, so the rest of the
 * tree stays connected; their saved positions are dropped.
 */
export function removeAgents(doc: BuilderDocument, ids: ReadonlySet<string>): BuilderDocument {
  if (ids.size === 0) return doc;
  const managerOf = new Map(doc.org.agents.map((a) => [a.id, a.reports_to]));
  const survivingManager = (start: string | null): string | null => {
    const seen = new Set<string>();
    let current = start;
    while (current !== null && ids.has(current) && !seen.has(current)) {
      seen.add(current);
      current = managerOf.get(current) ?? null;
    }
    return current !== null && ids.has(current) ? null : current;
  };
  const agents = doc.org.agents
    .filter((agent) => !ids.has(agent.id))
    .map((agent) =>
      agent.reports_to !== null && ids.has(agent.reports_to)
        ? { ...agent, reports_to: survivingManager(agent.reports_to) }
        : agent,
    );
  if (agents.length === doc.org.agents.length) return doc;
  const positions = Object.fromEntries(
    Object.entries(doc.layout.positions).filter(([id]) => !ids.has(id)),
  );
  return removeAgentRefs({ ...doc, org: { ...doc.org, agents }, layout: { positions } }, ids);
}

/** Removes the reporting line above each listed agent (they become top agents). */
export function removeReportingLines(doc: BuilderDocument, reportIds: ReadonlySet<string>): BuilderDocument {
  return mapAgents(doc, (agent) =>
    reportIds.has(agent.id) && agent.reports_to !== null ? { ...agent, reports_to: null } : agent,
  );
}

// ---- Roles ------------------------------------------------------------------------------------

export function roleIds(doc: BuilderDocument): Set<string> {
  return new Set(doc.org.roles.map((r) => r.id));
}

/** Adds the role unless one with its id exists. */
export function ensureRole(doc: BuilderDocument, role: Role): BuilderDocument {
  if (doc.org.roles.some((r) => r.id === role.id)) return doc;
  return withOrg(doc, { ...doc.org, roles: [...doc.org.roles, role] });
}

/** Adds a new custom worker role with a free id; returns the id. */
export function addRole(doc: BuilderDocument, name = "New role"): { doc: BuilderDocument; id: string } {
  const id = uniqueId(name, roleIds(doc));
  const role: Role = { id, name, kind: "worker", description: "", instructions: "", tools: [], default_model: null };
  return { doc: withOrg(doc, { ...doc.org, roles: [...doc.org.roles, role] }), id };
}

export function updateRole(
  doc: BuilderDocument,
  id: string,
  patch: Partial<Omit<Role, "id">>,
): BuilderDocument {
  return mapRoles(doc, (role) => (role.id === id && !shallowEqual(role, patch) ? { ...role, ...patch } : role));
}

/** Renames a role and every agent's reference to it. Caller checks the new id. */
export function renameRole(doc: BuilderDocument, from: string, to: string): BuilderDocument {
  if (from === to) return doc;
  const next = mapRoles(doc, (role) => (role.id === from ? { ...role, id: to } : role));
  return renameRoleRefs(mapAgents(next, (agent) => (agent.role === from ? { ...agent, role: to } : agent)), from, to);
}

/** Agents holding a role. */
export function roleUsage(doc: BuilderDocument, roleId: string): number {
  return doc.org.agents.filter((a) => a.role === roleId).length;
}

/** Deletes a role nobody holds (a role in use is left alone; the UI says why). */
export function removeRole(doc: BuilderDocument, id: string): BuilderDocument {
  if (roleUsage(doc, id) > 0) return doc;
  const roles = doc.org.roles.filter((r) => r.id !== id);
  return roles.length === doc.org.roles.length ? doc : removeRoleRefs(withOrg(doc, { ...doc.org, roles }), id);
}

// ---- Layout -----------------------------------------------------------------------------------

/** Pins agents at user-chosen positions (dragged or dropped). */
export function setPositions(doc: BuilderDocument, positions: Record<string, XY>): BuilderDocument {
  const entries = Object.entries(positions);
  const current = doc.layout.positions;
  if (entries.every(([id, p]) => current[id]?.x === p.x && current[id].y === p.y)) return doc;
  return { ...doc, layout: { positions: { ...current, ...positions } } };
}

/** Forgets every user-chosen position, so automatic layout places every agent. */
export function clearPositions(doc: BuilderDocument): BuilderDocument {
  return Object.keys(doc.layout.positions).length === 0 ? doc : { ...doc, layout: { positions: {} } };
}

