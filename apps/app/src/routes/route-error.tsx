import { isRouteErrorResponse, useRouteError } from "react-router";

import { ErrorFallback } from "@/components/error-fallback";

/** Router-level error screen (errors thrown while rendering a route). */
export function RouteErrorPage() {
  const error = useRouteError();
  const normalized = isRouteErrorResponse(error)
    ? new Error(`${String(error.status)} ${error.statusText}`)
    : error instanceof Error
      ? error
      : new Error(String(error));
  return <ErrorFallback error={normalized} />;
}
