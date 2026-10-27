import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

type ErrorBoundaryProps = { children: ReactNode };
type ErrorBoundaryState = { error: Error | null };

/**
 * Keeps one bad payload from blanking the whole window. Without this, a single
 * unexpected field in a REST response unmounts the React tree to a white page.
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("The interface hit an unexpected error.", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex h-dvh w-full items-center justify-center bg-canvas p-6">
          <div className="max-w-sm rounded-2xl border border-fg/[0.08] bg-fg/[0.02] p-5 text-center">
            <p className="text-[13px] font-medium text-fg/90">
              Something in the interface broke
            </p>
            <p className="mt-1.5 text-[11px] leading-5 text-fg/68">
              {this.state.error.message || "Unknown rendering error"}
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="mt-4 rounded-lg border border-fg/10 bg-fg/[0.04] px-3 py-1.5 text-[11px] text-fg/80 transition-colors hover:bg-fg/[0.08]"
            >
              Reload the window
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
