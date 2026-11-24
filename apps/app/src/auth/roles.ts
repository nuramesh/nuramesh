/**
 * Where a person lands and which menu items they see, by role name (spec approver-experience AE-1, AE-2).
 * This is navigation only, never access control (AE-3): the server's role check is the only gate, and no
 * screen decides what a person may do from this table (use `useCanAct`).
 */
export type MenuKind = "all" | "approver" | "viewer";

export interface RoleRule {
  landing: string;
  menu: MenuKind;
}

export const ROLE_RULES: Record<string, RoleRule> = {
  owner: { landing: "/builder", menu: "all" },
  admin: { landing: "/builder", menu: "all" },
  builder: { landing: "/builder", menu: "all" },
  approver: { landing: "/approvals", menu: "approver" },
  viewer: { landing: "/runs", menu: "viewer" },
};

const VIEWER: RoleRule = { landing: "/runs", menu: "viewer" };

/** An unknown role value is a viewer (the role notes); no signed-in person at all keeps today's `/builder`. */
export function ruleFor(role: string | null): RoleRule {
  if (role === null) return { landing: "/builder", menu: "all" };
  return Object.hasOwn(ROLE_RULES, role) ? (ROLE_RULES[role] ?? VIEWER) : VIEWER;
}

export function landingFor(role: string | null): string {
  return ruleFor(role).landing;
}
