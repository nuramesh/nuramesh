/**
 * Copy and paste of selected agents, including the reporting lines between them.
 * Pasted agents get fresh ids; lines inside the selection are remapped to the new ids, and a
 * pasted agent whose manager wasn't copied keeps reporting to that manager if it still exists.
 */
import { type Agent, NAME_MAX, type Role } from "@/model/org";

import type { BuilderDocument, XY } from "./document";
import { agentIds, ensureRole, uniqueId } from "./ops";

export interface Fragment {
  roles: Role[];
  agents: Agent[];
  /** User-chosen positions of the copied agents (others are placed by layout after paste). */
  positions: Record<string, XY>;
}

export const PASTE_OFFSET = 40;

/** The selected agents (in document order), the roles they hold and their pinned positions. */
export function copyFragment(doc: BuilderDocument, ids: ReadonlySet<string>): Fragment | null {
  const agents = doc.org.agents.filter((a) => ids.has(a.id));
  if (agents.length === 0) return null;
  const held = new Set(agents.map((a) => a.role));
  const positions: Record<string, XY> = {};
  for (const agent of agents) {
    const position = doc.layout.positions[agent.id];
    if (position) positions[agent.id] = position;
  }
  return { roles: doc.org.roles.filter((r) => held.has(r.id)), agents, positions };
}

function copyName(name: string): string {
  const suffix = " (copy)";
  const base = Array.from(name);
  const room = NAME_MAX - suffix.length;
  return `${base.length > room ? base.slice(0, room).join("") : name}${suffix}`;
}

/** Pastes a fragment; returns the new document and the new agents' ids (in fragment order). */
export function pasteFragment(
  doc: BuilderDocument,
  fragment: Fragment,
  offset: number = PASTE_OFFSET,
): { doc: BuilderDocument; ids: string[] } {
  let next = doc;
  for (const role of fragment.roles) next = ensureRole(next, role);

  const taken = agentIds(next);
  const remap = new Map<string, string>();
  for (const agent of fragment.agents) {
    const id = uniqueId(agent.id, taken);
    taken.add(id);
    remap.set(agent.id, id);
  }
  const existing = agentIds(next);
  const pasted: Agent[] = fragment.agents.map((agent) => {
    const managerInside = agent.reports_to === null ? undefined : remap.get(agent.reports_to);
    const reportsTo =
      managerInside ?? (agent.reports_to !== null && existing.has(agent.reports_to) ? agent.reports_to : null);
    return { ...agent, id: remap.get(agent.id) ?? agent.id, name: copyName(agent.name), reports_to: reportsTo };
  });

  const positions = { ...next.layout.positions };
  for (const [id, p] of Object.entries(fragment.positions)) {
    const newId = remap.get(id);
    if (newId !== undefined) positions[newId] = { x: p.x + offset, y: p.y + offset };
  }

  return {
    doc: { ...next, org: { ...next.org, agents: [...next.org.agents, ...pasted] }, layout: { positions } },
    ids: pasted.map((a) => a.id),
  };
}
