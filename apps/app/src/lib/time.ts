/** Display-only date and time helpers (the ledger keeps UTC; these show the viewer's local time). */

/** "19:39:18" in the viewer's time zone. */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "today 19:39" for today, "21 Oct 16:05" otherwise. */
export function formatStarted(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const sameDay = date.toDateString() === now.toDateString();
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? `today ${time}` : `${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${time}`;
}

export interface LocalTimeOptions {
  /** Locale override for tests; the viewer's own locale when omitted. */
  locale?: string;
  /** Time zone override for tests; the viewer's own zone when omitted. */
  timeZone?: string;
}

const UNITS: readonly [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

/** "3 min. ago" or "in 2 mo." from the clock `now` (injectable, so tests are not time-dependent). */
export function formatRelative(iso: string, now: Date, locale?: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((then - now.getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "short" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.trunc(seconds / size), unit);
  }
  return rtf.format(0, "second");
}

/** "27 Oct 2026, 7:34 AM · 3 min. ago": the viewer's local date and time, then the relative time. */
export function formatLocalWithRelative(iso: string, now: Date = new Date(), options: LocalTimeOptions = {}): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const absolute = new Intl.DateTimeFormat(options.locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    ...(options.timeZone === undefined ? {} : { timeZone: options.timeZone }),
  }).format(date);
  return `${absolute} · ${formatRelative(iso, now, options.locale)}`;
}
