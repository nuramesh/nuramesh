import { cn } from "@/lib/utils";
import type { RoleKind } from "@/model/org";

// Role colours (the design tokens notes, "Role colours"): indigo = Chief of Staff,
// blue = Worker, amber = Verifier, in both themes. The text always says the role as well.
const STYLE: Record<RoleKind, string> = {
  chief_of_staff: "bg-role-chief-bg text-role-chief-text",
  worker: "bg-role-worker-bg text-role-worker-text",
  verifier: "bg-role-verifier-bg text-role-verifier-text",
};

const DOT: Record<RoleKind, string> = {
  chief_of_staff: "bg-role-chief-dot",
  worker: "bg-role-worker-dot",
  verifier: "bg-role-verifier-dot",
};

const LABEL: Record<RoleKind, string> = {
  chief_of_staff: "Chief of Staff",
  worker: "Worker",
  verifier: "Verifier",
};

/** Small label saying what a role is for. */
export function KindBadge({ kind, className }: { kind: RoleKind; className?: string }) {
  return (
    <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[11px] leading-none font-medium", STYLE[kind], className)}>
      {LABEL[kind]}
    </span>
  );
}

/** The role's colour as a dot (decorative: the badge and the role name say it in words). */
export function RoleDot({ kind, className }: { kind: RoleKind; className?: string }) {
  return <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", DOT[kind], className)} data-role-dot={kind} />;
}
