import { Menu, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Link, Outlet, useLocation } from "react-router";

import { useLanding } from "@/app/use-landing";

import { isEditableTarget } from "@/builder/shortcuts";
import { DevMetrics } from "@/components/dev-metrics";
import { Sidebar } from "@/components/sidebar";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  LAYOUT_EVENT,
  readSidebarPreference,
  resolveCollapsed,
  type SidebarPreference,
  WIDE_QUERY,
  writeSidebarPreference,
} from "@/lib/sidebar-pref";
import { OrgStatusBar } from "@/org-safety/org-status-bar";
import { Logo } from "@brand/Logo";

/** Live `matchMedia` result; false where it is unavailable. */
function useMedia(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => {
        list.removeEventListener("change", notify);
      };
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches, () => false);
}

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);
const SHORTCUT_LABEL = IS_MAC ? "⌘B" : "Ctrl+B";

/**
 * Page frame (SPEC-app-v1 §2): skip link, the left sidebar and the main landmark. Below 768 px the
 * sidebar becomes a menu under a top bar. It is one `<nav>` either way, so assistive technology
 * never sees two.
 */
export function AppShell() {
  const [open, setOpen] = useState(false);
  const landing = useLanding();
  const { pathname } = useLocation();
  const builder = pathname === "/builder" || pathname.startsWith("/builder/");
  const wide = useMedia(WIDE_QUERY);
  const desktop = useMedia("(min-width: 768px)");
  const [stored, setStored] = useState<SidebarPreference | null>(readSidebarPreference);
  const collapsed = resolveCollapsed(stored, wide);
  // Icons only applies to the desktop sidebar; the narrow-width menu is always the full one.
  const rail = collapsed && desktop;
  const toggleSidebar = useCallback(() => {
    const next: SidebarPreference = collapsed ? "expanded" : "collapsed";
    setStored(next);
    writeSidebarPreference(next);
  }, [collapsed]);
  // Ctrl/Cmd+B toggles it, except while typing (and not over the builder's own shortcuts).
  useEffect(() => {
    if (!desktop) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.defaultPrevented || event.key.toLowerCase() !== "b") return;
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      if (isEditableTarget(event.target)) return;
      event.preventDefault();
      toggleSidebar();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [desktop, toggleSidebar]);
  // Tell the builder the canvas width changed (not on first render).
  const railSeen = useRef(rail);
  useEffect(() => {
    if (railSeen.current === rail) return;
    railSeen.current = rail;
    window.dispatchEvent(new Event(LAYOUT_EVENT));
  }, [rail]);
  const menuButton = useRef<HTMLButtonElement>(null);
  const focusMainOnNavigate = useRef(false);
  // A route change closes the menu (the "adjust state while rendering" pattern, no effect needed).
  const [seen, setSeen] = useState(pathname);
  if (seen !== pathname) {
    setSeen(pathname);
    setOpen(false);
  }
  // After a link in the menu is followed, keyboard focus moves to the page (it would be lost
  // with the menu hidden).
  useEffect(() => {
    if (!focusMainOnNavigate.current) return;
    focusMainOnNavigate.current = false;
    document.getElementById("main")?.focus();
  }, [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      // Escape closes the innermost layer only: a theme menu open inside the sidebar takes it first.
      if (document.querySelector('[role="menu"]') !== null) return;
      setOpen(false);
      menuButton.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <TooltipProvider delayDuration={150} skipDelayDuration={300}>
    <div className="flex h-full flex-col md:flex-row">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-background px-3 py-2 focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-panel px-4 md:hidden">
        <Link to={landing} className="flex min-h-11 items-center text-foreground no-underline">
          <Logo size={32} />
        </Link>
        <Button
          variant="ghost"
          size="icon-lg"
          className="ml-auto size-11"
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          aria-controls="app-sidebar"
          ref={menuButton}
          onClick={() => {
            setOpen((value) => !value);
          }}
        >
          {open ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
        </Button>
      </header>
      <aside
        id="app-sidebar"
        className={`${open ? "flex flex-col" : "hidden"} min-h-0 flex-1 overflow-y-auto border-r bg-panel shadow-[1px_0_8px_rgba(15,23,42,0.05)] dark:shadow-none md:block ${rail ? "md:w-14 md:overflow-x-hidden" : "md:w-62"} md:flex-none md:shrink-0 md:flex-col`}
      >
        <Sidebar
          rail={rail}
          onToggle={toggleSidebar}
          shortcut={SHORTCUT_LABEL}
          onNavigate={() => {
            focusMainOnNavigate.current = true;
            setOpen(false);
          }}
        />
      </aside>
      <div className={`${open ? "flex-none md:flex-1" : "flex-1"} flex min-h-0 min-w-0 flex-col`}>
        {/* The bar is part of the page flow, never pinned over content (ND106): it sits above the
            canvas on the builder (a full-height screen that does not scroll) and at the top of the
            scrolling page everywhere else, so it scrolls away with the content. */}
        {builder && <OrgStatusBar />}
        <main
          id="main"
          tabIndex={-1}
          className={`${open ? "hidden md:block" : ""} min-h-0 min-w-0 flex-1 overflow-auto focus:outline-none`}
        >
          {!builder && <OrgStatusBar />}
          <Outlet />
        </main>
      </div>
    {import.meta.env.DEV && <DevMetrics />}
    </div>
    </TooltipProvider>
  );
}
