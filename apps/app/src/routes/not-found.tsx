import { Link, useLocation } from "react-router";

import { Button } from "@/components/ui/button";

/** Shown for any URL the app doesn't know. */
export function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <section aria-labelledby="not-found-title" className="mx-auto flex max-w-xl flex-col gap-4 p-8">
      <p className="text-sm font-medium text-muted-foreground">404</p>
      <h1 id="not-found-title" className="text-2xl font-semibold">
        Page not found
      </h1>
      <p className="text-muted-foreground">
        There is no page at <code className="rounded bg-muted px-1">{pathname}</code>.
      </p>
      <div>
        <Button asChild>
          <Link to="/builder">Go to the builder</Link>
        </Button>
      </div>
    </section>
  );
}
