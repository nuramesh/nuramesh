import { Panel } from "@xyflow/react";
import { Minus, Plus, ScanSearch } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";

import { useViewportActions } from "../viewport-actions";

/**
 * The corner controls: zoom in, zoom out and fit view, each with a visible tooltip (hover or keyboard
 * focus) and an accessible name. Fit view has its own icon (a magnifier over a frame) so it is not
 * mistaken for full screen. Hidden in full screen, where the floating toolbar has the same actions.
 */
export function CanvasControls() {
  const { zoomIn, zoomOut, fit } = useViewportActions();
  return (
    <Panel
      position="bottom-left"
      aria-label="Canvas controls"
      className="flex flex-col overflow-hidden rounded-md border bg-panel shadow-md group-data-[fullscreen=true]/fs:hidden"
    >
      <Tip label="Zoom in" side="right">
        <Button variant="ghost" size="icon-sm" className="rounded-none max-md:size-11" aria-label="Zoom in" onClick={zoomIn}>
          <Plus aria-hidden="true" />
        </Button>
      </Tip>
      <Tip label="Zoom out" side="right">
        <Button variant="ghost" size="icon-sm" className="rounded-none border-t max-md:size-11" aria-label="Zoom out" onClick={zoomOut}>
          <Minus aria-hidden="true" />
        </Button>
      </Tip>
      <Tip label="Fit view" side="right">
        <Button variant="ghost" size="icon-sm" className="rounded-none border-t max-md:size-11" aria-label="Fit view" onClick={fit}>
          <ScanSearch aria-hidden="true" />
        </Button>
      </Tip>
    </Panel>
  );
}
