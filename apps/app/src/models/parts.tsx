import type { ReactNode } from "react";

import { NotAvailableError } from "@/api/client";
import { describeProblem } from "@/api/problems";
import type { EntryStatus } from "@/api/models";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";

import { STATUS_TONE } from "./words";

export function StatusChip({ status }: { status: EntryStatus }) {
  return <Chip tone={STATUS_TONE[status]}>{status}</Chip>;
}

/** A page-level loading state. */
export function Loading({ what }: { what: string }) {
  return (
    <p role="status" aria-label="Loading" className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
      Loading {what}…
    </p>
  );
}

/** A failed or impossible read: names what is wrong in words and offers a retry. */
export function ReadFailed({ what, error, retry }: { what: string; error: unknown; retry: () => void }) {
  const unavailable = error instanceof NotAvailableError;
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-dashed p-6">
      <p className="text-sm">
        {unavailable ? `This data source can't give ${what} yet.` : `Couldn't load ${what}.`}{" "}
        <span className="text-muted-foreground">{unavailable ? "" : describeProblem(error)}</span>
      </p>
      <Button variant="outline" className="min-h-11" onClick={retry}>
        Try again
      </Button>
    </div>
  );
}

/** What the last write did, kept until the next one. */
export function Notice({ lines, onDismiss }: { lines: string[]; onDismiss: () => void }) {
  return (
    <div role="status" aria-label="Last change" aria-live="polite" className="callout-success flex items-start gap-3 px-4 py-3 text-sm">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {lines.map((line) => (
          <span key={line}>{line}</span>
        ))}
      </div>
      <Button variant="ghost" size="sm" className="min-h-11" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

/** A list row card: stacks on a phone, a grid from 768 px. */
export function RowCard({ children, ...rest }: { children: ReactNode } & React.ComponentProps<"li">) {
  return (
    <li {...rest} className="flex flex-col gap-2 rounded-xl border border-card-border bg-panel px-4 py-3.5 shadow-card">
      {children}
    </li>
  );
}

/** What every tab gets from the page: whether the viewer is an admin, and a way to report a write. */
export interface TabProps {
  admin: boolean;
  notify: (lines: string[]) => void;
}
