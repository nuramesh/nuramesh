import { Tooltip as TooltipPrimitive } from "radix-ui";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Where tooltips are drawn. In the browser's real full-screen mode only the full-screen element's
 * subtree is shown, so a tooltip portalled to the body would be invisible: the full-screen canvas
 * provides its own element here.
 */
export const TipContainerContext = React.createContext<HTMLElement | null>(null);

/** A small label for an icon: shown on hover and on keyboard focus, announced as its description. */
export function Tip({
  label,
  shortcut,
  side = "right",
  disabled = false,
  children,
}: {
  label: string;
  shortcut?: string;
  side?: "right" | "bottom" | "top" | "left";
  disabled?: boolean;
  children: React.ReactElement;
}) {
  const [open, setOpen] = React.useState(false);
  const container = React.use(TipContainerContext);
  return (
    <TooltipPrimitive.Root disableHoverableContent open={disabled ? false : open} onOpenChange={setOpen}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal container={container}>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={8}
          className={cn(
            "z-50 flex items-center gap-2 rounded-md border bg-popover px-2 py-1 text-xs font-medium whitespace-nowrap text-popover-foreground shadow-md",
          )}
        >
          {label}
          {shortcut !== undefined && (
            <kbd className="rounded border bg-panel-2 px-1 font-mono text-[10.5px] text-small">{shortcut}</kbd>
          )}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

export const TooltipProvider = TooltipPrimitive.Provider;
