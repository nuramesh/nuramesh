/**
 * Maps builder state to React Flow nodes and edges. Node and edge objects are cached and reused
 * while nothing about them changed, so React Flow (which compares by reference) only updates the
 * nodes that moved or changed: during a drag that's the dragged nodes, not all 500.
 */
import type { Edge, Node } from "@xyflow/react";

import { type Appearance, gatesOf, type OrgConfig, type Person } from "@/model/config";
import { effectiveModel, isOpenWeight } from "@/model/family";
import type { Agent, Org, RoleKind } from "@/model/org";

import type { BuilderDocument, XY } from "./document";
import type { IssueSummary } from "./issues";
import { NODE_HEIGHT, NODE_WIDTH, provisionalPosition, reportingLines } from "./layout";
import { approvalEdgeId, checkEdgeId, edgeId } from "./edge-ids";

export interface AgentNodeData extends Record<string, unknown> {
  agent: Agent;
  roleName: string | null;
  kind: RoleKind | null;
  errors: number;
  warnings: number;
  issueText: string;
  /** The model the agent runs (its own or the role's default), shown as a chip. */
  model: string | null;
  /** Dashed chip outline: public weights or self-hosted. */
  openWeight: boolean;
  appearance: Appearance;
}

export interface PersonNodeData extends Record<string, unknown> {
  person: Person;
  /** Roles this person approves for. */
  roles: number;
}

export type AgentFlowNode = Node<AgentNodeData, "agent">;
export type PersonFlowNode = Node<PersonNodeData, "person">;
export type FlowNode = AgentFlowNode | PersonFlowNode;

/** Reporting lines carry the diamond with the number of gates the report's work must pass. */
export type ReportingEdge = Edge<{ gates: number }, "reporting">;
export type VerificationEdge = Edge<Record<string, never>, "smoothstep">;
export type ApprovalEdge = Edge<Record<string, never>, "smoothstep">;
export type FlowEdge = ReportingEdge | VerificationEdge | ApprovalEdge;

const KIND_LABEL: Record<RoleKind, string> = {
  chief_of_staff: "Chief of Staff",
  worker: "worker",
  verifier: "verifier",
};

export function kindLabel(kind: RoleKind): string {
  return KIND_LABEL[kind];
}

function nodeLabel(data: AgentNodeData): string {
  const role = data.roleName ?? `unknown role ${data.agent.role}`;
  const kind = data.kind === null ? "" : ` (${kindLabel(data.kind)})`;
  const problems =
    data.errors + data.warnings === 0
      ? ""
      : `, ${String(data.errors)} error${data.errors === 1 ? "" : "s"}, ${String(data.warnings)} warning${data.warnings === 1 ? "" : "s"}`;
  return `Agent ${data.agent.name}, ${role}${kind}${problems}`;
}

export interface FlowInput {
  doc: BuilderDocument;
  computed: Record<string, XY>;
  dragging: Record<string, XY> | null;
  selected: ReadonlySet<string>;
  issues: ReadonlyMap<string, IssueSummary>;
}

const PERSON_GAP = 100;
const PERSON_MARGIN = 140;

/** Builds nodes, reusing the previous object for any node whose inputs are unchanged. */
export class NodeCache {
  private cache = new Map<string, FlowNode>();
  private provisional = new Map<string, XY>();

  build({ doc, computed, dragging, selected, issues }: FlowInput): FlowNode[] {
    const roles = new Map(doc.org.roles.map((r) => [r.id, r]));
    const manual = doc.layout.positions;
    const next = new Map<string, FlowNode>();
    const nodes: FlowNode[] = [];
    const placed = new Map<string, XY>();
    let lowest = 0;
    let leftmost = Number.POSITIVE_INFINITY;
    const siblings = new Map<string | null, number>();
    const pending: Agent[] = [];

    for (const agent of doc.org.agents) {
      if (next.has(agent.id)) continue; // duplicate ids can't be drawn twice; validation reports them
      const position = dragging?.[agent.id] ?? manual[agent.id] ?? computed[agent.id];
      if (position === undefined) {
        pending.push(agent);
        next.set(agent.id, undefined as unknown as FlowNode); // reserve the id
        continue;
      }
      placed.set(agent.id, position);
      lowest = Math.max(lowest, position.y);
      leftmost = Math.min(leftmost, position.x);
      next.set(agent.id, this.node(agent, position, roles, selected, issues, doc));
    }

    // Agents layout hasn't placed yet: put them below their manager until the result arrives.
    const provisional = new Map<string, XY>();
    for (const agent of pending) {
      const keep = this.provisional.get(agent.id);
      let position = keep;
      if (position === undefined) {
        const index = siblings.get(agent.reports_to) ?? 0;
        siblings.set(agent.reports_to, index + 1);
        const managerPosition = agent.reports_to === null ? undefined : placed.get(agent.reports_to);
        position = provisionalPosition(managerPosition, index, lowest);
      }
      provisional.set(agent.id, position);
      placed.set(agent.id, position);
      next.set(agent.id, this.node(agent, position, roles, selected, issues, doc));
    }
    this.provisional = provisional;

    // People stand to the left of the organization until the user places them.
    const left = (Number.isFinite(leftmost) ? leftmost : 0) - NODE_WIDTH - PERSON_MARGIN;
    doc.ui.people.forEach((person, index) => {
      const position = dragging?.[person.id] ?? manual[person.id] ?? { x: left, y: index * PERSON_GAP };
      next.set(person.id, this.person(person, position, selected, doc.config));
    });

    for (const node of next.values()) nodes.push(node);
    this.cache = next;
    return nodes;
  }

  private person(person: Person, position: XY, selected: ReadonlySet<string>, config: OrgConfig): PersonFlowNode {
    const roles = config.policies.filter((p) => p.approvers.includes(person.id)).length;
    const isSelected = selected.has(person.id);
    const previous = this.cache.get(person.id);
    if (
      previous?.type === "person" &&
      previous.data.person === person &&
      previous.data.roles === roles &&
      previous.position.x === position.x &&
      previous.position.y === position.y &&
      previous.selected === isSelected
    ) {
      return previous;
    }
    return {
      id: person.id,
      type: "person",
      position,
      data: { person, roles },
      selected: isSelected,
      width: PERSON_WIDTH,
      height: PERSON_HEIGHT,
      ariaLabel: `Human approver ${person.label}`,
    };
  }

  private node(
    agent: Agent,
    position: XY,
    roles: ReadonlyMap<string, Org["roles"][number]>,
    selected: ReadonlySet<string>,
    issues: ReadonlyMap<string, IssueSummary>,
    doc: BuilderDocument,
  ): AgentFlowNode {
    const role = roles.get(agent.role);
    const summary = issues.get(agent.id);
    const errors = summary?.errors ?? 0;
    const warnings = summary?.warnings ?? 0;
    const issueText = summary?.messages.join("; ") ?? "";
    const isSelected = selected.has(agent.id);
    const model = effectiveModel(agent, role);
    const appearance = doc.ui.appearance[agent.id] ?? NO_APPEARANCE;
    const previous = this.cache.get(agent.id);
    const prev = previous?.type === "agent" ? previous.data : undefined;
    const sameData =
      prev?.agent === agent &&
      prev.roleName === (role?.name ?? null) &&
      prev.kind === (role?.kind ?? null) &&
      prev.errors === errors &&
      prev.warnings === warnings &&
      prev.issueText === issueText &&
      prev.model === model &&
      prev.appearance === appearance;
    if (
      previous !== undefined &&
      prev !== undefined &&
      sameData &&
      previous.position.x === position.x &&
      previous.position.y === position.y &&
      previous.selected === isSelected
    ) {
      return previous as AgentFlowNode;
    }
    const data: AgentNodeData =
      sameData
        ? prev
        : {
            agent,
            roleName: role?.name ?? null,
            kind: role?.kind ?? null,
            errors,
            warnings,
            issueText,
            model,
            openWeight: isOpenWeight(model),
            appearance,
          };
    return {
      id: agent.id,
      type: "agent",
      position,
      data,
      selected: isSelected,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      ariaLabel: nodeLabel(data),
    };
  }
}

const NO_APPEARANCE: Appearance = {};
export const PERSON_WIDTH = 150;
export const PERSON_HEIGHT = 56;

/** Builds every line, reusing unchanged edge objects: reporting (solid), verification and approval (dashed). */
export class EdgeCache {
  private cache = new Map<string, FlowEdge>();

  build(doc: BuilderDocument, selected: ReadonlySet<string>): FlowEdge[] {
    const { org, config } = doc;
    const next = new Map<string, FlowEdge>();
    const names = new Map(org.agents.map((a) => [a.id, a.name]));
    const roleOf = new Map(org.agents.map((a) => [a.id, a.role]));
    const roleName = new Map(org.roles.map((r) => [r.id, r.name]));
    const reuse = <E extends FlowEdge>(edge: E, same: (previous: E) => boolean): void => {
      const previous = this.cache.get(edge.id) as E | undefined;
      next.set(edge.id, previous !== undefined && same(previous) ? previous : edge);
    };

    for (const { manager, report } of reportingLines(org)) {
      const id = edgeId(report);
      const isSelected = selected.has(report);
      const label = `${names.get(report) ?? report} reports to ${names.get(manager) ?? manager}`;
      const gates = gatesOf(config, roleOf.get(report) ?? "")?.gates.length ?? 0;
      reuse<ReportingEdge>(
        { id, source: manager, target: report, type: "reporting", data: { gates }, selected: isSelected, ariaLabel: label, reconnectable: true },
        (p) => p.source === manager && p.selected === isSelected && p.ariaLabel === label && p.data?.gates === gates,
      );
    }

    // Verification lines: from the verifier's right side to the left side of the work it checks.
    for (const verifier of org.agents) {
      for (const checked of verifier.checks) {
        if (!names.has(checked) || checked === verifier.id) continue;
        const id = checkEdgeId(verifier.id, checked);
        const isSelected = selected.has(id);
        const label = `${names.get(verifier.id) ?? verifier.id} verifies ${names.get(checked) ?? checked}`;
        reuse<VerificationEdge>(
          {
            id,
            source: verifier.id,
            sourceHandle: "check-out",
            target: checked,
            targetHandle: "side-in",
            type: "smoothstep",
            selected: isSelected,
            ariaLabel: label,
            className: "ng-edge-verify",
            style: { strokeDasharray: "6 4" },
          },
          (p) => p.selected === isSelected && p.ariaLabel === label,
        );
      }
    }

    // Approval lines: from a person to each agent whose role they approve for (warm, dashed).
    for (const person of org.agents.length === 0 ? [] : doc.ui.people) {
      const approves = config.policies.filter((p) => p.approvers.includes(person.id));
      for (const policy of approves) {
        for (const agent of org.agents) {
          if (agent.role !== policy.role) continue;
          const id = approvalEdgeId(person.id, agent.id);
          const isSelected = selected.has(id);
          const label = `${person.label} approves for ${names.get(agent.id) ?? agent.id} (${roleName.get(policy.role) ?? policy.role})`;
          reuse<ApprovalEdge>(
            {
              id,
              source: person.id,
              sourceHandle: "approve-out",
              target: agent.id,
              targetHandle: "side-in",
              type: "smoothstep",
              selected: isSelected,
              ariaLabel: label,
              label: "approves",
              className: "ng-edge-approve",
              style: { strokeDasharray: "6 4" },
            },
            (p) => p.selected === isSelected && p.ariaLabel === label,
          );
        }
      }
    }

    this.cache = next;
    return [...next.values()];
  }
}
