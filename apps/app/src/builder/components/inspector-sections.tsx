import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, X } from "lucide-react";
import { useState } from "react";

import { useOptionalApi } from "@/api/context";
import type { PolicyPreset } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import {
  ACCENTS,
  ACTIONS,
  agentBudget,
  approversOf,
  assignmentOf,
  DECISIONS,
  effectiveRules,
  gatesOf,
  MONEY,
  policyOf,
  type PolicyRule,
  ruleProblem,
} from "@/model/config";
import { effectiveModel, modelFamily, isOpenWeight } from "@/model/family";
import { type Agent, fieldRules, type Role } from "@/model/org";

import {
  addGate,
  addRule,
  applyPolicyPreset,
  moveGate,
  removeCheck,
  removeGate,
  removePolicyPreset,
  removeRule,
  setAgentBudget,
  setAppearance,
  setApprovers,
  setGateRequired,
  setRoleChecks,
  setRoleEvals,
  updateRule,
  addCheck,
} from "../config-ops";
import { useBuilder, useBuilderStore } from "../store-context";
import { effectiveSuites, gatePresetForRole, instantiateGates } from "../gate-plan";
import { Field, TextField } from "./fields";

export function SectionTitle({ id, children, aside }: { id: string; children: string; aside?: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <h4 id={id} className="font-mono text-[11px] tracking-[0.1em] text-small uppercase">
        {children}
      </h4>
      {aside !== undefined && <span className="ml-auto text-xs text-small">{aside}</span>}
    </div>
  );
}

const DECISION_CHIP: Record<string, string> = {
  allow: "chip-success",
  ask: "chip-warning",
  deny: "chip-danger",
};

const DECISION_STYLE: Record<string, string> = {
  allow: "bg-link-soft text-link",
  ask: "bg-warm-soft text-warm",
  deny: "bg-danger-soft text-danger",
};

const MODEL_SUGGESTIONS = [
  "anthropic/claude-sonnet-5-5",
  "anthropic/claude-opus-5-5",
  "anthropic/claude-haiku-5-5",
  "openai/gpt-4.1-mini",
  "google/gemini-2.5-pro",
  "meta/llama-3.1-70b",
];

const iconButton =
  "inline-flex size-11 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-40";

// ---- identity: avatar and colour ------------------------------------------------------------------

export function AppearanceFields({ agent }: { agent: Agent }) {
  const store = useBuilderStore();
  const appearance = useBuilder((s) => s.doc.ui.appearance[agent.id]);
  return (
    <div className="grid grid-cols-2 gap-3">
      <TextField
        id="agent-avatar"
        label="Avatar"
        value={appearance?.avatar ?? ""}
        placeholder="Initials"
        hint="Up to 3 characters."
        onChange={(avatar) => {
          store.getState().edit("Change avatar", (d) => setAppearance(d, agent.id, { avatar: avatar.slice(0, 3) }), `agent:${agent.id}:avatar`);
        }}
      />
      <Field id="agent-color" label="Colour">
        <NativeSelect
          id="agent-color"
          className="w-full"
          value={appearance?.color ?? ""}
          onChange={(e) => {
            store.getState().edit("Change colour", (d) => setAppearance(d, agent.id, { color: e.target.value }));
          }}
        >
          <NativeSelectOption value="">Automatic</NativeSelectOption>
          {ACCENTS.map((c) => (
            <NativeSelectOption key={c} value={c}>
              {c[0]?.toUpperCase()}
              {c.slice(1)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </Field>
    </div>
  );
}

// ---- model ----------------------------------------------------------------------------------------

export function ModelSection({ agent, role }: { agent: Agent; role: Role | undefined }) {
  const store = useBuilderStore();
  const org = useBuilder((s) => s.doc.org);
  const own = agent.model ?? "";
  const model = effectiveModel(agent, role);
  const family = modelFamily(model);
  const checkedFamilies = agent.checks
    .map((id) => org.agents.find((a) => a.id === id))
    .flatMap((a) => (a === undefined ? [] : [modelFamily(effectiveModel(a, org.roles.find((r) => r.id === a.role)))]))
    .filter((f): f is string => f !== null);
  const used = [...new Set(org.agents.map((a) => a.model).filter((m): m is string => m !== null))];
  const options = [...new Set([...MODEL_SUGGESTIONS, ...used])];
  const note =
    role?.kind === "verifier" && agent.checks.length > 0
      ? `A verifier must be a different model family from the work it checks${checkedFamilies.length > 0 ? ` (now: ${[...new Set(checkedFamilies)].join(", ")})` : ""}.`
      : undefined;
  return (
    <section aria-labelledby="sec-model" className="flex flex-col gap-2">
      <SectionTitle id="sec-model">Model</SectionTitle>
      <TextField
        id="agent-model"
        label="Model"
        value={own}
        placeholder={role?.default_model ?? "No model chosen"}
        hint={note ?? "provider/name. Leave empty to use the role's default model."}
        error={agent.model === null ? null : fieldRules.model(agent.model)}
        onChange={(value) => {
          store.getState().edit("Change model", (d) => ({ ...d, org: { ...d.org, agents: d.org.agents.map((a) => (a.id === agent.id ? { ...a, model: value === "" ? null : value } : a)) } }), `agent:${agent.id}:model`);
        }}
        list="model-suggestions"
      />
      <datalist id="model-suggestions">
        {options.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {model !== null && (
        <p className="text-xs text-small" data-testid="model-family">
          {family} family · {isOpenWeight(model) ? "open weights (dashed chip)" : "hosted model"}
        </p>
      )}
    </section>
  );
}

// ---- budget ---------------------------------------------------------------------------------------

export function BudgetSection({ agent }: { agent: Agent }) {
  const store = useBuilderStore();
  const budget = useBuilder((s) => agentBudget(s.doc.config, agent.id));
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? budget?.limit.amount ?? "";
  const error = shown !== "" && !MONEY.test(shown) ? "An amount such as 5.00" : null;
  return (
    <section aria-labelledby="sec-budget" className="flex flex-col gap-2">
      <SectionTitle id="sec-budget">Budget</SectionTitle>
      <Field id="agent-budget" label="Limit (USD)" error={error} hint="Empty means no budget of its own: the run and organization limits still apply.">
        <Input
          id="agent-budget"
          inputMode="decimal"
          value={shown}
          placeholder="No limit of its own"
          aria-invalid={error !== null ? true : undefined}
          aria-describedby={error !== null ? "agent-budget-error" : "agent-budget-hint"}
          onChange={(e) => {
            const next = e.target.value.trim();
            setDraft(next);
            if (next === "") store.getState().edit("Remove budget", (d) => setAgentBudget(d, agent.id, null), `agent:${agent.id}:budget`);
            else if (MONEY.test(next)) store.getState().edit("Change budget", (d) => setAgentBudget(d, agent.id, next), `agent:${agent.id}:budget`);
          }}
          onBlur={() => {
            setDraft(null);
          }}
        />
      </Field>
    </section>
  );
}

// ---- permissions ----------------------------------------------------------------------------------

function RuleRow({ roleId, index, rule }: { roleId: string; index: number; rule: PolicyRule }) {
  const store = useBuilderStore();
  const problem = ruleProblem(rule);
  const set = (patch: Partial<PolicyRule>): void => {
    store.getState().edit("Change permission", (d) => updateRule(d, roleId, index, { ...rule, ...patch }), `rule:${roleId}:${String(index)}`);
  };
  return (
    <li className="flex flex-col gap-1.5 rounded-md border p-2">
      <div className="flex items-center gap-1.5">
        <NativeSelect
          size="sm"
          aria-label={`Rule ${String(index + 1)} decision`}
          value={rule.decision}
          className={`w-24 ${DECISION_STYLE[rule.decision] ?? ""}`}
          onChange={(e) => {
            set({ decision: e.target.value as PolicyRule["decision"] });
          }}
        >
          {DECISIONS.map((d) => (
            <NativeSelectOption key={d} value={d}>
              {d}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <NativeSelect
          size="sm"
          aria-label={`Rule ${String(index + 1)} action`}
          value={rule.action}
          className="min-w-0 flex-1"
          onChange={(e) => {
            const action = e.target.value as PolicyRule["action"];
            set({ action, ...(action === "spend" ? { target: "*" } : {}) });
          }}
        >
          {ACTIONS.map((a) => (
            <NativeSelectOption key={a} value={a}>
              {a}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <button
          type="button"
          className={iconButton}
          aria-label={`Remove rule ${String(index + 1)}`}
          onClick={() => {
            store.getState().edit("Remove permission", (d) => removeRule(d, roleId, index));
          }}
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>
      <Input
        aria-label={`Rule ${String(index + 1)} target`}
        className="h-8 font-mono text-xs"
        value={rule.target}
        disabled={rule.action === "spend"}
        aria-invalid={problem !== null ? true : undefined}
        onChange={(e) => {
          set({ target: e.target.value });
        }}
      />
      {problem !== null && <p className="text-xs text-destructive">{problem}</p>}
    </li>
  );
}

export function PermissionsSection({ role }: { role: Role }) {
  const store = useBuilderStore();
  const api = useOptionalApi();
  const config = useBuilder((s) => s.doc.config);
  const people = useBuilder((s) => s.doc.ui.people);
  const presets = useQuery({
    queryKey: ["policyPresets"],
    queryFn: () => (api ? api.policyPresets.list() : Promise.reject(new Error("no api"))),
    enabled: api !== null,
  });
  const own = policyOf(config, role.id)?.rules ?? [];
  const applied = assignmentOf(config, role.id)?.policy_presets ?? [];
  const inherited = effectiveRules(config, role.id).filter((r) => r.source !== "This role");
  const approvers = approversOf(config, role.id);
  const available: PolicyPreset[] = (presets.data?.policy ?? []).filter((p) => !applied.includes(p.id));
  return (
    <section aria-labelledby="sec-permissions" className="flex flex-col gap-2">
      <SectionTitle id="sec-permissions" aside="Default: deny">
        Permissions
      </SectionTitle>
      <p className="text-xs text-muted-foreground">
        For every agent with the {role.name} role. Anything not allowed here asks a person or is denied.
      </p>
      {applied.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Applied presets">
          {applied.map((id) => {
            const def = config.presets.policy.find((p) => p.id === id);
            return (
              <li key={id} className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs">
                {def?.name ?? id}
                <button
                  type="button"
                  aria-label={`Remove preset ${def?.name ?? id}`}
                  className="relative text-muted-foreground before:absolute before:-inset-3 hover:text-foreground"
                  onClick={() => {
                    store.getState().edit("Remove preset", (d) => removePolicyPreset(d, role.id, id));
                  }}
                >
                  <X aria-hidden="true" className="size-3" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {inherited.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Rules from presets">
          {inherited.map((r, i) => (
            <li key={`${r.source}-${String(i)}`} className="flex items-center gap-2 rounded-md border border-dashed px-2 py-1 text-xs">
              <span className={`chip font-mono ${DECISION_CHIP[r.rule.decision] ?? "chip-neutral"}`}>{r.rule.decision}</span>
              <span className="font-mono">{r.rule.action}</span>
              <span className="truncate font-mono text-small">{r.rule.target}</span>
              <span className="ml-auto shrink-0 text-small">{r.source}</span>
            </li>
          ))}
        </ul>
      )}
      <ul className="flex flex-col gap-1.5" aria-label="Rules of this role">
        {own.map((rule, index) => (
          <RuleRow key={`${role.id}-${String(index)}`} roleId={role.id} index={index} rule={rule} />
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          className="border-dashed"
          onClick={() => {
            store.getState().edit("Add permission", (d) => addRule(d, role.id, { action: "call_model", decision: "ask", target: "*" }));
          }}
        >
          <Plus aria-hidden="true" /> Add rule
        </Button>
        {api !== null && (
          <Field id="preset-apply" label="Apply policy preset">
            <NativeSelect
              id="preset-apply"
              size="sm"
              value=""
              disabled={available.length === 0}
              onChange={(e) => {
                const preset = available.find((p) => p.id === e.target.value);
                if (preset === undefined) return;
                store.getState().edit(`Apply preset ${preset.name}`, (d) =>
                  applyPolicyPreset(d, role.id, { id: preset.id, name: preset.name, description: preset.description, rules: preset.rules }),
                );
              }}
            >
              <NativeSelectOption value="">{available.length === 0 ? "No more presets" : "Choose…"}</NativeSelectOption>
              {available.map((p) => (
                <NativeSelectOption key={p.id} value={p.id}>
                  {p.name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
        )}
      </div>
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1 text-xs font-medium">Approvers</legend>
        {people.length === 0 ? (
          <p className="text-xs text-muted-foreground">No people yet. Add a human approver, then choose who answers this role's approval requests.</p>
        ) : (
          people.map((p) => (
            <label key={p.id} className="flex min-h-11 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={approvers.includes(p.id)}
                onChange={(e) => {
                  store.getState().edit("Change approvers", (d) => setApprovers(d, role.id, e.target.checked ? [...approvers, p.id] : approvers.filter((x) => x !== p.id)));
                }}
              />
              {p.label}
            </label>
          ))
        )}
        <Button
          variant="outline"
          size="sm"
          className="w-fit border-dashed"
          onClick={() => {
            store.getState().addPersonNode();
          }}
        >
          <Plus aria-hidden="true" /> Add human approver
        </Button>
      </fieldset>
    </section>
  );
}

// ---- gates and evals --------------------------------------------------------------------------------

const GATE_LABEL: Record<string, string> = {
  rule: "Rule check",
  test_command: "Test command",
  verifier: "Independent verifier",
  human: "Human approval",
  eval: "Eval",
};

export function GatesSection({ role }: { role: Role }) {
  const store = useBuilderStore();
  const preset = useBuilder((s) => gatesOf(s.doc.config, role.id));
  const gates = preset?.gates ?? [];
  return (
    <section aria-labelledby="sec-gates" className="flex flex-col gap-2">
      <SectionTitle id="sec-gates" aside="Cheapest first">
        Gates
      </SectionTitle>
      {gates.length === 0 ? (
        <p className="text-xs text-muted-foreground">No gates: this role's work is not checked before it counts as done.</p>
      ) : (
        <ol className="flex flex-col gap-1.5">
          {gates.map((g, i) => (
            <li key={g.id} className="flex items-center gap-2 rounded-md border p-2">
              <span aria-hidden="true" className="size-3 shrink-0 rotate-45 rounded-[3px] border-2 border-link" />
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-sm font-medium">{GATE_LABEL[g.kind] ?? g.kind}</span>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={g.required}
                    onChange={(e) => {
                      store.getState().edit("Change gate", (d) => setGateRequired(d, role.id, g.id, e.target.checked, role.name));
                    }}
                  />
                  Required
                </label>
              </div>
              <button
                type="button"
                className={iconButton}
                aria-label={`Move ${GATE_LABEL[g.kind] ?? g.kind} up`}
                disabled={i === 0}
                onClick={() => {
                  store.getState().edit("Reorder gates", (d) => moveGate(d, role.id, g.id, -1, role.name));
                }}
              >
                <ArrowUp aria-hidden="true" className="size-4" />
              </button>
              <button
                type="button"
                className={iconButton}
                aria-label={`Move ${GATE_LABEL[g.kind] ?? g.kind} down`}
                disabled={i === gates.length - 1}
                onClick={() => {
                  store.getState().edit("Reorder gates", (d) => moveGate(d, role.id, g.id, 1, role.name));
                }}
              >
                <ArrowDown aria-hidden="true" className="size-4" />
              </button>
              <button
                type="button"
                className={iconButton}
                aria-label={`Remove ${GATE_LABEL[g.kind] ?? g.kind}`}
                onClick={() => {
                  store.getState().edit("Remove gate", (d) => removeGate(d, role.id, g.id, role.name));
                }}
              >
                <X aria-hidden="true" className="size-4" />
              </button>
            </li>
          ))}
        </ol>
      )}
      <Field id="gate-add" label="Add a gate">
        <NativeSelect
          id="gate-add"
          size="sm"
          value=""
          onChange={(e) => {
            if (e.target.value === "") return;
            const kind = e.target.value as "rule" | "test_command" | "verifier";
            store.getState().edit(`Add ${GATE_LABEL[kind] ?? kind}`, (d) => addGate(d, role.id, kind, role.name));
          }}
        >
          <NativeSelectOption value="">+ Add…</NativeSelectOption>
          {(["rule", "test_command", "verifier"] as const).map((k) => (
            <NativeSelectOption key={k} value={k}>
              {GATE_LABEL[k]}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </Field>
    </section>
  );
}

/** A stable empty list: a selector that returns a new array every time would re-render forever. */
const NO_SUITES: readonly string[] = [];

export function EvalsSection({ role }: { role: Role }) {
  const store = useBuilderStore();
  const suites = useBuilder((s) => s.doc.config.evals.find((e) => e.role === role.id)?.suites ?? NO_SUITES);
  const [draft, setDraft] = useState("");
  const error = draft !== "" ? fieldRules.slug(draft) : null;
  return (
    <section aria-labelledby="sec-evals" className="flex flex-col gap-2">
      <SectionTitle id="sec-evals">Evals</SectionTitle>
      <p className="text-xs text-muted-foreground">Suites an agent with this role must have passed before it takes new work. The suites themselves are defined in the evals part of your config in Git.</p>
      {suites.length > 0 && (
        <ul className="flex flex-col gap-1">
          {suites.map((s) => (
            <li key={s} className="flex items-center gap-2 font-mono text-xs">
              <span className="min-w-0 flex-1 truncate">{s}</span>
              <button
                type="button"
                className={iconButton}
                aria-label={`Remove eval ${s}`}
                onClick={() => {
                  store.getState().edit("Remove eval", (d) => setRoleEvals(d, role.id, suites.filter((x) => x !== s)));
                }}
              >
                <X aria-hidden="true" className="size-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex items-start gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft === "" || error !== null || suites.includes(draft)) return;
          store.getState().edit("Require eval", (d) => setRoleEvals(d, role.id, [...suites, draft]));
          setDraft("");
        }}
      >
        <Field id="eval-add" label="Add an eval suite" error={error}>
          <Input
            id="eval-add"
            className="h-8 font-mono text-xs"
            value={draft}
            placeholder="suite-id"
            aria-invalid={error !== null ? true : undefined}
            onChange={(e) => {
              setDraft(e.target.value);
            }}
          />
        </Field>
        <Button type="submit" variant="outline" size="sm" className="mt-[1.65rem]" disabled={draft === "" || error !== null}>
          Add
        </Button>
      </form>
    </section>
  );
}

// ---- run plan -----------------------------------------------------------------------------------------

const NO_CHECKS: Record<string, string[]> = {};
const planDrafts = new WeakMap<object, Map<string, Map<string, string>>>();

function draftsFor(store: object, agentId: string): Map<string, string> {
  let byAgent = planDrafts.get(store);
  if (byAgent === undefined) {
    byAgent = new Map<string, Map<string, string>>();
    planDrafts.set(store, byAgent);
  }
  let drafts = byAgent.get(agentId);
  if (drafts === undefined) {
    drafts = new Map<string, string>();
    byAgent.set(agentId, drafts);
  }
  return drafts;
}

/** What a task for this role will carry: the criteria each gate of its preset checks, and the eval suites it needs. */
export function PlannerSection({ agentId, role }: { agentId: string; role: Role }) {
  const store = useBuilderStore();
  const config = useBuilder((s) => s.doc.config);
  const preset = gatePresetForRole(config, role.id);
  const stored = config.checks.find((c) => c.role === role.id)?.checks ?? NO_CHECKS;
  // Unsaved criterion text is kept per agent (outside this component, which unmounts when the selection changes) until it is fixed or cleared.
  const [, rerender] = useState(0);
  const drafts = draftsFor(store, agentId);
  if (preset === null) return null;
  const text = (gate: string) => drafts.get(gate) ?? (stored[gate] ?? []).join(", ");
  // Only gates the preset still has: a gate removed since is dropped, as the core refuses checks for unknown gates.
  const checks: Record<string, string[]> = {};
  for (const g of preset.gates) if ((stored[g.id] ?? []).length > 0) checks[g.id] = stored[g.id] ?? [];
  let gates: ReturnType<typeof instantiateGates> = [];
  let problem: string | null = null;
  try {
    gates = instantiateGates(preset, checks);
  } catch (error) {
    problem = error instanceof Error ? error.message : "Can't plan these gates";
  }
  const suites = effectiveSuites(config, role.id);
  return (
    <section aria-labelledby="sec-plan" className="flex flex-col gap-2">
      <SectionTitle id="sec-plan" aside="Per task">
        Run plan
      </SectionTitle>
      <p className="text-xs text-muted-foreground">Say which criteria each gate checks. This is a planning view of the gates this role's preset will require, and of the eval suites an agent must have passed. The checks are kept in this organization draft and are not yet sent when a run starts.</p>
      {preset.gates.map((g) => {
        const draft = text(g.id);
        const parts = draft.split(",").map((x) => x.trim()).filter((x) => x !== "");
        const bad = parts.find((x) => fieldRules.slug(x) !== null);
        return (
          <Field key={g.id} id={`plan-${g.id}`} label={`Criteria for gate ${g.id}`} error={bad === undefined ? null : `'${bad}' is not a valid criterion id`}>
            <Input
              id={`plan-${g.id}`}
              className="h-8 font-mono text-xs"
              value={draft}
              placeholder="criterion-a, criterion-b"
              onChange={(e) => {
                const value = e.target.value;
                const list = value.split(",").map((x) => x.trim()).filter((x) => x !== "");
                const valid = list.every((x) => fieldRules.slug(x) === null);
                // The text is kept as a draft unless it is exactly what is stored (so an invalid entry, or a half-typed list, survives).
                if (valid && list.join(", ") === value) drafts.delete(g.id);
                else drafts.set(g.id, value);
                rerender((n) => n + 1);
                store.getState().edit("Set gate criteria", (d) => setRoleChecks(d, role.id, { ...checks, [g.id]: valid ? list : [] }));
              }}
            />
          </Field>
        );
      })}
      {problem !== null ? (
        <p role="status" className="text-xs text-warning">
          {problem}
        </p>
      ) : (
        <ol aria-label="Instantiated gates" className="flex flex-col gap-1 text-xs">
          {gates.map((g) => (
            <li key={g.id} className="font-mono">
              {g.id} ({g.kind}{g.required ? ", required" : ""}): {g.checks.join(", ")}
            </li>
          ))}
        </ol>
      )}
      <h4 className="text-xs font-medium">Required eval suites</h4>
      {suites.length === 0 ? (
        <p className="text-xs text-muted-foreground">None.</p>
      ) : (
        <ul aria-label="Required eval suites" className="font-mono text-xs">
          {suites.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- verification line ------------------------------------------------------------------------------

export function ChecksSection({ agent, role }: { agent: Agent; role: Role | undefined }) {
  const store = useBuilderStore();
  const agents = useBuilder((s) => s.doc.org.agents);
  const names = new Map(agents.map((a) => [a.id, a.name]));
  const checkedBy = agents.filter((a) => a.checks.includes(agent.id));
  const isVerifier = role?.kind === "verifier";
  if (!isVerifier && checkedBy.length === 0) return null;
  const candidates = agents.filter((a) => a.id !== agent.id && !agent.checks.includes(a.id));
  return (
    <section aria-labelledby="sec-checks" className="flex flex-col gap-2">
      <SectionTitle id="sec-checks">{isVerifier ? "Verifies" : "Verified by"}</SectionTitle>
      {isVerifier ? (
        <>
          <p className="text-xs text-muted-foreground">The verification line is separate from who it reports to. A verifier can't report to, or answer to, the work it checks.</p>
          <ul className="flex flex-col gap-1">
            {agent.checks.map((id) => (
              <li key={id} className="flex items-center gap-2 text-sm">
                <span className="min-w-0 flex-1 truncate">{names.get(id) ?? id}</span>
                <button
                  type="button"
                  className={iconButton}
                  aria-label={`Stop verifying ${names.get(id) ?? id}`}
                  onClick={() => {
                    store.getState().edit("Remove verification line", (d) => removeCheck(d, agent.id, id));
                  }}
                >
                  <X aria-hidden="true" className="size-4" />
                </button>
              </li>
            ))}
          </ul>
          <Field id="check-add" label="Verify another agent">
            <NativeSelect
              id="check-add"
              size="sm"
              value=""
              disabled={candidates.length === 0}
              onChange={(e) => {
                const id = e.target.value;
                if (id) store.getState().edit("Add verification line", (d) => addCheck(d, agent.id, id));
              }}
            >
              <NativeSelectOption value="">+ Add…</NativeSelectOption>
              {candidates.map((a) => (
                <NativeSelectOption key={a.id} value={a.id}>
                  {a.name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
        </>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {checkedBy.map((a) => (
            <li key={a.id}>{a.name}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
