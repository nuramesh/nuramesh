import { LayoutGrid, PanelLeft, PanelRight, Redo2, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/button";

import { useBuilder, useBuilderStore } from "../store-context";

/**
 * Canvas actions, all on the left: undo/redo and Auto-layout (plus the drawer toggles below 1280 px).
 * Full screen is a button in the canvas corner; document actions are in the header; Fit view lives
 * in the canvas controls.
 */
export function Toolbar({
  drawers,
}: {
  /** Present when the palette and Properties are overlay drawers (below 1280 px): their toggles. */
  drawers?: { leftOpen: boolean; rightOpen: boolean; onLeft: () => void; onRight: () => void };
}) {
  const store = useBuilderStore();
  const canUndo = useBuilder((s) => s.history.past.length > 0);
  const canRedo = useBuilder((s) => s.history.future.length > 0);

  return (
    <div className="flex flex-wrap items-center gap-1 border-b bg-pane px-3 py-1.5" role="toolbar" aria-label="Canvas">
      {drawers !== undefined && (
        <>
          <Button
            variant="outline"
            size="sm"
            aria-pressed={drawers.leftOpen}
            aria-controls="builder-drawer-palette"
            data-drawer-toggle="left"
            title="Show or hide the palette"
            onClick={drawers.onLeft}
          >
            <PanelLeft aria-hidden="true" /> Palette
          </Button>
          <Button
            variant="outline"
            size="sm"
            aria-pressed={drawers.rightOpen}
            aria-controls="builder-drawer-properties"
            data-drawer-toggle="right"
            title="Show or hide the properties panel"
            onClick={drawers.onRight}
          >
            <PanelRight aria-hidden="true" /> Properties
          </Button>
          <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
        </>
      )}
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Undo"
        title="Undo (Ctrl/⌘ Z)"
        aria-keyshortcuts="Control+Z Meta+Z"
        disabled={!canUndo}
        onClick={() => {
          store.getState().undo();
        }}
      >
        <Undo2 aria-hidden="true" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Redo"
        title="Redo (Shift Ctrl/⌘ Z or Ctrl Y)"
        aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z Control+Y"
        disabled={!canRedo}
        onClick={() => {
          store.getState().redo();
        }}
      >
        <Redo2 aria-hidden="true" />
      </Button>
      <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          store.getState().autoLayout();
        }}
      >
        <LayoutGrid aria-hidden="true" /> Auto-layout
      </Button>
    </div>
  );
}
