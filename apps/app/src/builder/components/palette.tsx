import { PanelLeftClose, PanelLeftOpen, Plus, ShieldCheck, UserRound, Users, type LucideIcon } from "lucide-react";
import { memo, useCallback, useMemo } from "react";

import type { Role } from "@/model/org";

import { useBuilder, useBuilderStore } from "../store-context";
import { extraRoles } from "../templates";
import type { CheckItem } from "../store";
import { ITEM_MIME, ROLE_MIME } from "./canvas";
import { KindBadge } from "./kind-badge";

const PaletteItem = memo(function PaletteItem({ role, onAdd }: { role: Role; onAdd: (role: Role) => void }) {
  return (
    <li>
      <button
        type="button"
        draggable
        data-role-id={role.id}
        onDragStart={(event) => {
          event.dataTransfer.setData(ROLE_MIME, role.id);
          event.dataTransfer.effectAllowed = "copy";
        }}
        onClick={() => {
          onAdd(role);
        }}
        className="flex w-full cursor-grab items-center gap-2 rounded-md border border-card-border bg-card px-2.5 py-2 text-left text-sm hover:bg-accent active:cursor-grabbing"
      >
        <Plus aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 break-words">
          <span className="sr-only">Add </span>
          {role.name}
        </span>
        <KindBadge kind={role.kind} />
      </button>
    </li>
  );
});

const CHECKS: { item: CheckItem; name: string; desc: string }[] = [
  { item: "rule", name: "Rule check", desc: "A fast, deterministic rule" },
  { item: "test_command", name: "Test command", desc: "Runs tests in the sandbox" },
  { item: "verifier", name: "Independent verifier", desc: "A different model checks the work" },
  { item: "eval", name: "Eval", desc: "Must pass before taking work" },
];

function ItemButton({
  payload,
  name,
  desc,
  icon,
  onAdd,
}: {
  payload: string;
  name: string;
  desc: string;
  icon: "check" | "person";
  onAdd: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        draggable
        data-palette-item={payload}
        onDragStart={(event) => {
          event.dataTransfer.setData(ITEM_MIME, payload);
          event.dataTransfer.effectAllowed = "copy";
        }}
        onClick={onAdd}
        className="flex min-h-11 w-full cursor-grab items-center gap-2.5 rounded-md border bg-card px-2.5 py-1.5 text-left hover:bg-accent active:cursor-grabbing"
      >
        {icon === "check" ? (
          <ShieldCheck aria-hidden="true" className="size-4 shrink-0 text-link" />
        ) : (
          <UserRound aria-hidden="true" className="size-4 shrink-0 text-warm" />
        )}
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">
            <span className="sr-only">Add </span>
            {name}
          </span>
          <span className="truncate text-xs text-muted-foreground">{desc}</span>
        </span>
      </button>
    </li>
  );
}

/**
 * What to add (SPEC-app-v1 3.1): **Agents** (the organization's roles, predefined roles and a
 * custom one), **Checks** (rule check, test command, independent verifier, eval) and **People**
 * (human approver). Drag onto the canvas, or press to add: an agent reports to the selected agent
 * (a verifier, or any agent added while a verifier is selected, reports to the top agent instead);
 * a check goes to the agent it is dropped on, or the selected one; a person appears on the canvas.
 */
export function Palette({ collapsed = false, onExpand, onCollapse }: { collapsed?: boolean; onExpand?: () => void; onCollapse?: () => void }) {
  const store = useBuilderStore();
  const roles = useBuilder((s) => s.doc.org.roles);
  const extra = useMemo(() => extraRoles(roles), [roles]);
  const onAdd = useCallback(
    (role: Role): void => {
      store.getState().addAgentFromRole(role);
    },
    [store],
  );

  if (collapsed) return <PaletteRail onExpand={onExpand} />;

  return (
    <aside aria-labelledby="palette-title" className="flex min-h-0 flex-col gap-4 overflow-y-auto border-l bg-pane p-3 xl:border-r xl:border-l-0">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 id="palette-title" className="text-sm font-semibold">
            Add to the organization
          </h2>
          <p className="text-xs text-muted-foreground">Drag onto the canvas, or press to add.</p>
        </div>
        {onCollapse !== undefined && (
          <button
            type="button"
            aria-label="Collapse the palette to a rail"
            title="Collapse the palette"
            onClick={onCollapse}
            className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <PanelLeftClose aria-hidden="true" className="size-4" />
          </button>
        )}
      </div>
      <section aria-labelledby="palette-agents" className="flex flex-col gap-1.5">
        <h3 id="palette-agents" className="font-sans text-[11px] font-semibold tracking-[0.06em] text-small uppercase">
          Agents
        </h3>
        <ul className="flex flex-col gap-1.5">
          {roles.map((role) => (
            <PaletteItem key={role.id} role={role} onAdd={onAdd} />
          ))}
          {extra.map((role) => (
            <PaletteItem key={role.id} role={role} onAdd={onAdd} />
          ))}
          <li>
            <button
              type="button"
              onClick={() => {
                store.getState().addCustomRoleAgent();
              }}
              className="flex min-h-11 w-full items-center gap-2 rounded-md border border-dashed border-border-strong px-2.5 py-1.5 text-left text-sm text-muted-foreground hover:bg-accent"
            >
              <Plus aria-hidden="true" className="size-3.5 shrink-0" />
              Custom role
            </button>
          </li>
        </ul>
      </section>
      <section aria-labelledby="palette-checks" className="flex flex-col gap-1.5">
        <h3 id="palette-checks" className="font-sans text-[11px] font-semibold tracking-[0.06em] text-small uppercase">
          Checks
        </h3>
        <ul className="flex flex-col gap-1.5">
          {CHECKS.map((c) => (
            <ItemButton
              key={c.item}
              payload={`check:${c.item}`}
              name={c.name}
              desc={c.desc}
              icon="check"
              onAdd={() => {
                store.getState().addCheckItem(c.item);
              }}
            />
          ))}
        </ul>
      </section>
      <section aria-labelledby="palette-people" className="flex flex-col gap-1.5">
        <h3 id="palette-people" className="font-sans text-[11px] font-semibold tracking-[0.06em] text-small uppercase">
          People
        </h3>
        <ul className="flex flex-col gap-1.5">
          <ItemButton
            payload="person"
            name="Human approver"
            desc="Answers approval requests"
            icon="person"
            onAdd={() => {
              store.getState().addPersonNode();
            }}
          />
        </ul>
      </section>
    </aside>
  );
}

/** A rail button: an icon with a label that shows on hover and on keyboard focus (and is its accessible name). */
function RailButton({ icon: Icon, label, onClick }: { icon: LucideIcon; label: string; onClick?: () => void }) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      className="group relative flex size-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      <Icon aria-hidden="true" className="size-5" />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-full z-40 ml-2 -translate-y-1/2 rounded-md border bg-popover px-2 py-1 text-xs font-medium whitespace-nowrap text-popover-foreground opacity-0 shadow-md group-hover:opacity-100 group-focus-visible:opacity-100"
      >
        {label}
      </span>
    </button>
  );
}

/** The palette collapsed to a narrow icon rail: one icon per group, each expands the full palette. */
function PaletteRail({ onExpand }: { onExpand?: () => void }) {
  return (
    <aside aria-label="Add to the organization" className="flex h-full w-12 flex-col items-center gap-1 border-r bg-pane py-2">
      <RailButton icon={PanelLeftOpen} label="Expand the palette" onClick={onExpand} />
      <div className="my-1 h-px w-6 bg-border" aria-hidden="true" />
      <RailButton icon={Users} label="Agents: expand the palette" onClick={onExpand} />
      <RailButton icon={ShieldCheck} label="Checks: expand the palette" onClick={onExpand} />
      <RailButton icon={UserRound} label="People: expand the palette" onClick={onExpand} />
    </aside>
  );
}
