import { Copy, Trash2, Unlink } from "lucide-react";
import { useId, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { type Agent, fieldRules } from "@/model/org";

import { renamePerson } from "../config-ops";
import { edgeRef } from "../edge-ids";
import { kindLabel } from "../flow";
import { setManager, updateAgent, updateOrg } from "../ops";
import { useBuilder, useBuilderStore } from "../store-context";
import { avatarClass, avatarText } from "./appearance";
import { DraftField, Field, TextField } from "./fields";
import {
  AppearanceFields,
  BudgetSection,
  ChecksSection,
  EvalsSection,
  PlannerSection,
  GatesSection,
  ModelSection,
  PermissionsSection,
  SectionTitle,
} from "./inspector-sections";
import { YamlPanel } from "./yaml-panel";

/** Properties of whatever is selected: the organization, one agent, several agents or a line. */
export function Inspector() {
  const nodes = useBuilder((s) => s.selection.nodes);
  const edges = useBuilder((s) => s.selection.edges);
  if (nodes.size === 1) {
    const [id] = nodes;
    if (id !== undefined) return id.startsWith("human:") ? <PersonInspector personId={id} /> : <AgentInspector agentId={id} />;
  }
  if (nodes.size > 1) return <MultiInspector count={nodes.size} />;
  if (edges.size > 0) return <EdgeInspector reportIds={[...edges]} />;
  return <OrgInspector />;
}

function OrgInspector() {
  const store = useBuilderStore();
  const org = useBuilder((s) => s.doc.org);
  return (
    <div className="flex flex-col gap-4">
      <h3 className="text-sm font-semibold">Organization</h3>
      <TextField
        id="org-name"
        label="Name"
        value={org.name}
        error={fieldRules.name(org.name)}
        onChange={(name) => {
          store.getState().edit("Rename organization", (d) => updateOrg(d, { name }), "org:name");
        }}
      />
      <DraftField
        id="org-id"
        label="Id"
        value={org.id}
        hint="Used in config files and the API."
        check={(draft) => fieldRules.slug(draft)}
        onCommit={(id) => {
          store.getState().edit("Change organization id", (d) => updateOrg(d, { id }), "org:id");
        }}
      />
      <section aria-labelledby="shortcuts-title" className="text-xs text-muted-foreground">
        <h4 id="shortcuts-title" className="mb-1 font-medium text-foreground">
          Keyboard
        </h4>
        <ul className="list-inside list-disc space-y-0.5">
          <li>Tab to an agent, Enter to select, arrow keys to move it</li>
          <li>Shift-click or Shift-drag to select several</li>
          <li>Delete removes the selection</li>
          <li>Ctrl/⌘ Z undo · Shift Ctrl/⌘ Z or Ctrl Y redo</li>
          <li>Ctrl/⌘ A select all · C copy · X cut · V paste · D duplicate</li>
        </ul>
      </section>
    </div>
  );
}

/** Ids of agents below `id` (it can't report to them: that would be a loop). */
function below(agents: readonly Agent[], id: string): Set<string> {
  const reports = new Map<string, string[]>();
  for (const a of agents) {
    if (a.reports_to === null) continue;
    const list = reports.get(a.reports_to) ?? [];
    list.push(a.id);
    reports.set(a.reports_to, list);
  }
  const found = new Set<string>();
  const stack = [...(reports.get(id) ?? [])];
  while (stack.length > 0) {
    const next = stack.pop();
    if (next === undefined || found.has(next) || next === id) continue;
    found.add(next);
    stack.push(...(reports.get(next) ?? []));
  }
  return found;
}

function AgentInspector({ agentId }: { agentId: string }) {
  const store = useBuilderStore();
  const agents = useBuilder((s) => s.doc.org.agents);
  const roles = useBuilder((s) => s.doc.org.roles);
  const issues = useBuilder((s) => s.issues.issues);
  const appearance = useBuilder((s) => s.doc.ui.appearance[agentId]);
  const editKey = useId();
  const agent = agents.find((a) => a.id === agentId);
  const managers = useMemo(() => {
    if (agent === undefined) return [];
    const excluded = below(agents, agent.id);
    return agents.filter((a) => a.id !== agent.id && !excluded.has(a.id));
  }, [agents, agent]);
  if (agent === undefined) return null;
  const own = issues.filter((i) => i.target?.kind === "agent" && i.target.id === agent.id);
  const role = roles.find((r) => r.id === agent.role);
  const roleKnown = role !== undefined;
  const taken = new Set(agents.map((a) => a.id));
  const manager = agents.find((a) => a.id === agent.reports_to);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className={cn("flex size-10 shrink-0 items-center justify-center rounded-lg font-mono text-xs font-semibold", avatarClass(appearance ?? {}, role?.kind ?? null))}
        >
          {avatarText(appearance ?? {}, agent.name)}
        </span>
        <div className="flex min-w-0 flex-col">
          <h3 className="truncate text-base font-semibold">{agent.name}</h3>
          <span className="truncate text-xs text-small">
            {role ? kindLabel(role.kind) : "unknown role"} · {manager ? `reports to ${manager.name}` : "top of the organization"}
          </span>
        </div>
      </div>
      <Tabs defaultValue="settings" className="gap-3">
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="yaml">YAML</TabsTrigger>
        </TabsList>
        <TabsContent value="settings" className="flex flex-col gap-5">
          <section aria-labelledby="sec-identity" className="flex flex-col gap-3">
            <SectionTitle id="sec-identity">Identity</SectionTitle>
            <TextField
              id="agent-name"
              label="Name"
              value={agent.name}
              error={fieldRules.name(agent.name)}
              onChange={(name) => {
                store.getState().edit("Rename agent", (d) => updateAgent(d, agent.id, { name }), `agent:${agent.id}:name`);
              }}
            />
            <DraftField
              id="agent-id"
              label="Id"
              value={agent.id}
              hint="Ledger, budgets and runs use this id; the name can change any time."
              check={(draft) => fieldRules.slug(draft) ?? (taken.has(draft) ? "Another agent has this id" : null)}
              onCommit={(id) => {
                store.getState().renameAgentId(agent.id, id, `agent-id:${editKey}`);
              }}
            />
            <Field id="agent-role" label="Role" error={roleKnown ? null : `Role '${agent.role}' doesn't exist`}>
              <div className="flex items-center gap-2">
                <NativeSelect
                  id="agent-role"
                  className="min-w-0 flex-1"
                  value={agent.role}
                  aria-invalid={roleKnown ? undefined : true}
                  onChange={(e) => {
                    const next = e.target.value;
                    store.getState().edit("Change role", (d) => updateAgent(d, agent.id, { role: next }));
                  }}
                >
                  {!roleKnown && <NativeSelectOption value={agent.role}>{agent.role} (missing)</NativeSelectOption>}
                  {roles.map((r) => (
                    <NativeSelectOption key={r.id} value={r.id}>
                      {r.name}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                {roleKnown && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      store.getState().focusRole(agent.role);
                    }}
                  >
                    Edit role
                  </Button>
                )}
              </div>
            </Field>
            <AppearanceFields agent={agent} />
            <Field id="agent-manager" label="Reports to" hint="Or drag from a manager's bottom handle to this agent.">
              <NativeSelect
                id="agent-manager"
                className="w-full"
                aria-describedby="agent-manager-hint"
                value={agent.reports_to ?? ""}
                onChange={(e) => {
                  const next = e.target.value === "" ? null : e.target.value;
                  store.getState().edit(next === null ? "Remove manager" : "Set manager", (d) => setManager(d, agent.id, next));
                }}
              >
                <NativeSelectOption value="">No one (top of the organization)</NativeSelectOption>
                {agent.reports_to !== null && !managers.some((m) => m.id === agent.reports_to) && (
                  <NativeSelectOption value={agent.reports_to}>{agent.reports_to} (invalid)</NativeSelectOption>
                )}
                {managers.map((m) => (
                  <NativeSelectOption key={m.id} value={m.id}>
                    {m.name} ({m.id})
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </Field>
          </section>
          <ModelSection agent={agent} role={role} />
          <BudgetSection agent={agent} />
          {role !== undefined && (
            <>
              <PermissionsSection role={role} />
              <GatesSection key={role.id} role={role} />
              <EvalsSection role={role} />
              <PlannerSection agentId={agent.id} role={role} />
            </>
          )}
          <ChecksSection agent={agent} role={role} />
          {own.length > 0 && (
            <section aria-labelledby="agent-issues" className="flex flex-col gap-1">
              <h4 id="agent-issues" className="text-xs font-medium">
                Problems
              </h4>
              <ul className="space-y-1 text-xs">
                {own.map((issue) => (
                  <li key={issue.key} className={issue.severity === "error" ? "text-destructive" : "text-warning"}>
                    {issue.message}
                  </li>
                ))}
              </ul>
            </section>
          )}
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                store.getState().duplicate();
              }}
            >
              <Copy aria-hidden="true" /> Duplicate
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => {
                store.getState().deleteSelection();
              }}
            >
              <Trash2 aria-hidden="true" /> Delete agent
            </Button>
          </div>
        </TabsContent>
        <TabsContent value="yaml">
          <YamlPanel agentId={agent.id} agentName={agent.name} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PersonInspector({ personId }: { personId: string }) {
  const store = useBuilderStore();
  const person = useBuilder((s) => s.doc.ui.people.find((p) => p.id === personId));
  const policies = useBuilder((s) => s.doc.config.policies);
  const roles = useBuilder((s) => s.doc.org.roles);
  if (person === undefined) return null;
  const approves = policies.filter((p) => p.approvers.includes(person.id)).map((p) => roles.find((r) => r.id === p.role)?.name ?? p.role);
  return (
    <div className="flex flex-col gap-4">
      <h3 className="truncate text-sm font-semibold">Human approver: {person.label}</h3>
      <TextField
        id="person-label"
        label="Label on the canvas"
        value={person.label}
        hint="Only shown here. The ledger and policies refer to this person by an opaque id, never a name or email."
        error={fieldRules.name(person.label)}
        onChange={(label) => {
          store.getState().edit("Rename approver", (d) => renamePerson(d, person.id, label), `person:${person.id}`);
        }}
      />
      <p className="font-mono text-xs break-all text-small">{person.id}</p>
      <p className="text-sm">
        {approves.length === 0 ? "Approves nothing yet. Drag from the right handle to an agent." : `Approves for: ${approves.join(", ")}`}
      </p>
      <div>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => {
            store.getState().deleteElements([person.id], []);
          }}
        >
          <Trash2 aria-hidden="true" /> Remove approver
        </Button>
      </div>
    </div>
  );
}

function MultiInspector({ count }: { count: number }) {
  const store = useBuilderStore();
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">{count} agents selected</h3>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            store.getState().duplicate();
          }}
        >
          <Copy aria-hidden="true" /> Duplicate
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => {
            store.getState().deleteSelection();
          }}
        >
          <Trash2 aria-hidden="true" /> Delete {count} agents
        </Button>
      </div>
    </div>
  );
}

function EdgeInspector({ reportIds: keys }: { reportIds: string[] }) {
  const store = useBuilderStore();
  const agents = useBuilder((s) => s.doc.org.agents);
  const people = useBuilder((s) => s.doc.ui.people);
  const names = new Map(agents.map((a) => [a.id, a.name]));
  const name = (id: string): string => names.get(id) ?? people.find((p) => p.id === id)?.label ?? id;
  const describe = (key: string): string => {
    const ref = edgeRef(key);
    if (ref.kind === "check") return `${name(ref.verifier)} verifies ${name(ref.checked)}`;
    if (ref.kind === "approval") return `${name(ref.person)} approves for the role of ${name(ref.agent)} (every agent that holds it)`;
    const manager = agents.find((a) => a.id === ref.report)?.reports_to;
    return `${name(ref.report)} reports to ${manager ? name(manager) : "no one"}`;
  };
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold">Line{keys.length === 1 ? "" : "s"}</h3>
      <ul className="space-y-1 text-sm">
        {keys.map((key) => (
          <li key={key}>{describe(key)}</li>
        ))}
      </ul>
      <div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            store.getState().deleteElements([], keys);
          }}
        >
          <Unlink aria-hidden="true" /> Remove line{keys.length === 1 ? "" : "s"}
        </Button>
      </div>
    </div>
  );
}
