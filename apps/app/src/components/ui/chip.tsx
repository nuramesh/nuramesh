import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The five tones of a status chip (tokens `--t-chip-<tone>-*`: background, text, 1px border). */
export type ChipTone = "success" | "warning" | "danger" | "info" | "neutral";

const TONE: Record<ChipTone, string> = {
  success: "chip-success",
  warning: "chip-warning",
  danger: "chip-danger",
  info: "chip-info",
  neutral: "chip-neutral",
};

/** The one status chip of the app: a word (colour only helps) in a bordered pill. */
export function Chip({ tone, className, children, ...rest }: { tone: ChipTone; children: ReactNode } & ComponentProps<"span">) {
  return (
    <span {...rest} className={cn("chip", TONE[tone], className)}>
      {children}
    </span>
  );
}

/** A filter pill: white with a visible border when off, filled indigo with white text when on. */
export function FilterPill({ pressed, className, ...rest }: { pressed: boolean } & Omit<ComponentProps<"button">, "type" | "aria-pressed">) {
  return (
    <button
      {...rest}
      type="button"
      aria-pressed={pressed}
      className={cn(
        "min-h-11 rounded-full border px-3.5 text-[13px]",
        pressed
          ? "border-pill-on-border bg-pill-on text-pill-on-text"
          : "border-pill-border bg-pill text-pill-text hover:bg-panel-3",
        className,
      )}
    />
  );
}
