import {
  CircleDollarSign,
  CirclePlay,
  LayoutGrid,
  Cpu,
  ListChecks,
  type LucideIcon,
  Network,
  ShieldCheck,
  KeyRound,
  Users,
  TrendingDown,
  ChartColumn,
} from "lucide-react";

import { ruleFor } from "@/auth/roles";

/** One sidebar link. */
export interface NavItem {
  id: string;
  label: string;
  to: string;
  icon: LucideIcon;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/** The sidebar, in the order of SPEC-app-v1 §2. */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "BUILD",
    items: [
      { id: "builder", label: "Org builder", to: "/builder", icon: Network },
      { id: "templates", label: "Templates", to: "/templates", icon: LayoutGrid },
    ],
  },
  {
    label: "OPERATE",
    items: [
      { id: "runs", label: "Runs", to: "/runs", icon: CirclePlay },
      { id: "agents", label: "Agents", to: "/agents", icon: Users },
      { id: "approvals", label: "Approvals", to: "/approvals", icon: ShieldCheck },
      { id: "autonomy", label: "Autonomy", to: "/autonomy", icon: KeyRound },
      { id: "budgets", label: "Budgets and spend", to: "/budgets", icon: CircleDollarSign },
      { id: "models", label: "Models", to: "/models", icon: Cpu },
    ],
  },
  { label: "RECORD", items: [{ id: "ledger", label: "Ledger", to: "/ledger", icon: ListChecks }] },
  {
    label: "REPORTS",
    items: [
      { id: "dashboards", label: "Dashboards", to: "/dashboards/verification", icon: ChartColumn },
      { id: "downgrade", label: "Downgrade report", to: "/reports/downgrade", icon: TrendingDown },
    ],
  },
];

const APPROVER_ITEMS = new Set(["approvals", "runs", "ledger"]);
const BUILD_ITEMS = new Set(["builder", "templates"]);

/**
 * The sidebar for a role (AE-2): a navigation convenience only, never access control (AE-3).
 * An unknown role is a viewer; no signed-in person (`null`) sees everything.
 */
export function navGroupsFor(role: string | null): NavGroup[] {
  const menu = ruleFor(role).menu;
  if (menu === "all") return NAV_GROUPS;
  const keep = (item: NavItem): boolean => (menu === "approver" ? APPROVER_ITEMS.has(item.id) : !BUILD_ITEMS.has(item.id));
  return NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter(keep) })).filter((g) => g.items.length > 0);
}

/** Every page the app has (SPEC-app-v1 §2). `:param` segments are details pages. */
export const PAGES = [
  { path: "/builder", title: "Org builder" },
  { path: "/templates", title: "Templates" },
  { path: "/runs", title: "Runs" },
  { path: "/runs/:id", title: "Run" },
  { path: "/agents", title: "Agents" },
  { path: "/approvals", title: "Approvals" },
  { path: "/approvals/:id", title: "Approval" },
  { path: "/autonomy", title: "Autonomy" },
  { path: "/budgets", title: "Budgets and spend" },
  { path: "/models/:tab", title: "Models" },
  { path: "/ledger", title: "Ledger" },
  { path: "/ledger/:entry", title: "Ledger entry" },
  { path: "/dashboards/:view", title: "Dashboards" },
  { path: "/reports/downgrade", title: "Model downgrade report" },
  { path: "/start", title: "Get started" },
  { path: "/settings", title: "Settings" },
] as const;
