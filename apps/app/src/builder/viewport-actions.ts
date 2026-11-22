import { type FitViewOptions, type Node, useReactFlow } from "@xyflow/react";

import { FIT_OPTIONS } from "./fit";

/**
 * Whether the person has moved the view themselves (drag, wheel, pinch, or the zoom buttons) since the
 * last fit. A view they adjusted is never refitted behind their back (entering or leaving full screen,
 * for example); an untouched one follows the canvas size. One builder is open at a time.
 */
let touched = false;

export function markViewportTouched(): void {
  touched = true;
}

export function viewportIsTouched(): boolean {
  return touched;
}

/** Fits the organization into view and counts the view as untouched again. */
export function fitCanvas<N extends Node>(flow: { fitView: (options?: FitViewOptions<N>) => Promise<boolean> }, duration = 0): void {
  touched = false;
  void flow.fitView({ duration, ...FIT_OPTIONS });
}

/** Zoom and fit actions for the canvas (shared by the corner controls and the full-screen toolbar). */
export function useViewportActions() {
  const flow = useReactFlow();
  return {
    zoomIn: () => {
      markViewportTouched();
      void flow.zoomIn({ duration: 150 });
    },
    zoomOut: () => {
      markViewportTouched();
      void flow.zoomOut({ duration: 150 });
    },
    fit: () => {
      fitCanvas(flow, 200);
    },
  };
}
