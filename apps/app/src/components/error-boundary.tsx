import { Component, type ErrorInfo, type ReactNode } from "react";

import { ErrorFallback } from "@/components/error-fallback";

interface Props {
  children: ReactNode;
  /** Called once per caught error (for logging/telemetry). */
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface State {
  error: Error | null;
}

/** Catches render errors anywhere below it and shows a recoverable error screen. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    if (this.props.onError) this.props.onError(error, info);
    else console.error("Unhandled UI error", error, info.componentStack);
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    if (this.state.error !== null) {
      return <ErrorFallback error={this.state.error} onRetry={this.reset} />;
    }
    return this.props.children;
  }
}
