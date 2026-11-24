/**
 * Graph rules for an organization: the same rules, codes, severities and subjects as
 * `validate_org` in `src/nuramesh/core/org.py`. The shared fixtures
 * run against both, so the canvas and the backend can't disagree about what is valid.
 */
import type { Agent, Role } from "./org";

export type Severity = "error" | "warning";

/** Stable issue codes (shared with the Python core). */
export type IssueCode =
  | "duplicate_role_id"
  | "duplicate_agent_id"
  | "unknown_role"
  | "unknown_manager"
  | "self_report"
  | "no_root"
  | "multiple_roots"
  | "root_not_chief_of_staff"
  | "chief_of_staff_not_root"
  | "unknown_checked"
  | "self_check"
  | "duplicate_check"
  | "checker_not_verifier"
  | "verifier_in_chain"
  | "cycle"
  | "unreachable"
  | "unused_role"
  | "invalid_field";

/** One validation problem. `subject` is the agent or role id it is about, if any. */
export interface OrgIssue {
  code: IssueCode;
  severity: Severity;
  message: string;
  subject: string | null;
}

type RoleLike = Pick<Role, "id" | "kind">;
type AgentLike = Pick<Agent, "id" | "role" | "reports_to"> & { checks?: readonly string[] };

function error(code: IssueCode, message: string, subject: string | null = null): OrgIssue {
  return { code, severity: "error", message, subject };
}

/** Every problem in an organization, errors before warnings. Linear time in roles + agents. */
export function validateOrg(
  roles: readonly RoleLike[],
  agents: readonly AgentLike[],
): OrgIssue[] {
  const issues: OrgIssue[] = [];

  // Only the first role or agent with an id takes part in later rules.
  const roleById = new Map<string, RoleLike>();
  for (const role of roles) {
    if (roleById.has(role.id)) {
      issues.push(error("duplicate_role_id", `Role id '${role.id}' is used twice`, role.id));
    } else {
      roleById.set(role.id, role);
    }
  }

  const agentById = new Map<string, AgentLike>();
  for (const agent of agents) {
    if (agentById.has(agent.id)) {
      issues.push(error("duplicate_agent_id", `Agent id '${agent.id}' is used twice`, agent.id));
    } else {
      agentById.set(agent.id, agent);
    }
  }

  const roots: AgentLike[] = [];
  const manager = new Map<string, string>();
  for (const agent of agentById.values()) {
    const role = roleById.get(agent.role);
    if (role === undefined) {
      issues.push(
        error(
          "unknown_role",
          `Agent '${agent.id}' has role '${agent.role}', which doesn't exist`,
          agent.id,
        ),
      );
    }
    if (agent.reports_to === null) {
      roots.push(agent);
    } else if (agent.reports_to === agent.id) {
      issues.push(error("self_report", `Agent '${agent.id}' reports to itself`, agent.id));
    } else if (!agentById.has(agent.reports_to)) {
      issues.push(
        error(
          "unknown_manager",
          `Agent '${agent.id}' reports to '${agent.reports_to}', which doesn't exist`,
          agent.id,
        ),
      );
    } else {
      manager.set(agent.id, agent.reports_to);
    }
    if (role?.kind === "chief_of_staff" && agent.reports_to !== null) {
      issues.push(
        error(
          "chief_of_staff_not_root",
          `Agent '${agent.id}' has the Chief of Staff role but reports to someone`,
          agent.id,
        ),
      );
    }
  }

  if (roots.length === 0) {
    issues.push(error("no_root", "No agent is at the top (one must report to no one)"));
  } else if (roots.length > 1) {
    const names = roots.map((a) => `'${a.id}'`).join(", ");
    for (const root of roots) {
      issues.push(
        error("multiple_roots", `Only one agent may be at the top; found ${names}`, root.id),
      );
    }
  }
  for (const root of roots) {
    const role = roleById.get(root.role);
    if (role !== undefined && role.kind !== "chief_of_staff") {
      issues.push(
        error(
          "root_not_chief_of_staff",
          `The top agent '${root.id}' must have a Chief of Staff role`,
          root.id,
        ),
      );
    }
  }

  addCycleIssues(issues, manager, agentById.keys());
  addCheckIssues(issues, agentById, roleById, manager);

  const usedRoles = new Set<string>();
  for (const agent of agentById.values()) usedRoles.add(agent.role);
  for (const roleId of roleById.keys()) {
    if (!usedRoles.has(roleId)) {
      issues.push({
        code: "unused_role",
        severity: "warning",
        message: `Role '${roleId}' has no agents`,
        subject: roleId,
      });
    }
  }

  // Stable sort: errors first, otherwise insertion order (same as the Python sort key).
  return issues.sort((a, b) => Number(a.severity !== "error") - Number(b.severity !== "error"));
}

/** The agent and its managers upwards; stops on a loop (loops are reported separately). */
function chain(id: string, manager: ReadonlyMap<string, string>): Set<string> {
  const seen = new Set<string>();
  let node: string | undefined = id;
  while (node !== undefined && !seen.has(node)) {
    seen.add(node);
    node = manager.get(node);
  }
  return seen;
}

/**
 * Problems on the verification line (`Agent.checks`), as `_check_issues` in org.py: a checker must
 * have a verifier role and may not be in the reporting chain of an agent it checks (the author,
 * anyone above the author, or anyone who answers to the author). Peers and other branches are fine.
 */
function addCheckIssues(
  issues: OrgIssue[],
  agentById: ReadonlyMap<string, AgentLike>,
  roleById: ReadonlyMap<string, RoleLike>,
  manager: ReadonlyMap<string, string>,
): void {
  for (const agent of agentById.values()) {
    const checks = agent.checks ?? [];
    if (checks.length === 0) continue;
    const role = roleById.get(agent.role);
    if (role !== undefined && role.kind !== "verifier") {
      issues.push(
        error(
          "checker_not_verifier",
          `Agent '${agent.id}' checks others but its role '${role.id}' isn't a verifier`,
          agent.id,
        ),
      );
    }
    const ownChain = chain(agent.id, manager);
    const listed = new Set<string>();
    for (const checked of checks) {
      if (listed.has(checked)) {
        issues.push(error("duplicate_check", `Agent '${agent.id}' lists '${checked}' twice in checks`, agent.id));
        continue;
      }
      listed.add(checked);
      if (checked === agent.id) {
        issues.push(error("self_check", `Agent '${agent.id}' checks itself`, agent.id));
      } else if (!agentById.has(checked)) {
        issues.push(
          error("unknown_checked", `Agent '${agent.id}' checks '${checked}', which doesn't exist`, agent.id),
        );
      } else if (chain(checked, manager).has(agent.id) || ownChain.has(checked)) {
        issues.push(
          error(
            "verifier_in_chain",
            `Verifier '${agent.id}' is in the reporting chain of '${checked}', which it checks: a checker must be independent of the work`,
            agent.id,
          ),
        );
      }
    }
  }
}

/** Appends agents on a reporting cycle, and agents whose chain leads into one (never reach the top). */
function addCycleIssues(
  issues: OrgIssue[],
  manager: ReadonlyMap<string, string>,
  agentIds: Iterable<string>,
): void {
  const ON_WALK = 1;
  const DONE = 2;
  const state = new Map<string, number>();
  const bad = new Set<string>();
  for (const start of agentIds) {
    if (state.has(start)) continue;
    const walk: string[] = [];
    const position = new Map<string, number>(); // index in `walk`, so slicing stays linear
    let node: string | undefined = start;
    while (node !== undefined && !state.has(node)) {
      state.set(node, ON_WALK);
      position.set(node, walk.length);
      walk.push(node);
      node = manager.get(node);
    }
    let leading: string[] = [];
    if (node !== undefined && state.get(node) === ON_WALK) {
      const at = position.get(node) ?? 0;
      const cycle = walk.slice(at);
      const members = [...cycle, cycle[0]].join(" → ");
      for (const member of cycle) {
        issues.push(error("cycle", `Reporting lines form a loop: ${members}`, member));
        bad.add(member);
      }
      leading = walk.slice(0, at);
    } else if (node !== undefined && bad.has(node)) {
      leading = walk;
    }
    for (const agentId of leading) {
      bad.add(agentId);
      issues.push(
        error(
          "unreachable",
          `Agent '${agentId}' reports into a loop and never reaches the top`,
          agentId,
        ),
      );
    }
    for (const agentId of walk) state.set(agentId, DONE);
  }
}

/** True when no issue is an error (warnings are allowed). */
export function isValid(issues: readonly OrgIssue[]): boolean {
  return issues.every((issue) => issue.severity !== "error");
}
