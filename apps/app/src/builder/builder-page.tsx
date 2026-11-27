import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { LayoutGrid, Minus, PanelRight, Plus, ScanSearch, UserPlus, X } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useLocation, useNavigate } from "react-router";

import { LAYOUT_EVENT } from "@/lib/sidebar-pref";
import { cn } from "@/lib/utils";

import { Button } from "@/components/ui/button";
import { Tip, TipContainerContext } from "@/components/ui/tooltip";

import { Canvas } from "./components/canvas";
import { BuilderHeader } from "./components/header";
import { Palette } from "./components/palette";
import { SidePanel } from "./components/side-panel";
import { Toolbar } from "./components/toolbar";
import { blankDocument, type BuilderDocument, parseDocument } from "./document";
import { createLayoutEngine } from "./layout-engine";
import { fitCanvas, useViewportActions, viewportIsTouched } from "./viewport-actions";
import { useBuilderShortcuts } from "./shortcuts";
import { connectDraftAutosave, connectLayout, createBuilderStore } from "./store";
import { BuilderStoreContext, useBuilder, useBuilderStore } from "./store-context";
import { useStorageScope } from "@/auth/storage-scope";

import { loadDraft, saveDraft } from "./storage";

function StatusBar() {
  const announcement = useBuilder((s) => s.announcement);
  const agents = useBuilder((s) => s.doc.org.agents.length);
  const errors = useBuilder((s) => s.issues.errorCount);
  const layout = useBuilder((s) => s.layoutStatus);
  return (
    <div className="flex items-center gap-4 border-t px-3 py-1 text-xs text-muted-foreground">
      <span data-testid="builder-summary">
        {agents} agent{agents === 1 ? "" : "s"} · {errors === 0 ? "valid" : `${String(errors)} error${errors === 1 ? "" : "s"}`}
        {layout === "running" ? " · arranging…" : ""}
      </span>
      {/* Polite live region: confirms edits, undo/redo and imports for screen-reader users. */}
      <p role="status" aria-live="polite" className="ml-auto truncate">
        {announcement.text}
      </p>
    </div>
  );
}

/** Matches a media query and follows it (`useSyncExternalStore`, so there is no flash on first paint). */
function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const media = window.matchMedia(query);
      media.addEventListener("change", onChange);
      return () => {
        media.removeEventListener("change", onChange);
      };
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** The phone-mode banner: what this view is and where to edit (a note, always visible; not a live region). */
function PhoneNotice() {
  const name = useBuilder((s) => s.doc.org.name);
  return (
    <div role="note" className="flex shrink-0 flex-col gap-0.5 border-b bg-panel px-4 py-3 text-sm">
      <h1 className="truncate font-medium" data-testid="phone-org-name">
        {name}
      </h1>
      <p className="text-muted-foreground">
        Read-only on a phone: drag to pan, pinch to zoom. Open this page on a larger screen to edit.
      </p>
    </div>
  );
}

const PHONE_QUERY = "(max-width: 767px)";

const RAIL_KEY = "nuramesh.builder.palette-expanded";

/** The remembered palette choice (`true` expanded, `false` rail), or `null` for "no choice yet". */
function readRailChoice(): boolean | null {
  try {
    const value = window.localStorage.getItem(RAIL_KEY);
    return value === "1" ? true : value === "0" ? false : null;
  } catch {
    return null;
  }
}

function writeRailChoice(expanded: boolean): void {
  try {
    window.localStorage.setItem(RAIL_KEY, expanded ? "1" : "0");
  } catch {
    // storage blocked: the choice lasts for this visit only
  }
}

type Side = "left" | "right";

/** Focuses the visible button that opens a drawer (the toolbar's or the full-screen one). */
function focusToggle(side: Side): void {
  const buttons = document.querySelectorAll<HTMLElement>(`[data-drawer-toggle="${side}"]`);
  for (const button of buttons) {
    if (button.offsetParent !== null) {
      button.focus();
      return;
    }
  }
}

/**
 * The title bar of an overlay drawer, with its close button.
 */
function DrawerBar({ side, title, onClose }: { side: Side; title: string; onClose: () => void }) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b bg-panel px-3 py-1.5">
      <span className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Close ${title.toLowerCase()}`}
        title="Close (Esc)"
        data-drawer-close={side}
        onClick={onClose}
      >
        <X aria-hidden="true" />
      </Button>
    </div>
  );
}

/**
 * The builder's layout (design decision, round 2):
 * - **1280 px and wider**: Properties docked on the right; the palette docked on the left, as an
 *   icon rail (about 3rem) that expands on click; with no stored choice it starts as that rail below
 *   1600 px and expanded from 1600 px (a stored choice wins at every width);
 * - **below 1280 px, and in full screen**: the palette and Properties are drawers over the canvas,
 *   opened from toolbar buttons; Esc, the close button or a click outside closes them. They overlay
 *   the canvas and never push it.
 * One drawer component serves both, and the canvas never remounts when the layout changes.
 */
function BuilderLayout() {
  // Full-screen canvas: the app shell and the builder chrome step aside; the palette and Properties
  // are drawers over the canvas. The layout re-classes in place, so the canvas never remounts.
  const [fullscreen, setFullscreen] = useState(false);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  // True while the browser's own full-screen mode shows our root (as opposed to the fixed overlay fallback).
  const nativeFullscreen = useRef(false);
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [railChoice, setRailChoice] = useState<boolean | null>(readRailChoice);
  const flow = useReactFlow();
  const store = useBuilderStore();
  const focusRequest = useBuilder((s) => s.focusRequest);
  const wide = useMediaQuery("(min-width: 1280px)");
  const roomy = useMediaQuery("(min-width: 1600px)");
  // Phone mode (ND65): below 768 px the builder is a read-only view you pan and zoom. Editing needs room.
  const phone = useMediaQuery(PHONE_QUERY);
  // A phone has no full-screen toggle and no way out of it (the chrome is hidden): leave it when
  // a rotation or resize turns this view into a phone (adjusting state while rendering).
  if (phone && fullscreen) setFullscreen(false);
  const overlay = fullscreen || !wide;
  const expanded = railChoice ?? roomy;
  const anyOpen = overlay && (leftOpen || rightOpen);

  // Leaving drawer mode (a window resized wide, full screen exited) forgets which drawer was open.
  const [wasOverlay, setWasOverlay] = useState(overlay);
  if (wasOverlay !== overlay) {
    // (adjusting state while rendering, the documented way to react to a prop or query change)
    setWasOverlay(overlay);
    setLeftOpen(false);
    setRightOpen(false);
  }

  const closeDrawers = (): void => {
    setLeftOpen(false);
    setRightOpen(false);
  };
  const toggleDrawer = (side: Side): void => {
    setLeftOpen(side === "left" ? !leftOpen : false);
    setRightOpen(side === "right" ? !rightOpen : false);
  };
  const setExpanded = (value: boolean): void => {
    setRailChoice(value);
    writeRailChoice(value);
  };

  // Focus moves into a drawer when it opens and back to its button when it closes.
  const wasOpen = useRef<Record<Side, boolean>>({ left: false, right: false });
  useEffect(() => {
    for (const [side, open] of [
      ["left", overlay && leftOpen],
      ["right", overlay && rightOpen],
    ] as const) {
      if (open && !wasOpen.current[side]) {
        document.querySelector<HTMLElement>(`[data-drawer-close="${side}"]`)?.focus();
      } else if (!open && wasOpen.current[side] && overlay) {
        focusToggle(side);
      }
      wasOpen.current[side] = open;
    }
  }, [overlay, leftOpen, rightOpen]);

  // Escape closes the innermost layer only: an open menu or dialog takes it first, then an open
  // drawer, then full screen.
  useEffect(() => {
    if (!fullscreen && !anyOpen) return;
    const onKey = (event: KeyboardEvent): void => {
      // (not `defaultPrevented`: the open tooltip of the focused corner button prevents it, and a
      // tooltip must not make Esc need two presses)
      if (event.key !== "Escape") return;
      if (document.querySelector('[role="menu"], [role="dialog"]') !== null) return;
      if (anyOpen) {
        setLeftOpen(false);
        setRightOpen(false);
      } else {
        setFullscreen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [fullscreen, anyOpen]);

  // Full screen shows the canvas over the whole window. The browser's Fullscreen API is asked first;
  // when it is missing or refused the fixed overlay (`fixed inset-0`) already covers the window. The
  // view (zoom and position) is never refitted when entering or leaving: it stays as the person left it.
  const enterFullscreen = (): void => {
    setFullscreen(true);
    if (root === null || typeof root.requestFullscreen !== "function") return;
    root.requestFullscreen().then(
      () => {
        // Keyboard lock lets Esc reach the page (to close a drawer first) instead of leaving full screen at once.
        const keyboard = (navigator as Navigator & { keyboard?: { lock?: (keys: string[]) => Promise<void> } }).keyboard;
        if (keyboard?.lock !== undefined) void keyboard.lock(["Escape"]).catch(() => undefined);
      },
      () => undefined, // refused: the overlay stays
    );
  };

  const toggleFullscreen = (): void => {
    if (fullscreen) setFullscreen(false);
    else enterFullscreen();
  };

  // The browser can leave full screen itself (Esc, F11, a tab switch): follow it.
  useEffect(() => {
    const onChange = (): void => {
      if (document.fullscreenElement !== null && document.fullscreenElement === root) {
        nativeFullscreen.current = true;
      } else if (nativeFullscreen.current && document.fullscreenElement === null) {
        nativeFullscreen.current = false;
        setFullscreen(false);
      }
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
    };
  }, [root]);

  // A view the person never moved follows the new canvas size once the window has resized; one they
  // moved (zoom, pan) is left exactly as it is.
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    const timer = window.setTimeout(() => {
      if (!viewportIsTouched()) fitCanvas(flow);
    }, 250);
    return () => {
      window.clearTimeout(timer);
    };
  }, [fullscreen, flow]);

  useEffect(() => {
    if (!fullscreen) return;
    const frame = requestAnimationFrame(() => {
      // the canvas corner button is the same element in both states; keep keyboard focus on it
      document.getElementById("builder-fullscreen-toggle")?.focus();
    });
    const behind = document.querySelectorAll("#app-sidebar, #root header");
    behind.forEach((el) => {
      el.setAttribute("inert", "");
    });
    return () => {
      cancelAnimationFrame(frame);
      behind.forEach((el) => {
        el.removeAttribute("inert");
      });
      nativeFullscreen.current = false;
      if (document.fullscreenElement !== null) void document.exitFullscreen().catch(() => undefined);
      (navigator as Navigator & { keyboard?: { unlock?: () => void } }).keyboard?.unlock?.();
      requestAnimationFrame(() => {
        document.getElementById("builder-fullscreen-toggle")?.focus();
      });
    };
  }, [fullscreen]);

  // The docked panels change the canvas width: fit again once the new size is measured.
  const dockedWidth = !wide ? "overlay" : expanded ? "expanded" : "rail";
  const fittedFor = useRef(dockedWidth);
  useEffect(() => {
    if (fittedFor.current === dockedWidth) return;
    fittedFor.current = dockedWidth;
    if (fullscreen) return;
    const timer = window.setTimeout(() => {
      fitCanvas(flow);
    }, 80);
    return () => {
      window.clearTimeout(timer);
    };
  }, [dockedWidth, fullscreen, flow]);

  // The app sidebar collapsing or expanding also changes the canvas width.
  useEffect(() => {
    let timer: number | undefined;
    const onLayout = (): void => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (!fullscreen) fitCanvas(flow);
      }, 80);
    };
    window.addEventListener(LAYOUT_EVENT, onLayout);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener(LAYOUT_EVENT, onLayout);
    };
  }, [fullscreen, flow]);

  // Choosing a problem (or anything that focuses an agent) puts the drawer away and shows the canvas.
  const [seenFocus, setSeenFocus] = useState(focusRequest);
  if (seenFocus !== focusRequest) {
    setSeenFocus(focusRequest);
    setRightOpen(false);
  }

  const chrome = fullscreen ? "hidden" : undefined;
  const drawerTop = fullscreen ? "top-14" : "top-0";
  return (
    <TipContainerContext value={root}>
    <div
      ref={setRoot}
      data-fullscreen={fullscreen}
      data-testid="builder-root"
      className={cn("group/fs", fullscreen ? "fixed inset-0 z-50 flex flex-col bg-background" : "flex h-full min-h-0 w-full max-w-full min-w-0 flex-col")}
    >
      {phone && <PhoneNotice />}
      <div className={cn(chrome, phone && "hidden")}>
        <BuilderHeader />
      </div>
      <div className={cn(chrome, phone && "hidden")}>
        <Toolbar
          drawers={
            overlay
              ? {
                  leftOpen,
                  rightOpen,
                  onLeft: () => {
                    toggleDrawer("left");
                  },
                  onRight: () => {
                    toggleDrawer("right");
                  },
                }
              : undefined
          }
        />
      </div>
      <div className="relative flex min-h-0 min-w-0 flex-1">
        <div
          id="builder-drawer-palette"
          className={cn(
            "flex min-h-0 flex-col [&>aside]:min-h-0 [&>aside]:flex-1",
            phone && "hidden",
            overlay
              ? cn("absolute bottom-0 left-0 z-20 w-72 max-w-[calc(100%-2.5rem)] border-r bg-pane shadow-xl [&>aside]:border-0", drawerTop, !leftOpen && "hidden")
              : cn("relative z-30 shrink-0", expanded ? "w-66 overflow-hidden" : "w-12"),
          )}
        >
          {overlay && (
            <DrawerBar
              side="left"
              title="Palette"
              onClose={() => {
                setLeftOpen(false);
              }}
            />
          )}
          <Palette
            collapsed={!overlay && !expanded}
            onExpand={() => {
              setExpanded(true);
            }}
            onCollapse={
              overlay
                ? undefined
                : () => {
                    setExpanded(false);
                  }
            }
          />
        </div>
        <div className="h-full min-h-0 min-w-0 flex-1">
          <Canvas fullscreen={fullscreen} onToggleFullscreen={toggleFullscreen} readOnly={phone} />
        </div>
        <div
          id="builder-drawer-properties"
          className={cn(
            "flex min-h-0 flex-col [&>aside]:min-h-0 [&>aside]:flex-1",
            phone && "hidden",
            overlay
              ? cn("absolute right-0 bottom-0 z-20 w-80 max-w-[calc(100%-2.5rem)] border-l bg-pane shadow-xl [&>aside]:border-0", drawerTop, !rightOpen && "hidden")
              : "w-80 shrink-0",
          )}
        >
          {overlay && (
            <DrawerBar
              side="right"
              title="Properties"
              onClose={() => {
                setRightOpen(false);
              }}
            />
          )}
          <SidePanel />
        </div>
        {anyOpen && (
          <div
            aria-hidden="true"
            data-testid="drawer-backdrop"
            className={cn("absolute inset-x-0 bottom-0 z-10 bg-black/30", drawerTop)}
            onClick={closeDrawers}
          />
        )}
      </div>
      <div className={chrome}>
        <StatusBar />
      </div>
      {fullscreen && (
        <FullscreenToolbar
          leftOpen={leftOpen}
          rightOpen={rightOpen}
          onLeft={() => {
            toggleDrawer("left");
          }}
          onRight={() => {
            // Properties show the selection, or the organization's own properties when nothing is selected.
            store.getState().setPanel("inspector");
            toggleDrawer("right");
          }}
        />
      )}
    </div>
    </TipContainerContext>
  );
}

/**
 * The floating toolbar of the full-screen canvas: add an agent (palette drawer), Properties drawer,
 * zoom, fit view and auto-layout. The way out is the corner button of the canvas (Esc and F also leave;
 * a drawer or menu closes first).
 */
function FullscreenToolbar({
  leftOpen,
  rightOpen,
  onLeft,
  onRight,
}: {
  leftOpen: boolean;
  rightOpen: boolean;
  onLeft: () => void;
  onRight: () => void;
}) {
  const store = useBuilderStore();
  const { zoomIn, zoomOut, fit } = useViewportActions();
  return (
    <div
      role="toolbar"
      aria-label="Full-screen canvas"
      className="absolute top-3 left-1/2 z-30 flex max-w-[calc(100%-8rem)] -translate-x-1/2 flex-wrap items-center justify-center gap-1.5 rounded-lg border bg-panel p-1.5 shadow-md"
    >
      <Button
        variant="outline"
        size="sm"
        aria-pressed={leftOpen}
        aria-controls="builder-drawer-palette"
        data-drawer-toggle="left"
        title="Add an agent: opens the palette"
        onClick={onLeft}
      >
        <UserPlus aria-hidden="true" /> Add agent
      </Button>
      <Button
        variant="outline"
        size="sm"
        aria-pressed={rightOpen}
        aria-controls="builder-drawer-properties"
        data-drawer-toggle="right"
        title="Properties of the selection, or of the organization"
        onClick={onRight}
      >
        <PanelRight aria-hidden="true" /> Properties
      </Button>
      <div className="mx-0.5 h-5 w-px bg-border" aria-hidden="true" />
      <Tip label="Zoom out" side="bottom">
        <Button variant="ghost" size="icon-sm" aria-label="Zoom out" onClick={zoomOut}>
          <Minus aria-hidden="true" />
        </Button>
      </Tip>
      <Tip label="Zoom in" side="bottom">
        <Button variant="ghost" size="icon-sm" aria-label="Zoom in" onClick={zoomIn}>
          <Plus aria-hidden="true" />
        </Button>
      </Tip>
      <Tip label="Fit view" side="bottom">
        <Button variant="ghost" size="icon-sm" aria-label="Fit view" onClick={fit}>
          <ScanSearch aria-hidden="true" />
        </Button>
      </Tip>
      <Tip label="Auto-layout" side="bottom">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Auto-layout"
          onClick={() => {
            store.getState().autoLayout();
          }}
        >
          <LayoutGrid aria-hidden="true" />
        </Button>
      </Tip>
    </div>
  );
}

/** A document another page hands to the builder (Use template, Import YAML) through router state. */
export interface BuilderHandoff {
  document: unknown;
  label: string;
}

function readHandoff(state: unknown): { doc: BuilderDocument; label: string } | null {
  const handoff = (state as { handoff?: Partial<BuilderHandoff> } | null)?.handoff;
  if (handoff === undefined || typeof handoff.label !== "string") return null;
  // History state outlives a deploy: read it like any untrusted document.
  const parsed = parseDocument(handoff.document);
  return parsed.ok ? { doc: parsed.value, label: handoff.label } : null;
}

/** The Workflow builder (PLAN A.1–A.5): canvas, palette, properties, roles and live validation. */
function BuilderForScope() {
  const location = useLocation();
  const navigate = useNavigate();
  const [handoff] = useState(() => readHandoff(location.state));
  const scope = useStorageScope(); // a person's draft is theirs: the key carries the organization and the person
  // The saved draft is the starting point, and a handed-over document loads on top of it as one
  // undoable step, so Undo returns to what the person had.
  const [store] = useState(() => {
    const created = createBuilderStore(loadDraft(scope) ?? blankDocument(), scope);
    if (handoff !== null) {
      created.getState().load(handoff.label, handoff.doc);
      saveDraft(created.getState().doc, scope); // so a reload before the first edit keeps it
    }
    return created;
  });
  // Used once: clear it, so a reload or Back doesn't apply it again over later edits.
  useEffect(() => {
    if (handoff !== null) void navigate(location.pathname, { replace: true, state: null });
  }, [handoff, location.pathname, navigate]);
  useBuilderShortcuts(store, useMediaQuery(PHONE_QUERY));
  useEffect(() => {
    const engine = createLayoutEngine();
    const stop = connectLayout(store, engine);
    return () => {
      stop();
      engine.dispose();
    };
  }, [store]);
  useEffect(() => connectDraftAutosave(store), [store]);

  return (
    <BuilderStoreContext value={store}>
      <ReactFlowProvider>
        <title>Org builder · NuraMesh</title>
        <BuilderLayout />
      </ReactFlowProvider>
    </BuilderStoreContext>
  );
}

/** The builder, remounted whenever whose draft it shows changes, so one person's draft is never shown to the next. */
export function BuilderPage() {
  const scope = useStorageScope();
  return <BuilderForScope key={scope ?? ""} />;
}
