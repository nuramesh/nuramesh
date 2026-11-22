import type { NodeStatus, RunStatus, Tone } from "@/api/types";
import type { ChipTone } from "@/components/ui/chip";

export const STATUS_LABEL: Record<RunStatus, string> = {
  running: "Running",
  stopping: "Stopping…",
  waiting: "Waiting for you",
  paused: "Paused",
  blocked: "Blocked by budget",
  done: "Done",
  failed: "Failed",
  stopped: "Stopped",
  unknown: "Unknown",
};

/** Colour is never the only signal (the label always says it); these are the tokens behind it. */
export const STATUS_TONE: Record<RunStatus, ChipTone> = {
  running: "warning",
  stopping: "warning",
  waiting: "warning",
  paused: "neutral",
  blocked: "danger",
  done: "success",
  failed: "danger",
  stopped: "neutral",
  unknown: "neutral",
};

export const NODE_STYLE: Record<NodeStatus, { dot: string; text: string }> = {
  done: { dot: "bg-link", text: "text-link" },
  running: { dot: "bg-warm", text: "text-warm" },
  waiting: { dot: "bg-warm", text: "text-warm" },
  idle: { dot: "border-[1.5px] border-small bg-transparent", text: "text-small" },
};

export const TONE_STYLE: Record<Tone, { dot: string; kind: string }> = {
  accent: { dot: "bg-link", kind: "text-link" },
  warm: { dot: "bg-warm", kind: "text-warm" },
  muted: { dot: "bg-small", kind: "text-muted-foreground" },
};

/** A run that can still change (so the page keeps asking for updates). */
export function isActive(status: RunStatus): boolean {
  return status === "running" || status === "stopping" || status === "waiting" || status === "paused" || status === "blocked";
}

/** A run whose numbers can still change: active, and not ended (a server run "waiting for a person" has ended). */
export function isLive(run: { status: RunStatus; ended?: boolean }): boolean {
  return run.ended !== true && isActive(run.status);
}

export function canPause(status: RunStatus, ended = false): boolean {
  return !ended && (status === "running" || status === "waiting");
}

/** Stop is offered while the run can still change and a stop isn't already on its way. */
export function canStop(status: RunStatus, ended = false): boolean {
  return !ended && status !== "stopping" && isActive(status);
}

/** "3m 12s", "1h 04m", "45s". */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${String(h)}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${String(m)}m ${String(s).padStart(2, "0")}s`;
  return `${String(s)}s`;
}

/** "19:39" in the viewer's time zone (the ledger keeps UTC; this is display only). */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "Oct 23, 19:39" for a list; the time alone for today. */
export function formatStarted(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const sameDay = date.toDateString() === now.toDateString();
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? `today ${time}` : `${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${time}`;
}
