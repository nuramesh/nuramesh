/**
 * Predefined roles (PLAN B.1) and the built-in team templates (PLAN B.3) as typed data.
 * Templates carry roles, hierarchy, models, verification lines, permissions and budgets. They
 * carry no gate preset: a gate with no settings can never be met by a task, so a template that
 * shipped one could not run (ND194 Q2 (i) = A, issue #1407). A team's gates come from its team
 * script, or are added in the builder with their settings.
 */
import { emptyConfig, emptyUi } from "@/model/config";
import { addCheck, addRule, setAgentBudget } from "./config-ops";
import type { Agent, Role } from "@/model/org";

import { type BuilderDocument, SCHEMA_VERSION } from "./document";

function role(id: string, name: string, kind: Role["kind"], description: string, instructions: string, tools: string[] = []): Role {
  return { id, name, kind, description, instructions, tools, default_model: null };
}

function agent(id: string, name: string, roleId: string, reportsTo: string | null): Agent {
  return { id, name, role: roleId, reports_to: reportsTo, model: null, checks: [] };
}

/** The roles every organization can start from (PLAN B.1). */
export const PREDEFINED_ROLES: readonly Role[] = [
  role(
    "chief-of-staff",
    "Chief of Staff",
    "chief_of_staff",
    "Turns a goal into a task tree and delegates down the graph.",
    "Break the goal into tasks, each with a definition of done. Delegate only to eligible agents below you, within policy and budget. Report what was done and verified.",
  ),
  role(
    "developer",
    "Developer",
    "worker",
    "Writes and changes code to meet a task's definition of done.",
    "Make the smallest change that meets the definition of done. Add or update tests with every behaviour change.",
    ["git", "editor", "shell"],
  ),
  role(
    "tester",
    "Tester",
    "worker",
    "Writes and runs tests for a change.",
    "Write tests that would fail without the change. Run the full suite and report results with evidence.",
    ["shell"],
  ),
  role(
    "reviewer",
    "Reviewer",
    "verifier",
    "Independently checks work against the definition of done and the canon.",
    "You see the task, the definition of done, the output and the canon, never the worker's reasoning. Cite evidence for every item you check.",
  ),
  role(
    "support-agent",
    "Support agent",
    "worker",
    "Answers customer questions from the canon.",
    "Answer only from the canon. Escalate to a human when the answer isn't there.",
  ),
  role(
    "sales-agent",
    "Sales agent",
    "worker",
    "Drafts outreach and qualifies leads.",
    "Draft messages for human approval; never send external messages on your own.",
  ),
  role(
    "devops-engineer",
    "DevOps engineer",
    "worker",
    "Runs builds, deployments and infrastructure changes.",
    "Every change goes through a pull request and a plan; nothing reaches production without approval.",
    ["shell", "git"],
  ),
  role(
    "qa-analyst",
    "QA analyst",
    "verifier",
    "Checks releases against acceptance criteria.",
    "Check each acceptance criterion and record evidence; a criterion without evidence fails.",
  ),
];

/**
 * Predefined roles worth offering next to the organization's own: not one the org already has by id,
 * nor one it has under another id with the same name (the dev-team template calls its Chief of
 * Staff `cos`, as the package does, so the predefined `chief-of-staff` would be a duplicate).
 */
export function extraRoles(roles: readonly Role[]): Role[] {
  const ids = new Set(roles.map((r) => r.id));
  const names = new Set(roles.map((r) => r.name.toLowerCase()));
  return PREDEFINED_ROLES.filter((r) => !ids.has(r.id) && !names.has(r.name.toLowerCase()));
}

function predefined(id: string): Role {
  const found = PREDEFINED_ROLES.find((r) => r.id === id);
  if (found === undefined) throw new Error(`No predefined role '${id}'`);
  return found;
}

export interface Template {
  id: string;
  name: string;
  description: string;
  document: BuilderDocument;
}

/** Built-in team templates (PLAN B.3). */
const BASE_TEMPLATES: readonly Template[] = [
  {
    id: "dev-team",
    name: "Dev team",
    description: "A Chief of Staff leading a developer, a tester and a DevOps engineer, with an independent reviewer.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "dev-team",
        name: "Dev team",
        roles: [
          { ...predefined("chief-of-staff"), id: "cos" },
          predefined("developer"),
          predefined("tester"),
          predefined("reviewer"),
          { ...predefined("devops-engineer"), id: "devops" },
        ],
        agents: [
          agent("chief", "Chief of Staff", "cos", null),
          agent("developer", "Developer", "developer", "chief"),
          agent("tester", "Tester", "tester", "chief"),
          agent("reviewer", "Reviewer", "reviewer", "chief"),
          agent("devops", "DevOps engineer", "devops", "chief"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "code-review",
    name: "Code review",
    description: "A Chief of Staff routing pull requests to independent code and security reviewers.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "code-review",
        name: "Code review",
        roles: [
          predefined("chief-of-staff"),
          role(
            "change-summarizer",
            "Change summarizer",
            "worker",
            "Summarizes a pull request: what changed, why, and the risky parts.",
            "Summarize the diff and list the files and behaviours a reviewer must check.",
            ["git"],
          ),
          role(
            "code-reviewer",
            "Code reviewer",
            "verifier",
            "Reviews a change for correctness, tests and readability.",
            "Check the diff against the definition of done and the canon; cite file and line for every finding.",
            ["git"],
          ),
          role(
            "security-reviewer",
            "Security reviewer",
            "verifier",
            "Reviews a change for security problems.",
            "Check for injection, secrets, unsafe defaults and permission changes; cite evidence for every finding.",
            ["git"],
          ),
        ],
        agents: [
          agent("chief-of-staff", "Chief of Staff", "chief-of-staff", null),
          agent("change-summarizer", "Change summarizer", "change-summarizer", "chief-of-staff"),
          agent("code-reviewer", "Code reviewer", "code-reviewer", "chief-of-staff"),
          agent("security-reviewer", "Security reviewer", "security-reviewer", "chief-of-staff"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "scrum",
    name: "Scrum team",
    description: "A product owner, a scrum master, two developers and a tester run a sprint; an independent reviewer checks every story.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "scrum",
        name: "Scrum team",
        roles: [
          role(
            "product-owner",
            "Product owner",
            "chief_of_staff",
            "Turns the product goal into a sprint of stories and delegates them.",
            "Break the goal into stories, each with a definition of done. Delegate only to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          role(
            "scrum-master",
            "Scrum master",
            "worker",
            "Plans the sprint and runs the sprint review.",
            "Write the sprint goal and the stories, and at the end say whether the goal was met. Keep it short and factual.",
          ),
          predefined("developer"),
          predefined("tester"),
          role(
            "reviewer",
            "Independent reviewer",
            "verifier",
            "Independently checks each story against its definition of done.",
            "You see the task, the definition of done and the output, never the developer's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("product-owner", "Product owner", "product-owner", null),
          agent("scrum-master", "Scrum master", "scrum-master", "product-owner"),
          agent("developer-1", "Developer 1", "developer", "product-owner"),
          agent("developer-2", "Developer 2", "developer", "product-owner"),
          agent("tester", "Tester", "tester", "product-owner"),
          agent("reviewer", "Reviewer", "reviewer", "product-owner"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "kanban",
    name: "Kanban team",
    description: "Cards flow one at a time from doing to testing to done, with an independent reviewer on every card.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "kanban",
        name: "Kanban team",
        roles: [
          role(
            "flow-lead",
            "Flow lead",
            "chief_of_staff",
            "Keeps the board moving: pulls the next card only when the last one is done.",
            "Delegate one card at a time (a work-in-progress limit of one) to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          predefined("developer"),
          predefined("tester"),
          role(
            "reviewer",
            "Independent reviewer",
            "verifier",
            "Independently checks each card against its definition of done.",
            "You see the task, the definition of done and the output, never the developer's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("flow-lead", "Flow lead", "flow-lead", null),
          agent("developer", "Developer", "developer", "flow-lead"),
          agent("tester", "Tester", "tester", "flow-lead"),
          agent("reviewer", "Reviewer", "reviewer", "flow-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "review-board",
    name: "Review board",
    description: "A change summarizer and three independent reviewers on different models: correctness, security and maintainability. All three must pass a change.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "review-board",
        name: "Review board",
        roles: [
          role(
            "chair",
            "Chair",
            "chief_of_staff",
            "Runs the board: sends each change to the summarizer and the reviewers and records the decision.",
            "Delegate only to eligible agents below you, within policy and budget. A change is accepted only when every reviewer passes it.",
          ),
          role(
            "change-summarizer",
            "Change summarizer",
            "worker",
            "Summarizes a change for the board and records its decision.",
            "Summarize what changed, why, and the risky parts. Never review your own summary.",
            ["git"],
          ),
          role(
            "correctness-reviewer",
            "Correctness reviewer",
            "verifier",
            "Checks that the change does what it says.",
            "Check the summary against the definition of done; cite evidence for every finding.",
          ),
          role(
            "security-reviewer",
            "Security reviewer",
            "verifier",
            "Checks the change for security problems.",
            "Check for injection, secrets, unsafe defaults and permission changes; cite evidence for every finding.",
          ),
          role(
            "maintainability-reviewer",
            "Maintainability reviewer",
            "verifier",
            "Checks that the change is readable and tested.",
            "Check readability, tests and the canon; cite evidence for every finding.",
          ),
        ],
        agents: [
          agent("chair", "Chair", "chair", null),
          agent("change-summarizer", "Change summarizer", "change-summarizer", "chair"),
          agent("correctness-reviewer", "Correctness reviewer", "correctness-reviewer", "chair"),
          agent("security-reviewer", "Security reviewer", "security-reviewer", "chair"),
          agent("maintainability-reviewer", "Maintainability reviewer", "maintainability-reviewer", "chair"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "support",
    name: "Support team",
    description: "A support lead, a triager and a responder handle each ticket; an independent reply checker verifies the triage. No customer message goes out without a human.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "support",
        name: "Support team",
        roles: [
          role(
            "support-lead",
            "Support lead",
            "chief_of_staff",
            "Keeps the queue moving: sends each ticket to triage, then to a reply draft.",
            "Delegate one ticket at a time to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          role(
            "triager",
            "Triager",
            "worker",
            "Reads a ticket and names its category and priority.",
            "State the category and priority and the reason. Never contact the customer.",
          ),
          role(
            "responder",
            "Responder",
            "worker",
            "Drafts the reply to the customer from the canon.",
            "Draft a reply that answers the question from the canon. A draft is never sent without a human.",
          ),
          role(
            "reply-checker",
            "Reply checker",
            "verifier",
            "Independently checks the triage against the support policy.",
            "You see the task, the definition of done and the output, never the triager's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("support-lead", "Support lead", "support-lead", null),
          agent("triager", "Triager", "triager", "support-lead"),
          agent("responder", "Responder", "responder", "support-lead"),
          agent("reply-checker", "Reply checker", "reply-checker", "support-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "sales",
    name: "Sales team",
    description: "A sales lead, a qualifier and an outreach writer work each lead; an independent claims checker verifies the qualification. No message goes out and the CRM is never written without a human.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "sales",
        name: "Sales team",
        roles: [
          role(
            "sales-lead",
            "Sales lead",
            "chief_of_staff",
            "Keeps the pipeline moving: sends each lead to qualification, then to an outreach draft.",
            "Delegate one lead at a time to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          role(
            "qualifier",
            "Qualifier",
            "worker",
            "Decides whether a lead fits the target profile and says why.",
            "Qualify only on stated facts; invent nothing about the lead. Never contact the lead.",
          ),
          role(
            "outreach-writer",
            "Outreach writer",
            "worker",
            "Drafts the first outreach message.",
            "Draft a short message that makes no claim we cannot back up. A draft is never sent without a human.",
          ),
          role(
            "claims-checker",
            "Claims checker",
            "verifier",
            "Independently checks the qualification for unsupported claims.",
            "You see the task, the definition of done and the output, never the qualifier's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("sales-lead", "Sales lead", "sales-lead", null),
          agent("qualifier", "Qualifier", "qualifier", "sales-lead"),
          agent("outreach-writer", "Outreach writer", "outreach-writer", "sales-lead"),
          agent("claims-checker", "Claims checker", "claims-checker", "sales-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "qa",
    name: "QA and testing team",
    description: "A QA lead, a test designer and a test runner cover each feature; an independent test reviewer checks the test plan. The runner may only run tests.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "qa",
        name: "QA and testing team",
        roles: [
          role(
            "qa-lead",
            "QA lead",
            "chief_of_staff",
            "Keeps testing moving: sends each feature to test design, then to a test run.",
            "Delegate one feature at a time to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          role(
            "test-designer",
            "Test designer",
            "worker",
            "Designs the tests for a feature: happy path, edge cases and failures.",
            "Cover the happy path, an edge case and a failure case with an expected result for each. Change no files.",
          ),
          role(
            "test-runner",
            "Test runner",
            "worker",
            "Runs the tests and reports the result.",
            "Run the tests and report passes and failures exactly. Change no files and never merge.",
          ),
          role(
            "test-reviewer",
            "Test reviewer",
            "verifier",
            "Independently checks the test plan for gaps.",
            "You see the task, the definition of done and the output, never the designer's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("qa-lead", "QA lead", "qa-lead", null),
          agent("test-designer", "Test designer", "test-designer", "qa-lead"),
          agent("test-runner", "Test runner", "test-runner", "qa-lead"),
          agent("test-reviewer", "Test reviewer", "test-reviewer", "qa-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "devops",
    name: "DevOps team",
    description: "An ops lead, a change planner and a staging checker handle each change; an independent change reviewer verifies the plan. A deploy or infrastructure change always asks a human.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "devops",
        name: "DevOps team",
        roles: [
          role(
            "ops-lead",
            "Ops lead",
            "chief_of_staff",
            "Keeps changes moving: sends each change to planning, then to a staging check.",
            "Delegate one change at a time to eligible agents below you, within policy and budget. Report what was done and verified.",
          ),
          role(
            "change-planner",
            "Change planner",
            "worker",
            "Plans a change: ordered steps, health checks and a rollback.",
            "Write ordered steps, a health check after each step and a rollback. Change nothing.",
          ),
          role(
            "staging-checker",
            "Staging checker",
            "worker",
            "Checks a change in staging with read-only health checks.",
            "Run the staging health check and report the exact result. A deploy or infrastructure change always asks a human.",
          ),
          role(
            "change-reviewer",
            "Change reviewer",
            "verifier",
            "Independently checks the plan for steps, health checks and a rollback.",
            "You see the task, the definition of done and the output, never the planner's reasoning. Cite evidence for every item you check.",
          ),
        ],
        agents: [
          agent("ops-lead", "Ops lead", "ops-lead", null),
          agent("change-planner", "Change planner", "change-planner", "ops-lead"),
          agent("staging-checker", "Staging checker", "staging-checker", "ops-lead"),
          agent("change-reviewer", "Change reviewer", "change-reviewer", "ops-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "research-report",
    name: "Research report team",
    description: "A lead routing a research question to a researcher and a writer, with an independent fact checker and a human approval.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "research-report",
        name: "Research report team",
        roles: [
          role("research-lead", "Research lead", "chief_of_staff", "Research lead.", "Do the work of the research lead for this team."),
          role("researcher", "Researcher", "worker", "Researcher.", "Do the work of the researcher for this team."),
          role("writer", "Writer", "worker", "Writer.", "Do the work of the writer for this team."),
          role("fact-checker", "Fact checker", "verifier", "Fact checker.", "Do the work of the fact checker for this team."),
        ],
        agents: [
          agent("research-lead", "Research lead", "research-lead", null),
          agent("researcher", "Researcher", "researcher", "research-lead"),
          agent("writer", "Writer", "writer", "research-lead"),
          agent("fact-checker", "Fact checker", "fact-checker", "research-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "data-analysis",
    name: "Data analysis team",
    description: "A lead routing a data question to an analyst and a summarizer, with an independent number checker.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "data-analysis",
        name: "Data analysis team",
        roles: [
          role("analysis-lead", "Analysis lead", "chief_of_staff", "Analysis lead.", "Do the work of the analysis lead for this team."),
          role("analyst", "Analyst", "worker", "Analyst.", "Do the work of the analyst for this team."),
          role("summarizer", "Summarizer", "worker", "Summarizer.", "Do the work of the summarizer for this team."),
          role("number-checker", "Number checker", "verifier", "Number checker.", "Do the work of the number checker for this team."),
        ],
        agents: [
          agent("analysis-lead", "Analysis lead", "analysis-lead", null),
          agent("analyst", "Analyst", "analyst", "analysis-lead"),
          agent("summarizer", "Summarizer", "summarizer", "analysis-lead"),
          agent("number-checker", "Number checker", "number-checker", "analysis-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "document-extraction",
    name: "Document extraction team",
    description: "A lead routing invoices to an extractor and an exporter, with an independent invoice checker.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "document-extraction",
        name: "Document extraction team",
        roles: [
          role("extraction-lead", "Extraction lead", "chief_of_staff", "Extraction lead.", "Do the work of the extraction lead for this team."),
          role("extractor", "Extractor", "worker", "Extractor.", "Do the work of the extractor for this team."),
          role("exporter", "Exporter", "worker", "Exporter.", "Do the work of the exporter for this team."),
          role("invoice-checker", "Invoice checker", "verifier", "Invoice checker.", "Do the work of the invoice checker for this team."),
        ],
        agents: [
          agent("extraction-lead", "Extraction lead", "extraction-lead", null),
          agent("extractor", "Extractor", "extractor", "extraction-lead"),
          agent("exporter", "Exporter", "exporter", "extraction-lead"),
          agent("invoice-checker", "Invoice checker", "invoice-checker", "extraction-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "incident-triage",
    name: "Incident triage team",
    description: "A lead routing an alert to a triager, a responder and a postmortem writer, with human approval before any action.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "incident-triage",
        name: "Incident triage team",
        roles: [
          role("incident-lead", "Incident lead", "chief_of_staff", "Incident lead.", "Do the work of the incident lead for this team."),
          role("triager", "Triager", "worker", "Triager.", "Do the work of the triager for this team."),
          role("responder", "Responder", "worker", "Responder.", "Do the work of the responder for this team."),
          role("postmortem-writer", "Postmortem writer", "worker", "Postmortem writer.", "Do the work of the postmortem writer for this team."),
          role("incident-reviewer", "Incident reviewer", "verifier", "Incident reviewer.", "Do the work of the incident reviewer for this team."),
        ],
        agents: [
          agent("incident-lead", "Incident lead", "incident-lead", null),
          agent("triager", "Triager", "triager", "incident-lead"),
          agent("responder", "Responder", "responder", "incident-lead"),
          agent("postmortem-writer", "Postmortem writer", "postmortem-writer", "incident-lead"),
          agent("incident-reviewer", "Incident reviewer", "incident-reviewer", "incident-lead"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
  {
    id: "multi-framework-org",
    name: "Multi-framework organization",
    description: "Two teams (a native agent and a LangGraph agent) under one chief, with central policies, team budgets and one ledger.",
    document: {
      schema_version: SCHEMA_VERSION,
      org: {
        id: "multi-framework-org",
        name: "Multi-framework organization",
        roles: [
          role("chief", "Chief of Staff", "chief_of_staff", "Chief of Staff.", "Do the work of the chief of staff for this team."),
          role("team-lead", "Team lead", "worker", "Team lead.", "Do the work of the team lead for this team."),
          role("researcher", "Researcher (native agent)", "worker", "Researcher (native agent).", "Do the work of the researcher (native agent) for this team."),
          role("graph-drafter", "Drafter (LangGraph graph)", "worker", "Drafter (LangGraph graph).", "Do the work of the drafter (langgraph graph) for this team."),
          role("central-reviewer", "Central reviewer", "verifier", "Central reviewer.", "Do the work of the central reviewer for this team."),
        ],
        agents: [
          agent("chief", "Chief of Staff", "chief", null),
          agent("research-lead", "Research team lead", "team-lead", "chief"),
          agent("researcher", "Researcher", "researcher", "research-lead"),
          agent("ops-lead", "Operations team lead", "team-lead", "chief"),
          agent("graph-drafter", "Drafter", "graph-drafter", "ops-lead"),
          agent("central-reviewer", "Central reviewer", "central-reviewer", "chief"),
        ],
      },
      config: emptyConfig(),
      ui: emptyUi(),
      layout: { positions: {} },
    },
  },
];

/** Sets models on roles (a template's starting point; any agent can still override). */
function withModels(doc: BuilderDocument, models: Record<string, string>): BuilderDocument {
  return {
    ...doc,
    org: { ...doc.org, roles: doc.org.roles.map((r) => (models[r.id] === undefined ? r : { ...r, default_model: models[r.id] ?? null })) },
  };
}

/** Who checks whom in the launch templates, as in `src/nuramesh/templates/*` (#1538). */
const LAUNCH_CHECKS: Record<string, { lead: string; verifier: string; checks: string[] }> = {
  "research-report": { lead: "research-lead", verifier: "fact-checker", checks: ["researcher", "writer"] },
  "data-analysis": { lead: "analysis-lead", verifier: "number-checker", checks: ["analyst", "summarizer"] },
  "document-extraction": { lead: "extraction-lead", verifier: "invoice-checker", checks: ["extractor"] },
  "incident-triage": { lead: "incident-lead", verifier: "incident-reviewer", checks: ["triager", "postmortem-writer"] },
  "multi-framework-org": { lead: "chief", verifier: "central-reviewer", checks: ["researcher", "graph-drafter"] },
};

/**
 * The built-in templates with what a real team needs on top of its shape: models (the verifiers a
 * different family from the work they check), verification lines, permissions, gates, evals and
 * budgets (SPEC-app-v1 3.6). Built from the same pure edits the builder uses.
 */
function enrich(template: Template): Template {
  let doc = template.document;
  if (template.id === "dev-team") {
    doc = withModels(doc, {
      cos: "anthropic/claude-opus-5-5",
      developer: "anthropic/claude-sonnet-5-5",
      tester: "anthropic/claude-sonnet-5-5",
      reviewer: "openai/gpt-4.1-mini",
      devops: "anthropic/claude-sonnet-5-5",
    });
    for (const id of ["developer", "tester", "devops"]) doc = addCheck(doc, "reviewer", id);
    doc = addRule(doc, "cos", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "developer", { action: "write_file", decision: "allow", target: "src/*" });
    doc = addRule(doc, "developer", { action: "merge", decision: "ask", target: "*" });
    doc = addRule(doc, "tester", { action: "run_command", decision: "allow", target: "pytest **" });
    doc = addRule(doc, "reviewer", { action: "call_model", decision: "allow", target: "*" });
    doc = addRule(doc, "devops", { action: "merge", decision: "ask", target: "*" });
    doc = setAgentBudget(setAgentBudget(doc, "developer", "5.00"), "reviewer", "5.00");
  } else if (template.id === "code-review") {
    doc = withModels(doc, {
      "chief-of-staff": "anthropic/claude-opus-5-5",
      "change-summarizer": "anthropic/claude-sonnet-5-5",
      "code-reviewer": "openai/gpt-4.1-mini",
      "security-reviewer": "google/gemini-2.5-pro",
    });
    doc = addCheck(addCheck(doc, "code-reviewer", "change-summarizer"), "security-reviewer", "change-summarizer");
    doc = addRule(doc, "chief-of-staff", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "change-summarizer", { action: "use_tool", decision: "allow", target: "git" });
  } else if (template.id === "scrum") {
    doc = withModels(doc, {
      "product-owner": "anthropic/claude-opus-5-5",
      "scrum-master": "anthropic/claude-sonnet-5-5",
      developer: "anthropic/claude-sonnet-5-5",
      tester: "anthropic/claude-sonnet-5-5",
      reviewer: "openai/gpt-4.1-mini",
    });
    for (const id of ["developer-1", "developer-2"]) doc = addCheck(doc, "reviewer", id);
    doc = addRule(doc, "product-owner", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "developer", { action: "write_file", decision: "allow", target: "src/*" });
    doc = addRule(doc, "developer", { action: "merge", decision: "ask", target: "*" });
    doc = addRule(doc, "tester", { action: "run_command", decision: "allow", target: "pytest **" });
    doc = addRule(doc, "reviewer", { action: "call_model", decision: "allow", target: "*" });
    doc = setAgentBudget(setAgentBudget(doc, "developer-1", "5.00"), "developer-2", "5.00");
  } else if (template.id === "kanban") {
    doc = withModels(doc, {
      "flow-lead": "anthropic/claude-opus-5-5",
      developer: "anthropic/claude-sonnet-5-5",
      tester: "anthropic/claude-sonnet-5-5",
      reviewer: "openai/gpt-4.1-mini",
    });
    doc = addCheck(doc, "reviewer", "developer");
    doc = addRule(doc, "flow-lead", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "developer", { action: "write_file", decision: "allow", target: "src/*" });
    doc = addRule(doc, "developer", { action: "merge", decision: "ask", target: "*" });
    doc = addRule(doc, "tester", { action: "run_command", decision: "allow", target: "pytest **" });
    doc = setAgentBudget(doc, "developer", "5.00");
  } else if (template.id === "review-board") {
    // Three reviewers from three model families, none the summarizer's.
    doc = withModels(doc, {
      chair: "anthropic/claude-opus-5-5",
      "change-summarizer": "anthropic/claude-sonnet-5-5",
      "correctness-reviewer": "openai/gpt-4.1-mini",
      "security-reviewer": "google/gemini-2.5-pro",
      "maintainability-reviewer": "mistral/mistral-large-latest",
    });
    for (const reviewer of ["correctness-reviewer", "security-reviewer", "maintainability-reviewer"]) doc = addCheck(doc, reviewer, "change-summarizer");
    doc = addRule(doc, "chair", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "change-summarizer", { action: "use_tool", decision: "allow", target: "git" });
    doc = setAgentBudget(doc, "change-summarizer", "5.00");
  } else if (template.id === "support") {
    doc = withModels(doc, {
      "support-lead": "anthropic/claude-opus-5-5",
      "triager": "anthropic/claude-sonnet-5-5",
      "responder": "anthropic/claude-sonnet-5-5",
      "reply-checker": "openai/gpt-4.1-mini",
    });
    doc = addCheck(doc, "reply-checker", "triager");
    doc = addRule(doc, "support-lead", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "responder", { action: "external_message", decision: "ask", target: "*" });
    doc = addRule(doc, "responder", { action: "use_tool", decision: "deny", target: "refund-*" });
    doc = addRule(doc, "triager", { action: "external_message", decision: "deny", target: "*" });
    doc = setAgentBudget(setAgentBudget(doc, "triager", "5.00"), "responder", "5.00");
  } else if (template.id === "sales") {
    doc = withModels(doc, {
      "sales-lead": "anthropic/claude-opus-5-5",
      "qualifier": "anthropic/claude-sonnet-5-5",
      "outreach-writer": "anthropic/claude-sonnet-5-5",
      "claims-checker": "openai/gpt-4.1-mini",
    });
    doc = addCheck(doc, "claims-checker", "qualifier");
    doc = addRule(doc, "sales-lead", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "outreach-writer", { action: "external_message", decision: "ask", target: "*" });
    doc = addRule(doc, "outreach-writer", { action: "use_tool", decision: "deny", target: "crm-write" });
    doc = addRule(doc, "qualifier", { action: "external_message", decision: "deny", target: "*" });
    doc = setAgentBudget(setAgentBudget(doc, "qualifier", "5.00"), "outreach-writer", "5.00");
  } else if (template.id === "qa") {
    doc = withModels(doc, {
      "qa-lead": "anthropic/claude-opus-5-5",
      "test-designer": "anthropic/claude-sonnet-5-5",
      "test-runner": "anthropic/claude-sonnet-5-5",
      "test-reviewer": "openai/gpt-4.1-mini",
    });
    doc = addCheck(doc, "test-reviewer", "test-designer");
    doc = addRule(doc, "qa-lead", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "test-runner", { action: "run_command", decision: "allow", target: "pytest **" });
    doc = addRule(doc, "test-runner", { action: "write_file", decision: "deny", target: "**" });
    doc = addRule(doc, "test-runner", { action: "merge", decision: "deny", target: "*" });
    doc = addRule(doc, "test-runner", { action: "external_message", decision: "deny", target: "*" });
    doc = addRule(doc, "test-runner", { action: "use_tool", decision: "deny", target: "*" });
    doc = addRule(doc, "test-designer", { action: "write_file", decision: "deny", target: "**" });
    doc = addRule(doc, "test-designer", { action: "external_message", decision: "deny", target: "*" });
    doc = setAgentBudget(setAgentBudget(doc, "test-designer", "5.00"), "test-runner", "5.00");
  } else if (template.id === "devops") {
    doc = withModels(doc, {
      "ops-lead": "anthropic/claude-opus-5-5",
      "change-planner": "anthropic/claude-sonnet-5-5",
      "staging-checker": "anthropic/claude-sonnet-5-5",
      "change-reviewer": "openai/gpt-4.1-mini",
    });
    doc = addCheck(doc, "change-reviewer", "change-planner");
    doc = addRule(doc, "ops-lead", { action: "delegate", decision: "allow", target: "*" });
    doc = addRule(doc, "staging-checker", { action: "use_tool", decision: "allow", target: "staging-health-check" });
    doc = addRule(doc, "staging-checker", { action: "use_tool", decision: "ask", target: "deploy-*" });
    doc = addRule(doc, "staging-checker", { action: "use_tool", decision: "ask", target: "terraform-*" });
    doc = addRule(doc, "staging-checker", { action: "write_file", decision: "deny", target: "infra/**" });
    doc = addRule(doc, "change-planner", { action: "use_tool", decision: "deny", target: "*" });
    doc = addRule(doc, "change-planner", { action: "write_file", decision: "deny", target: "**" });
    doc = setAgentBudget(setAgentBudget(doc, "change-planner", "5.00"), "staging-checker", "5.00");
  } else if (template.id in LAUNCH_CHECKS) {
    // The launch templates (#1538): the verifier checks the listed workers; the lead may delegate.
    const spec = LAUNCH_CHECKS[template.id];
    if (spec !== undefined) {
      // Models by kind: the verifier is a different family from the work it checks.
      const family = { chief_of_staff: "anthropic/claude-opus-5-5", worker: "anthropic/claude-sonnet-5-5", verifier: "openai/gpt-4.1-mini" };
      doc = withModels(doc, Object.fromEntries(doc.org.roles.map((r) => [r.id, family[r.kind]])));
      const kinds = new Map(doc.org.roles.map((r) => [r.id, r.kind]));
      const worker = doc.org.agents.find((a) => kinds.get(a.role) === "worker" && a.reports_to !== null);
      if (worker !== undefined) doc = setAgentBudget(doc, worker.id, "5.00");
      for (const worker of spec.checks) doc = addCheck(doc, spec.verifier, worker);
      doc = addRule(doc, spec.lead, { action: "delegate", decision: "allow", target: "*" });
    }
  }
  return { ...template, document: doc };
}

/** Built-in team templates (PLAN B.3), configured. */
export const BUILT_IN_TEMPLATES: readonly Template[] = BASE_TEMPLATES.map(enrich);
