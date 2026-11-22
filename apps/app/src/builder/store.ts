/**
 * Builder state (Zustand). Every change to the document goes through `edit`, which records it for
 * undo/redo and re-runs validation; selection, layout results and panels are view state and are
 * not part of history. Components subscribe to narrow slices, so a drag doesn't re-render panels.
 */
import { createStore, type StoreApi } from "zustand/vanilla";

import { approversOf } from "@/model/config";
import type { Role } from "@/model/org";

import { copyFragment, type Fragment, PASTE_OFFSET, pasteFragment } from "./clipboard";
import { addCheck, addGate, addPerson, removeCheck, removePerson, setApprovers, setRoleEvals, stripPeople } from "./config-ops";
import { blankDocument, type BuilderDocument, type XY } from "./document";
import { edgeRef } from "./edge-ids";
import { emptyHistory, type History, record, redo as redoHistory, undo as undoHistory } from "./history";
import { computeIssues, type IssueReport } from "./issues";
import type { LayoutEngine } from "./layout-engine";
import { structureKey, toElkGraph } from "./layout";
import {
  addAgent,
  addRole,
  clearPositions,
  removeAgents,
  removeReportingLines,
  renameAgent,
  renameRole,
  setManager,
  setPositions,
  wouldCreateCycle,
} from "./ops";
import { uniqueId } from "./ids";
import { loadUserTemplates, saveDraft, saveUserTemplates } from "./storage";
import { PREDEFINED_ROLES, type Template } from "./templates";

export interface Selection {
  nodes: ReadonlySet<string>;
  /** Selected reporting lines, by the id of the reporting agent (each agent has one line up). */
  edges: ReadonlySet<string>;
}

export type PanelTab = "inspector" | "roles" | "issues";

/** The checks the palette offers (SPEC-app-v1 3.1). */
export type CheckItem = "rule" | "test_command" | "verifier" | "eval";

export { edgeId, reportOfEdge } from "./edge-ids";

const EMPTY: ReadonlySet<string> = new Set();

export interface BuilderState {
  doc: BuilderDocument;
  history: History;
  issues: IssueReport;
  selection: Selection;
  /** Positions from automatic layout (derived; never saved). */
  computed: Record<string, XY>;
  layoutStatus: "idle" | "running" | "error";
  /** Positions of nodes being dragged right now (committed to the document on drop). */
  dragging: Record<string, XY> | null;
  clipboard: { fragment: Fragment; pastes: number } | null;
  /** The canvas centres and focuses this agent when `seq` changes. */
  focusRequest: { id: string; seq: number } | null;
  /** The canvas fits the view when this changes. */
  fitRequest: number;
  panel: PanelTab;
  activeRoleId: string | null;
  /** Polite screen-reader announcement. */
  announcement: { text: string; seq: number };
  userTemplates: Template[];
  /** Whose saved drafts and templates these are (organization and person), or undefined with no sign-in. */
  storageScope: string | undefined;

  edit: (label: string, fn: (doc: BuilderDocument) => BuilderDocument, mergeKey?: string) => void;
  undo: () => void;
  redo: () => void;
  load: (label: string, doc: BuilderDocument) => void;
  addAgentFromRole: (role: Role, position?: XY) => string;
  /** Renames an agent and its references; the selection follows it. */
  renameAgentId: (from: string, to: string, mergeKey: string) => void;
  /** Renames a role and its references; the roles panel follows it. */
  renameRoleId: (from: string, to: string, mergeKey: string) => void;
  connect: (managerId: string, reportId: string) => boolean;
  /** Adds a verification line: `verifierId` checks `checkedId`. */
  connectCheck: (verifierId: string, checkedId: string) => boolean;
  /** Makes a person an approver for the role of `agentId`. */
  connectApproval: (personId: string, agentId: string) => boolean;
  /**
   * Adds a check to an agent's role (dropped onto it, or the selected one): a rule check or test
   * command gate, an independent verifier (an agent that checks it, plus a verifier gate), or an
   * eval requirement. Returns false, with an announcement, if no agent was named.
   */
  addCheckItem: (item: CheckItem, agentId?: string) => boolean;
  /** Adds a new custom worker role and an agent holding it (like adding any role). */
  addCustomRoleAgent: () => string;
  /** Adds a human approver to the canvas; returns its opaque id. */
  addPersonNode: (position?: XY) => string;
  reconnect: (oldReportId: string, managerId: string, reportId: string) => boolean;
  /** Removes agents and people, and lines by selection key (a reporting line is its report's id). */
  deleteElements: (nodeIds: Iterable<string>, edgeKeys: Iterable<string>) => void;
  deleteSelection: () => void;
  setSelection: (nodes: Iterable<string>, edges?: Iterable<string>) => void;
  selectAll: () => void;
  dragMove: (positions: Record<string, XY>) => void;
  dragEnd: (positions: Record<string, XY>) => void;
  nudge: (positions: Record<string, XY>) => void;
  copy: () => boolean;
  cut: () => boolean;
  paste: () => boolean;
  duplicate: () => boolean;
  autoLayout: () => void;
  setComputed: (positions: Record<string, XY>) => void;
  setLayoutStatus: (status: BuilderState["layoutStatus"]) => void;
  focusAgent: (id: string) => void;
  focusRole: (id: string) => void;
  setPanel: (panel: PanelTab) => void;
  setActiveRole: (id: string | null) => void;
  announce: (text: string) => void;
  saveTemplate: (name: string, description: string) => boolean;
  deleteTemplate: (id: string) => boolean;
}

export type BuilderStore = StoreApi<BuilderState>;

/** Whether a selected line still exists (reporting, verification or approval). */
function lineExists(key: string, doc: BuilderDocument, withLine: ReadonlySet<string>): boolean {
  const ref = edgeRef(key);
  if (ref.kind === "reporting") return withLine.has(ref.report);
  if (ref.kind === "check") return doc.org.agents.some((a) => a.id === ref.verifier && a.checks.includes(ref.checked));
  const agent = doc.org.agents.find((a) => a.id === ref.agent);
  return agent !== undefined && approversOf(doc.config, agent.role).includes(ref.person);
}

function prune(selection: Selection, doc: BuilderDocument): Selection {
  if (selection.nodes.size === 0 && selection.edges.size === 0) return selection;
  const ids = new Set([...doc.org.agents.map((a) => a.id), ...doc.ui.people.map((p) => p.id)]);
  const withLine = new Set(doc.org.agents.filter((a) => a.reports_to !== null).map((a) => a.id));
  const nodes = [...selection.nodes].filter((id) => ids.has(id));
  const edges = [...selection.edges].filter((id) => lineExists(id, doc, withLine));
  if (nodes.length === selection.nodes.size && edges.length === selection.edges.size) return selection;
  return { nodes: new Set(nodes), edges: new Set(edges) };
}

export function createBuilderStore(initial: BuilderDocument = blankDocument(), storageScope?: string): BuilderStore {
  return createStore<BuilderState>()((set, get) => {
    /** Swaps in a new document: re-validates only if the org changed, prunes the selection. */
    const apply = (doc: BuilderDocument, history: History, extra: Partial<BuilderState> = {}): void => {
      const state = get();
      set({
        doc,
        history,
        issues: doc.org === state.doc.org && doc.config === state.doc.config ? state.issues : computeIssues(doc.org, doc.config),
        ...extra,
        selection: prune(extra.selection ?? state.selection, doc),
      });
    };

    const announce = (text: string): void => {
      set((s) => ({ announcement: { text, seq: s.announcement.seq + 1 } }));
    };

    const commit = (label: string, next: BuilderDocument, mergeKey?: string, extra?: Partial<BuilderState>): boolean => {
      const { doc, history } = get();
      if (next === doc) return false;
      apply(next, record(history, doc, next, label, mergeKey ?? null), extra);
      return true;
    };

    const pasteNow = (fragment: Fragment, pastes: number, label: string): boolean => {
      const result = pasteFragment(get().doc, fragment, PASTE_OFFSET * pastes);
      const changed = commit(label, result.doc, undefined, { selection: { nodes: new Set(result.ids), edges: EMPTY } });
      if (changed) announce(`${label}: ${String(result.ids.length)} agent${result.ids.length === 1 ? "" : "s"}`);
      return changed;
    };

    return {
      doc: initial,
      history: emptyHistory(),
      issues: computeIssues(initial.org, initial.config),
      selection: { nodes: EMPTY, edges: EMPTY },
      computed: {},
      layoutStatus: "idle",
      dragging: null,
      clipboard: null,
      focusRequest: null,
      fitRequest: 0,
      panel: "inspector",
      activeRoleId: null,
      announcement: { text: "", seq: 0 },
      userTemplates: loadUserTemplates(storageScope),
      storageScope,

      edit: (label, fn, mergeKey) => {
        commit(label, fn(get().doc), mergeKey);
      },

      undo: () => {
        const step = undoHistory(get().history);
        if (step === null) return;
        apply(step.doc, step.history);
        announce(`Undid ${step.label}`);
      },

      redo: () => {
        const step = redoHistory(get().history);
        if (step === null) return;
        apply(step.doc, step.history);
        announce(`Redid ${step.label}`);
      },

      load: (label, doc) => {
        const changed = commit(label, doc, undefined, {
          selection: { nodes: EMPTY, edges: EMPTY },
          activeRoleId: null,
          fitRequest: get().fitRequest + 1,
        });
        if (changed) announce(`${label}. Undo to go back.`);
      },

      addAgentFromRole: (role, position) => {
        const selected = [...get().selection.nodes];
        const { agents, roles } = get().doc.org;
        const kindOf = (agentId: string): string | undefined =>
          roles.find((r) => r.id === agents.find((a) => a.id === agentId)?.role)?.kind;
        // The org has one top agent (the Chief of Staff). A verifier is independent of the work it
        // checks, so it never joins the selected agent's team (it reports to the top agent, as in
        // the built-in templates), and nobody is added under a verifier either.
        const top = agents.find((a) => a.reports_to === null)?.id;
        const pick = selected.length === 1 ? selected[0] : undefined;
        const underVerifier = pick !== undefined && kindOf(pick) === "verifier";
        const manager = role.kind === "verifier" || underVerifier ? top : pick;
        const result = addAgent(get().doc, { role, reportsTo: manager ?? null, ...(position ? { position } : {}) });
        commit(`Add ${role.name}`, result.doc, undefined, {
          selection: { nodes: new Set([result.id]), edges: EMPTY },
          panel: "inspector",
        });
        announce(`Added ${role.name}${manager ? ` reporting to ${manager}` : ""}`);
        return result.id;
      },

      renameAgentId: (from, to, mergeKey) => {
        const { selection } = get();
        const nodes = new Set([...selection.nodes].map((id) => (id === from ? to : id)));
        const edges = new Set([...selection.edges].map((id) => (id === from ? to : id)));
        commit("Change agent id", renameAgent(get().doc, from, to), mergeKey, { selection: { nodes, edges } });
      },

      renameRoleId: (from, to, mergeKey) => {
        const active = get().activeRoleId;
        commit("Change role id", renameRole(get().doc, from, to), mergeKey, {
          activeRoleId: active === from ? to : active,
        });
      },

      connect: (managerId, reportId) => {
        const { doc } = get();
        if (wouldCreateCycle(doc, managerId, reportId)) {
          announce("That reporting line would make a loop");
          return false;
        }
        const changed = commit("Set manager", setManager(doc, reportId, managerId));
        if (changed) announce(`${reportId} now reports to ${managerId}`);
        return changed;
      },

      connectCheck: (verifierId, checkedId) => {
        if (verifierId === checkedId) {
          announce("An agent can't verify itself");
          return false;
        }
        const changed = commit("Add verification line", addCheck(get().doc, verifierId, checkedId));
        if (changed) announce(`${verifierId} now verifies ${checkedId}`);
        return changed;
      },

      connectApproval: (personId, agentId) => {
        const { doc } = get();
        const role = doc.org.agents.find((a) => a.id === agentId)?.role;
        if (role === undefined) return false;
        const current = approversOf(doc.config, role);
        if (current.includes(personId)) return false;
        const changed = commit("Add approver", setApprovers(doc, role, [...current, personId]));
        if (changed) announce(`${personId} now approves for the ${role} role`);
        return changed;
      },

      addCheckItem: (item, agentId) => {
        const { doc, selection } = get();
        const picked = agentId ?? (selection.nodes.size === 1 ? [...selection.nodes][0] : undefined);
        const agent = doc.org.agents.find((a) => a.id === picked);
        const role = doc.org.roles.find((r) => r.id === agent?.role);
        if (agent === undefined || role === undefined) {
          announce("Select an agent first, or drop the check onto one");
          return false;
        }
        let next = doc;
        let label: string;
        if (item === "rule" || item === "test_command") {
          next = addGate(doc, role.id, item, role.name);
          label = `Add ${item === "rule" ? "rule check" : "test command"} to ${role.name}`;
        } else if (item === "eval") {
          const existing = doc.config.evals.find((e) => e.role === role.id)?.suites ?? [];
          const suite = uniqueId(`${role.id}-basics`, new Set(existing));
          next = setRoleEvals(doc, role.id, [...existing, suite]);
          label = `Require eval ${suite} for ${role.name}`;
        } else {
          let verifier = doc.org.agents.find((a) => a.id !== agent.id && doc.org.roles.find((r) => r.id === a.role)?.kind === "verifier");
          if (verifier === undefined) {
            const base = PREDEFINED_ROLES.find((r) => r.kind === "verifier");
            if (base === undefined) return false;
            const top = doc.org.agents.find((a) => a.reports_to === null)?.id ?? null;
            const added = addAgent(doc, { role: base, reportsTo: top });
            next = added.doc;
            verifier = next.org.agents.find((a) => a.id === added.id);
          }
          if (verifier === undefined) return false;
          next = addCheck(next, verifier.id, agent.id);
          next = addGate(next, role.id, "verifier", role.name);
          label = `Add independent verifier ${verifier.name} for ${agent.name}`;
        }
        const changed = commit(label, next);
        if (changed) announce(label);
        return changed;
      },

      addCustomRoleAgent: () => {
        const created = addRole(get().doc, "Custom role");
        const role = created.doc.org.roles.find((r) => r.id === created.id);
        if (role === undefined) return "";
        return get().addAgentFromRole(role);
      },

      addPersonNode: (position) => {
        const result = addPerson(get().doc);
        const doc = position ? setPositions(result.doc, { [result.id]: position }) : result.doc;
        commit("Add human approver", doc, undefined, { selection: { nodes: new Set([result.id]), edges: EMPTY }, panel: "inspector" });
        announce("Added a human approver");
        return result.id;
      },

      reconnect: (oldReportId, managerId, reportId) => {
        let next = get().doc;
        if (oldReportId !== reportId) next = removeReportingLines(next, new Set([oldReportId]));
        if (wouldCreateCycle(next, managerId, reportId)) {
          announce("That reporting line would make a loop");
          return false;
        }
        return commit("Move reporting line", setManager(next, reportId, managerId));
      },

      deleteElements: (nodeIds, edgeKeys) => {
        const all = new Set(nodeIds);
        const people = [...all].filter((id) => get().doc.ui.people.some((p) => p.id === id));
        const nodes = new Set([...all].filter((id) => !people.includes(id)));
        let next = removeAgents(get().doc, nodes);
        for (const id of people) {
          const rest = Object.fromEntries(Object.entries(next.layout.positions).filter(([key]) => key !== id));
          next = removePerson({ ...next, layout: { positions: rest } }, id);
        }
        const reporting = new Set<string>();
        for (const key of new Set(edgeKeys)) {
          const ref = edgeRef(key);
          if (ref.kind === "reporting") {
            if (!nodes.has(ref.report)) reporting.add(ref.report);
          } else if (ref.kind === "check") {
            next = removeCheck(next, ref.verifier, ref.checked);
          } else {
            const role = next.org.agents.find((a) => a.id === ref.agent)?.role;
            if (role !== undefined) next = setApprovers(next, role, approversOf(next.config, role).filter((id) => id !== ref.person));
          }
        }
        next = removeReportingLines(next, reporting);
        const label =
          nodes.size + people.length > 0
            ? `Delete ${String(nodes.size + people.length)} ${nodes.size > 0 ? "agent" : "approver"}${nodes.size + people.length === 1 ? "" : "s"}`
            : "Delete line";
        if (commit(label, next, undefined, { selection: { nodes: EMPTY, edges: EMPTY } })) announce(label);
      },

      deleteSelection: () => {
        const { nodes, edges } = get().selection;
        get().deleteElements(nodes, edges);
      },

      setSelection: (nodes, edges = []) => {
        const next = { nodes: new Set(nodes), edges: new Set(edges) };
        const current = get().selection;
        const same = (a: ReadonlySet<string>, b: ReadonlySet<string>) => a.size === b.size && [...a].every((id) => b.has(id));
        if (same(next.nodes, current.nodes) && same(next.edges, current.edges)) return;
        set({ selection: next });
      },

      selectAll: () => {
        const { doc } = get();
        set({
          selection: {
            nodes: new Set(doc.org.agents.map((a) => a.id)),
            edges: new Set(doc.org.agents.filter((a) => a.reports_to !== null).map((a) => a.id)),
          },
        });
      },

      dragMove: (positions) => {
        set((s) => ({ dragging: { ...(s.dragging ?? {}), ...positions } }));
      },

      dragEnd: (positions) => {
        const all = { ...(get().dragging ?? {}), ...positions };
        set({ dragging: null });
        const ids = Object.keys(all).sort();
        if (ids.length > 0) {
          commit(ids.length === 1 ? "Move agent" : `Move ${String(ids.length)} agents`, setPositions(get().doc, all), `move:${ids.join(",")}`);
        }
      },

      nudge: (positions) => {
        const ids = Object.keys(positions).sort().join(",");
        commit("Move agents", setPositions(get().doc, positions), `nudge:${ids}`);
      },

      copy: () => {
        const fragment = copyFragment(get().doc, get().selection.nodes);
        if (fragment === null) return false;
        set({ clipboard: { fragment, pastes: 0 } });
        announce(`Copied ${String(fragment.agents.length)} agent${fragment.agents.length === 1 ? "" : "s"}`);
        return true;
      },

      cut: () => {
        if (!get().copy()) return false;
        get().deleteElements(get().selection.nodes, []);
        return true;
      },

      paste: () => {
        const clip = get().clipboard;
        if (clip === null) return false;
        const pastes = clip.pastes + 1;
        set({ clipboard: { ...clip, pastes } });
        return pasteNow(clip.fragment, pastes, "Paste");
      },

      duplicate: () => {
        const fragment = copyFragment(get().doc, get().selection.nodes);
        return fragment === null ? false : pasteNow(fragment, 1, "Duplicate");
      },

      autoLayout: () => {
        const changed = commit("Auto-layout", clearPositions(get().doc));
        set((s) => ({ fitRequest: s.fitRequest + 1 }));
        announce(changed ? "Arranged automatically" : "Already arranged automatically; fitted to view");
      },

      setComputed: (positions) => {
        set({ computed: positions, layoutStatus: "idle" });
      },

      setLayoutStatus: (layoutStatus) => {
        set({ layoutStatus });
      },

      focusAgent: (id) => {
        set((s) => ({
          selection: { nodes: new Set([id]), edges: EMPTY },
          focusRequest: { id, seq: (s.focusRequest?.seq ?? 0) + 1 },
          panel: s.panel === "issues" ? "issues" : "inspector",
        }));
      },

      focusRole: (id) => {
        set({ panel: "roles", activeRoleId: id });
      },

      setPanel: (panel) => {
        set({ panel });
      },

      setActiveRole: (activeRoleId) => {
        set({ activeRoleId });
      },

      announce,

      saveTemplate: (name, description) => {
        const templates = get().userTemplates;
        const taken = new Set(templates.map((t) => t.id));
        let id = `user-${String(Date.now())}`;
        while (taken.has(id)) id = `${id}-1`;
        const next = [...templates, { id, name, description, document: stripPeople(get().doc) }];
        if (!saveUserTemplates(next, storageScope)) {
          announce("Couldn't save the template: browser storage is unavailable or full");
          return false;
        }
        set({ userTemplates: next });
        announce(`Saved template ${name}`);
        return true;
      },

      deleteTemplate: (id) => {
        const next = get().userTemplates.filter((t) => t.id !== id);
        if (!saveUserTemplates(next, storageScope)) {
          announce("Couldn't update saved templates: browser storage is unavailable");
          return false;
        }
        set({ userTemplates: next });
        announce("Deleted template");
        return true;
      },
    };
  });
}

/**
 * Keeps automatic layout up to date: whenever agents or reporting lines change, ELK runs again
 * (off the main thread); stale results are dropped. Returns a function that stops it.
 */
export function connectLayout(store: BuilderStore, engine: LayoutEngine): () => void {
  let latest = 0;
  let lastKey: string | null = null;
  const run = (doc: BuilderDocument): void => {
    const key = structureKey(doc.org);
    if (key === lastKey) return;
    lastKey = key;
    const request = ++latest;
    store.getState().setLayoutStatus("running");
    engine
      .layout(toElkGraph(doc.org))
      .then((positions) => {
        if (request === latest) store.getState().setComputed(positions);
      })
      .catch(() => {
        if (request !== latest) return;
        store.getState().setLayoutStatus("error");
        store.getState().announce("Automatic layout failed; agents keep their current places");
      });
  };
  run(store.getState().doc);
  const unsubscribe = store.subscribe((state, previous) => {
    if (state.doc.org !== previous.doc.org) run(state.doc);
  });
  return () => {
    latest += 1;
    unsubscribe();
  };
}

/** Saves the current document to browser storage shortly after each edit. Returns a stop function. */
export function connectDraftAutosave(store: BuilderStore, delayMs = 300): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const unsubscribe = store.subscribe((state, previous) => {
    if (state.doc === previous.doc) return;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      saveDraft(store.getState().doc, store.getState().storageScope);
    }, delayMs);
  });
  return () => {
    if (timer !== null) {
      clearTimeout(timer);
      saveDraft(store.getState().doc, store.getState().storageScope);
    }
    unsubscribe();
  };
}
