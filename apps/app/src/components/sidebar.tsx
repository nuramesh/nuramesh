import { useQuery } from "@tanstack/react-query";
import { Flag, PanelLeftClose, PanelLeftOpen, Settings } from "lucide-react";
import { Link, matchPath, NavLink, useLocation } from "react-router";

import { useApi } from "@/api/context";
import { useSidebarFacts } from "@/api/hooks";
import { formatMoney, percentUsed } from "@/api/money";
import { SignOutButton } from "@/auth/sign-out-button";
import { useAuthState } from "@/auth/use-session";
import { ThemeToggle } from "@/components/theme-toggle";
import { COPY } from "@/first-run/copy";
import { useFirstRun } from "@/first-run/use-first-run";
import { Tip } from "@/components/ui/tooltip";
import { navGroupsFor } from "@/app/nav";
import { landingFor } from "@/auth/roles";
import { cn } from "@/lib/utils";
import { Logo, LogoTile } from "@brand/Logo";

const SECTION_LABEL = "px-2.5 pt-3.5 pb-1.5 font-sans text-[11px] font-semibold tracking-[0.06em] text-small";

/**
 * The left sidebar (design/Sidebar.dc.html): logo, workspace, navigation, user block. The live-run
 * count, the approvals badge and the spend card need data; they arrive with the API client (the
 * shell shows none rather than inventing numbers).
 */
export function Sidebar({
  onNavigate,
  rail = false,
  onToggle,
  shortcut,
}: {
  onNavigate?: () => void;
  /** Icons only (the collapsed desktop sidebar). */
  rail?: boolean;
  onToggle?: () => void;
  /** The keyboard shortcut shown in the toggle's tooltip, for example "Ctrl+B". */
  shortcut?: string;
}) {
  const { pathname } = useLocation();
  // className stays a plain string: the tooltip trigger merges classes and cannot take a function.
  const isActiveRoute = (to: string): boolean => matchPath({ path: to, end: false }, pathname) !== null;
  const facts = useSidebarFacts();
  const auth = useAuthState();
  const role = auth.kind === "signed-in" ? auth.user.role : null;
  const firstRun = useFirstRun();
  // Who is signed in, where the source has sign-in; otherwise the local-session placeholder.
  const person = auth.kind === "signed-in" ? (auth.user.displayName || auth.user.email || "Signed in") : "You";
  const personNote = auth.kind === "signed-in" ? auth.user.role : "Local session";
  const personTag = auth.kind === "signed-in" ? person.trim().slice(0, 2).toUpperCase() : "You";
  const api = useApi();
  const org = useQuery({ queryKey: ["source-info"], queryFn: () => api.info(), retry: false, refetchInterval: 30_000 });
  const orgName = org.data?.name ?? (org.isPending ? "Loading…" : "Not connected");
  const orgNote = org.data
    ? `${facts.source === "fixtures" ? "Sample data" : "Server"} · ${org.data.detail}`
    : org.isPending
      ? ""
      : "Can't reach the server";
  const words = orgName.split(/\s+/).filter((w) => w !== "");
  const initials = (words.length > 1 ? words.map((w) => w.charAt(0)).join("") : orgName.slice(0, 2))
    .slice(0, 2)
    .toUpperCase();
  const ToggleIcon = rail ? PanelLeftOpen : PanelLeftClose;
  const toggleLabel = rail ? "Expand sidebar" : "Collapse sidebar";
  const month = facts.orgMonth;
  const windowLabel = facts.orgWindow === "day" ? "Today" : facts.orgWindow === "none" ? "In total" : "This month";
  const percent = month ? percentUsed(month.spent, month.limit) : 0;
  const monthText = month
    ? `${windowLabel} ${formatMoney(month.spent)} of ${formatMoney(month.limit)} · ${String(percent)}%`
    : "";
  const toggle = onToggle !== undefined && (
    <Tip label={toggleLabel} shortcut={shortcut} side={rail ? "right" : "bottom"}>
      <button
        type="button"
        aria-label={toggleLabel}
        aria-expanded={!rail}
        aria-controls="app-sidebar"
        onClick={onToggle}
        className="hidden size-11 shrink-0 cursor-pointer items-center justify-center rounded-lg text-muted-foreground hover:bg-panel-2 hover:text-foreground md:flex"
      >
        <ToggleIcon aria-hidden="true" className="size-[18px]" />
      </button>
    </Tip>
  );
  return (
    <div
      className={cn(
        "flex h-full w-full flex-col pt-[18px] pb-5",
        rail ? "items-stretch gap-3 px-1" : "gap-5 px-3.5",
      )}
    >
      {/* Below 768 px the drawer's top bar already shows the logo and the close button. */}
      <div className={cn("flex items-center max-md:hidden", rail ? "flex-col gap-1" : "justify-between")}>
        <Link
          to={landingFor(role)}
          onClick={onNavigate}
          className={cn(
            "flex min-h-11 items-center text-foreground no-underline",
            rail ? "w-8 overflow-hidden" : "px-1.5",
          )}
        >
          {rail ? <LogoTile size={32} /> : <Logo size={32} />}
        </Link>
        {toggle}
      </div>

      {rail ? (
        <Tip label={orgName}>
          <div
            role="img"
            aria-label={`${orgName}. ${orgNote}`}
            className="mx-auto flex size-10 items-center justify-center rounded-[10px] border border-card-border bg-panel-2"
          >
            <span className="flex size-7 items-center justify-center rounded-[7px] bg-link-soft font-mono text-[11px] font-semibold text-link">
              {initials}
            </span>
          </div>
        </Tip>
      ) : (
        <div className="flex min-h-12 items-center gap-2.5 rounded-[10px] border border-card-border bg-panel-2 px-2.5 py-1.5">
          <span
            aria-hidden="true"
            className="flex size-7 items-center justify-center rounded-[7px] bg-link-soft font-mono text-[11px] font-semibold text-link"
          >
            {initials}
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-[13.5px] font-semibold">{orgName}</span>
            <span className="text-xs text-small">{orgNote}</span>
          </span>
        </div>
      )}

      <nav aria-label="Main" className="flex flex-col gap-0.5">
        {firstRun.active && (
          <Tip label={`${COPY.title} · ${String(firstRun.done)} ${COPY.sidebarOf}`} disabled={!rail}>
            <NavLink
              to="/start"
              onClick={onNavigate}
              className={cn(
                "relative flex min-h-11 items-center rounded-lg text-sm font-medium no-underline",
                rail ? "justify-center" : "gap-2.5 px-2.5",
                isActiveRoute("/start")
                  ? "bg-nav-active font-semibold text-nav-active-text"
                  : "text-muted-foreground hover:bg-panel-2 hover:text-foreground",
              )}
            >
              <Flag aria-hidden="true" className="size-[18px] shrink-0" />
              {rail ? (
                <span className="sr-only">
                  {COPY.title} {firstRun.done} {COPY.sidebarOf}
                </span>
              ) : (
                <>
                  {COPY.title}
                  <span className="ml-auto text-xs text-small">
                    {firstRun.done} {COPY.sidebarOf}
                  </span>
                </>
              )}
            </NavLink>
          </Tip>
        )}
        {navGroupsFor(role).map((group, index) => (
          <div
            key={group.label}
            role={rail ? "group" : undefined}
            aria-label={rail ? group.label : undefined}
            className="flex flex-col gap-0.5"
          >
            {rail ? (
              index > 0 && <div aria-hidden="true" className="mx-2.5 my-2 h-px bg-card-border" />
            ) : (
              <span className={cn(SECTION_LABEL, index === 0 && "pt-1.5")}>{group.label}</span>
            )}
            {group.items.map((item) => {
              const live = item.id === "runs" && facts.liveRuns !== undefined && facts.liveRuns > 0;
              const waiting =
                item.id === "approvals" && facts.pendingApprovals !== undefined && facts.pendingApprovals > 0;
              const tip = live
                ? `${item.label} · ${String(facts.liveRuns)} live`
                : waiting
                  ? `${item.label} · ${String(facts.pendingApprovals)} waiting`
                  : item.label;
              return (
                <Tip key={item.id} label={tip} disabled={!rail}>
                  <NavLink
                    to={item.to}
                    onClick={onNavigate}
                    className={cn(
                      "relative flex min-h-11 items-center rounded-lg text-sm no-underline",
                      rail ? "justify-center" : "gap-2.5 px-2.5",
                      isActiveRoute(item.to)
                        ? "bg-nav-active font-semibold text-nav-active-text before:absolute before:inset-y-1.5 before:left-0 before:w-[3px] before:rounded-r-sm before:bg-nav-bar"
                        : "font-medium text-muted-foreground hover:bg-panel-2 hover:text-foreground",
                    )}
                  >
                    {({ isActive }) => (
                      <>
                        <item.icon aria-hidden="true" className="size-[18px] shrink-0" />
                        {rail ? <span className="sr-only">{item.label}</span> : item.label}
                        {live && (
                          <span
                            className={cn(
                              rail ? "absolute top-2 right-2" : "ml-auto inline-flex items-center gap-[5px] text-xs",
                              isActive ? "text-live-on-active" : "text-live",
                            )}
                          >
                            <span aria-hidden="true" className="block size-1.5 rounded-full bg-live" />
                            <span className={rail ? "sr-only" : undefined}>{facts.liveRuns} live</span>
                          </span>
                        )}
                        {waiting && (
                          <span
                            className={cn(
                              "inline-flex items-center justify-center rounded-full bg-approvals font-bold text-warm-ink",
                              rail
                                ? "absolute top-0.5 right-0.5 h-4 min-w-4 px-1 text-[10px]"
                                : "ml-auto h-5 min-w-[22px] px-1.5 text-[11.5px]",
                            )}
                          >
                            {facts.pendingApprovals}
                            <span className="sr-only"> waiting</span>
                          </span>
                        )}
                      </>
                    )}
                  </NavLink>
                </Tip>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="grow" />

      {month &&
        (rail ? (
          <Tip label={monthText}>
            <div role="img" aria-label={monthText} className="relative mx-auto size-10">
              <svg viewBox="0 0 40 40" className="size-10 -rotate-90" aria-hidden="true">
                <circle cx="20" cy="20" r="16" fill="none" strokeWidth="4" className="stroke-card-border" />
                <circle
                  cx="20"
                  cy="20"
                  r="16"
                  fill="none"
                  strokeWidth="4"
                  strokeLinecap="round"
                  className="stroke-brand"
                  pathLength={100}
                  strokeDasharray={`${String(Math.min(percent, 100))} 100`}
                />
              </svg>
              <span
                aria-hidden="true"
                className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold"
              >
                {percent}%
              </span>
            </div>
          </Tip>
        ) : (
          <div className="flex flex-col gap-2.5 rounded-[10px] border border-card-border bg-panel-2 p-3">
            <span className="font-sans text-[11px] font-semibold tracking-[0.06em] text-small">{windowLabel.toUpperCase()}</span>
            <div className="flex items-baseline gap-1.5">
              <span className="text-lg font-semibold">{formatMoney(month.spent)}</span>
              <span className="text-[12.5px] text-small">
                of {formatMoney(month.limit)} · {percent}%
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-card-border" aria-hidden="true">
              <div className="h-full rounded-full bg-brand" style={{ width: `${String(percent)}%` }} />
            </div>
          </div>
        ))}

      <div className={cn("flex items-center", rail ? "flex-col gap-1" : "flex-wrap gap-x-2.5 gap-y-1 px-1.5")}>
        {rail ? (
          <Tip label={`${person} · ${personNote}`}>
            <span
              role="img"
              aria-label={`${person}, ${personNote}`}
              className="mb-1 flex size-8 shrink-0 items-center justify-center rounded-full bg-brand text-[11px] font-bold text-primary-foreground"
            >
              {personTag}
            </span>
          </Tip>
        ) : (
          <>
            <span
              aria-hidden="true"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-brand text-[11px] font-bold text-primary-foreground"
            >
              {personTag}
            </span>
            <span className="flex min-w-[8rem] grow basis-[8rem] flex-col">
              <span className="truncate text-[13.5px] font-semibold">{person}</span>
              <span className="truncate text-xs text-small">{personNote}</span>
            </span>
          </>
        )}
        <ThemeToggle tip={rail} />
        <SignOutButton tip={rail} />
        <Tip label="Settings" disabled={!rail}>
          <NavLink
            to="/settings"
            onClick={onNavigate}
            aria-label="Settings"
            className={cn(
              "flex size-11 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-panel-2 hover:text-foreground",
              isActiveRoute("/settings") && "bg-panel-3 text-foreground",
            )}
          >
            <Settings aria-hidden="true" className="size-[18px]" />
          </NavLink>
        </Tip>
      </div>
    </div>
  );
}
