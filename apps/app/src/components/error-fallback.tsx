import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";

interface Props {
  error: Error;
  onRetry?: () => void;
  /** Show the raw error message (development builds only by default: it can leak internals). */
  showDetails?: boolean;
}

/** Error screen: says what happened and offers a way out. */
export function ErrorFallback({ error, onRetry, showDetails = import.meta.env.DEV }: Props) {
  return (
    <div role="alert" className="mx-auto flex max-w-xl flex-col items-start gap-4 p-8">
      <AlertTriangle aria-hidden="true" className="size-8 text-destructive" />
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="text-muted-foreground">
        The page hit an unexpected error. Your saved work is not affected. Try again, or reload
        the page.
      </p>
      {showDetails && (
        <pre className="max-w-full overflow-auto rounded-md bg-muted p-3 text-sm">{error.message}</pre>
      )}
      <div className="flex gap-2">
        {onRetry && <Button onClick={onRetry}>Try again</Button>}
        <Button
          variant="outline"
          onClick={() => {
            window.location.reload();
          }}
        >
          Reload page
        </Button>
      </div>
    </div>
  );
}
