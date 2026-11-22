import type { ReactNode } from "react";

import { Logo } from "@brand/Logo";

/**
 * The frame of the sign-in screens: one centred column that fits a 320 px phone with no sideways
 * scroll, and a wider card from 640 px up.
 */
export function AuthCard({ title, intro, children, footer }: { title: string; intro?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <main className="flex min-h-dvh items-start justify-center bg-background px-4 py-8 sm:items-center">
      <section aria-labelledby="auth-title" className="flex w-full max-w-sm flex-col gap-5 rounded-xl border bg-panel p-5 sm:p-8">
        <div className="flex justify-center">
          <Logo size={36} />
        </div>
        <header className="flex flex-col gap-1.5 text-center">
          <h1 id="auth-title" className="text-2xl font-semibold">
            {title}
          </h1>
          {intro !== undefined && <p className="text-sm text-muted-foreground">{intro}</p>}
        </header>
        {children}
        {footer !== undefined && <p className="text-center text-[13px] text-muted-foreground">{footer}</p>}
      </section>
    </main>
  );
}

/** A labelled field row for the forms: label above the input, hint below. */
export function Field({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {hint !== undefined && (
        <p id={`${id}-hint`} className="text-[13px] text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}
