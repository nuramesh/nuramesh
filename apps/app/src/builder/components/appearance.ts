import type { Appearance } from "@/model/config";
import type { RoleKind } from "@/model/org";

/** Classes for an agent's avatar tile: a colour chosen in the inspector, else the role colour. */
const BY_ACCENT: Readonly<Record<string, string>> = {
  indigo: "bg-brand/15 text-brand",
  sky: "bg-link-soft text-link",
  amber: "bg-warm-soft text-warm",
  rose: "bg-danger-soft text-danger",
  slate: "bg-secondary text-secondary-foreground",
};

/** Role colours (docs/design/DESIGN-TOKENS.md): indigo = Chief of Staff, blue = Worker, amber = Verifier. */
const BY_KIND: Readonly<Record<RoleKind, string>> = {
  chief_of_staff: "bg-role-chief-bg text-role-chief-text",
  worker: "bg-role-worker-bg text-role-worker-text",
  verifier: "bg-role-verifier-bg text-role-verifier-text",
};

/** The 3px accent bar at the left of an agent card, in the role colour (unknown role: Worker). */
const BAR_BY_KIND: Readonly<Record<RoleKind, string>> = {
  chief_of_staff: "border-l-[3px] border-l-role-chief-dot",
  worker: "border-l-[3px] border-l-role-worker-dot",
  verifier: "border-l-[3px] border-l-role-verifier-dot",
};

export function accentBarClass(kind: RoleKind | null): string {
  return BAR_BY_KIND[kind ?? "worker"];
}

export function avatarClass(appearance: Appearance, kind: RoleKind | null): string {
  const chosen = appearance.color === undefined ? undefined : BY_ACCENT[appearance.color];
  return chosen ?? BY_KIND[kind ?? "worker"];
}

/** Up to three characters for the avatar: the chosen label, or the initials of the name. */
export function avatarText(appearance: Appearance, name: string): string {
  if (appearance.avatar !== undefined && appearance.avatar !== "") return appearance.avatar;
  const words = name.trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words.slice(0, 2).map((w) => w[0] ?? "").join("") : (words[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}
