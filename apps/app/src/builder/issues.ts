/**
 * Everything wrong with the document right now: graph rules (validate.ts, shared with Python) plus
 * malformed fields (`invalid_field`, the code Python's `OrgGraph.build` uses). Each issue knows
 * what it points at, so the panel can focus the agent or role.
 */
import { type ConfigProblem, configProblems, emptyConfig, type OrgConfig } from "@/model/config";
import { effectiveModel, modelFamily } from "@/model/family";
import { agentFieldErrors, fieldRules, type Org, roleFieldErrors } from "@/model/org";
import { type IssueCode, type OrgIssue, validateOrg } from "@/model/validate";

/** Codes the builder adds to the core's: configuration cross-references and the model-family rule. */
export type BuilderIssueCode = IssueCode | ConfigProblem["code"] | "verifier_same_family";

export type IssueTarget = { kind: "agent"; id: string } | { kind: "role"; id: string } | { kind: "org" } | null;

export interface BuilderIssue extends Omit<OrgIssue, "code"> {
  code: BuilderIssueCode;
  /** Stable key for lists. */
  key: string;
  target: IssueTarget;
}

const ROLE_SUBJECT: ReadonlySet<BuilderIssueCode> = new Set(["duplicate_role_id", "unused_role"]);

export interface IssueSummary {
  errors: number;
  warnings: number;
  /** Messages for the node's accessible description. */
  messages: string[];
}

export interface IssueReport {
  issues: BuilderIssue[];
  /** Issues per agent id, for the canvas indicators. */
  byAgent: Map<string, IssueSummary>;
  errorCount: number;
  warningCount: number;
}

/** Validates the organization. Linear time; fast enough to run on every edit (< 1 ms at 500 agents). */
export function computeIssues(org: Org, config: OrgConfig = emptyConfig()): IssueReport {
  const issues: BuilderIssue[] = [];
  const fieldIssue = (target: IssueTarget, subject: string | null, path: string, message: string): void => {
    issues.push({
      code: "invalid_field",
      severity: "error",
      message: `${path}: ${message}`,
      subject,
      target,
      key: `invalid_field|${path}|${String(issues.length)}`,
    });
  };

  const orgId = fieldRules.slug(org.id);
  if (orgId !== null) fieldIssue({ kind: "org" }, null, "organization id", orgId);
  const orgName = fieldRules.name(org.name);
  if (orgName !== null) fieldIssue({ kind: "org" }, null, "organization name", orgName);
  for (const role of org.roles) {
    for (const e of roleFieldErrors(role, `role '${role.id}' `)) fieldIssue({ kind: "role", id: role.id }, role.id, e.path, e.message);
  }
  for (const agent of org.agents) {
    for (const e of agentFieldErrors(agent, `agent '${agent.id}' `)) fieldIssue({ kind: "agent", id: agent.id }, agent.id, e.path, e.message);
  }

  validateOrg(org.roles, org.agents).forEach((issue, index) => {
    const target: IssueTarget =
      issue.subject === null ? null : ROLE_SUBJECT.has(issue.code) ? { kind: "role", id: issue.subject } : { kind: "agent", id: issue.subject };
    issues.push({ ...issue, target, key: `${issue.code}|${String(issue.subject)}|${String(index)}` });
  });

  // A verifier must be a different model family from the work it checks (SPEC-app-v1 3.1).
  const roleById = new Map(org.roles.map((r) => [r.id, r]));
  const agentById = new Map(org.agents.map((a) => [a.id, a]));
  for (const verifier of org.agents) {
    const verifierFamily = modelFamily(effectiveModel(verifier, roleById.get(verifier.role)));
    if (verifierFamily === null) continue;
    for (const checkedId of verifier.checks) {
      const checked = agentById.get(checkedId);
      if (checked === undefined || checked.id === verifier.id) continue;
      if (modelFamily(effectiveModel(checked, roleById.get(checked.role))) === verifierFamily) {
        issues.push({
          code: "verifier_same_family",
          severity: "error",
          message: `Verifier '${verifier.id}' runs a ${verifierFamily} model, the same family as '${checked.id}', which it checks: pick a different family`,
          subject: verifier.id,
          target: { kind: "agent", id: verifier.id },
          key: `verifier_same_family|${verifier.id}|${checked.id}`,
        });
      }
    }
  }

  const agentSubjects = new Set(org.agents.map((a) => a.id));
  configProblems(org, config).forEach((problem, index) => {
    const subject = problem.subject;
    const target: IssueTarget =
      subject === null ? null : agentSubjects.has(subject) && problem.code !== "unknown_role" ? { kind: "agent", id: subject } : roleById.has(subject) ? { kind: "role", id: subject } : null;
    // The builder can't see the suites defined in Git, so an eval requirement is a warning: the file may be fine.
    const severity = problem.code === "eval_suite_undefined" ? "warning" : "error";
    issues.push({ code: problem.code, severity, message: problem.message, subject, target, key: `${problem.code}|${String(subject)}|${String(index)}` });
  });

  // Errors first, field errors before graph errors within each severity (stable sort).
  issues.sort((a, b) => Number(a.severity !== "error") - Number(b.severity !== "error"));

  const byAgent = new Map<string, IssueSummary>();
  let errorCount = 0;
  for (const issue of issues) {
    if (issue.severity === "error") errorCount += 1;
    if (issue.target?.kind !== "agent") continue;
    let summary = byAgent.get(issue.target.id);
    if (summary === undefined) {
      summary = { errors: 0, warnings: 0, messages: [] };
      byAgent.set(issue.target.id, summary);
    }
    if (issue.severity === "error") summary.errors += 1;
    else summary.warnings += 1;
    summary.messages.push(issue.message);
  }
  return { issues, byAgent, errorCount, warningCount: issues.length - errorCount };
}
