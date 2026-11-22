import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { DESCRIPTION_MAX, fieldRules, INSTRUCTIONS_MAX, ROLE_KINDS, type Role, type RoleKind } from "@/model/org";
import { cn } from "@/lib/utils";

import { addRole, removeRole, roleUsage, updateRole } from "../ops";
import { useBuilder, useBuilderStore } from "../store-context";
import { DraftField, Field, TextField } from "./fields";
import { KindBadge } from "./kind-badge";

const KIND_NAMES: Record<RoleKind, string> = {
  chief_of_staff: "Chief of Staff (top of the organization)",
  worker: "Worker (does tasks)",
  verifier: "Verifier (independently checks work)",
};

/** The organization's roles: create, edit and delete custom roles. */
export function RolesPanel() {
  const store = useBuilderStore();
  const roles = useBuilder((s) => s.doc.org.roles);
  const agents = useBuilder((s) => s.doc.org.agents);
  const activeRoleId = useBuilder((s) => s.activeRoleId);
  const active = roles.find((r) => r.id === activeRoleId);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Roles</h3>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            const s = store.getState();
            const result = addRole(s.doc);
            s.edit("Add role", () => result.doc);
            s.setActiveRole(result.id);
          }}
        >
          <Plus aria-hidden="true" /> New role
        </Button>
      </div>
      {roles.length === 0 ? (
        <p className="text-sm text-muted-foreground">No roles yet. Add one, or drag a predefined role onto the canvas.</p>
      ) : (
        <ul className="flex flex-col gap-1" aria-label="Roles in this organization">
          {roles.map((role) => {
            const used = agents.filter((a) => a.role === role.id).length;
            return (
              <li key={role.id}>
                <button
                  type="button"
                  aria-current={role.id === activeRoleId ? "true" : undefined}
                  onClick={() => {
                    store.getState().setActiveRole(role.id);
                  }}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent",
                    role.id === activeRoleId && "bg-accent",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{role.name}</span>
                  <span className="text-xs text-muted-foreground">{used}</span>
                  <KindBadge kind={role.kind} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {active !== undefined && <RoleForm role={active} />}
    </div>
  );
}

function RoleForm({ role }: { role: Role }) {
  const store = useBuilderStore();
  const roles = useBuilder((s) => s.doc.org.roles);
  const used = useBuilder((s) => roleUsage(s.doc, role.id));
  const editKey = useId();
  const taken = new Set(roles.map((r) => r.id));
  const edit = (label: string, patch: Partial<Omit<Role, "id">>, field?: string): void => {
    store.getState().edit(label, (d) => updateRole(d, role.id, patch), field === undefined ? undefined : `role:${role.id}:${field}`);
  };

  return (
    <section aria-label={`Edit role ${role.name}`} className="flex flex-col gap-4 border-t pt-4">
      <TextField
        id="role-name"
        label="Role name"
        value={role.name}
        error={fieldRules.name(role.name)}
        onChange={(name) => {
          edit("Rename role", { name }, "name");
        }}
      />
      <DraftField
        id="role-id"
        label="Role id"
        value={role.id}
        hint="Agents refer to the role by this id."
        check={(draft) => fieldRules.slug(draft) ?? (taken.has(draft) ? "Another role has this id" : null)}
        onCommit={(id) => {
          store.getState().renameRoleId(role.id, id, `role-id:${editKey}`);
        }}
      />
      <Field id="role-kind" label="Kind">
        <NativeSelect
          id="role-kind"
          className="w-full"
          value={role.kind}
          onChange={(e) => {
            const kind = ROLE_KINDS.find((k) => k === e.target.value);
            if (kind !== undefined) edit("Change role kind", { kind });
          }}
        >
          {ROLE_KINDS.map((kind) => (
            <NativeSelectOption key={kind} value={kind}>
              {KIND_NAMES[kind]}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </Field>
      <TextField
        id="role-description"
        label="Description"
        multiline
        value={role.description}
        error={fieldRules.maxLength(role.description, DESCRIPTION_MAX)}
        onChange={(description) => {
          edit("Edit description", { description }, "description");
        }}
      />
      <TextField
        id="role-instructions"
        label="Instructions"
        multiline
        value={role.instructions}
        error={fieldRules.maxLength(role.instructions, INSTRUCTIONS_MAX)}
        onChange={(instructions) => {
          edit("Edit instructions", { instructions }, "instructions");
        }}
      />
      <ToolsField role={role} onChange={(tools) => { edit("Edit tools", { tools }, "tools"); }} />
      <TextField
        id="role-model"
        label="Default model"
        value={role.default_model ?? ""}
        placeholder="None"
        error={role.default_model === null ? null : fieldRules.model(role.default_model)}
        onChange={(value) => {
          edit("Change default model", { default_model: value === "" ? null : value }, "default_model");
        }}
      />
      <div className="flex flex-col gap-1">
        <Button
          variant="destructive"
          size="sm"
          className="self-start"
          disabled={used > 0}
          aria-describedby={used > 0 ? "role-delete-reason" : undefined}
          onClick={() => {
            const s = store.getState();
            s.edit("Delete role", (d) => removeRole(d, role.id));
            s.setActiveRole(null);
          }}
        >
          <Trash2 aria-hidden="true" /> Delete role
        </Button>
        {used > 0 && (
          <p id="role-delete-reason" className="text-xs text-muted-foreground">
            {used} agent{used === 1 ? " holds" : "s hold"} this role; give them another role first.
          </p>
        )}
      </div>
    </section>
  );
}

function parseTools(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t !== "");
}

/** Comma-separated tool ids; the draft keeps the user's spacing while the document gets the list. */
function ToolsField({ role, onChange }: { role: Role; onChange: (tools: string[]) => void }) {
  const joined = role.tools.join(", ");
  const [draft, setDraft] = useState(joined);
  const [source, setSource] = useState(joined);
  if (source !== joined) {
    setSource(joined);
    if (parseTools(draft).join(", ") !== joined) setDraft(joined);
  }
  const bad = role.tools.filter((t) => fieldRules.slug(t) !== null);
  return (
    <Field
      id="role-tools"
      label="Tools"
      hint="Comma-separated tool ids, for example git, shell."
      error={bad.length > 0 ? `Not valid ids: ${bad.join(", ")}` : null}
    >
      <input
        id="role-tools"
        className="h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive dark:bg-panel-2"
        value={draft}
        spellCheck={false}
        aria-invalid={bad.length > 0 ? true : undefined}
        aria-describedby={bad.length > 0 ? "role-tools-error" : "role-tools-hint"}
        onChange={(e) => {
          setDraft(e.target.value);
          onChange(parseTools(e.target.value));
        }}
      />
    </Field>
  );
}
