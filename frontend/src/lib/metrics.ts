import type { TokenMetrics } from "../types";

function formatCount(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const seconds = ms / 1000;
  return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
}

/** One-line summary for the answer footer, e.g. `42.3 tok/s · 128 tok · 3.1 s`. */
export function metricsSummary(metrics: TokenMetrics): string {
  const parts: string[] = [];

  if (metrics.tokens_per_second !== undefined) {
    parts.push(`${metrics.tokens_per_second.toFixed(1)} tok/s`);
  }
  if (metrics.completion_tokens !== undefined) {
    parts.push(`${formatCount(metrics.completion_tokens)} tok`);
  }
  if (metrics.eval_ms !== undefined) {
    parts.push(formatDuration(metrics.eval_ms));
  } else if (metrics.total_ms !== undefined) {
    parts.push(formatDuration(metrics.total_ms));
  }

  return parts.join(" · ");
}

export type MetricDetail = {
  label: string;
  value: string;
};

/** Full breakdown shown when the summary is expanded. */
export function metricsDetails(metrics: TokenMetrics): MetricDetail[] {
  const details: MetricDetail[] = [];

  if (metrics.model) {
    details.push({ label: "Model", value: metrics.model });
  }
  if (metrics.first_token_ms !== undefined) {
    details.push({
      // With a thinking model this is the first *answer* token; the reasoning
      // phase is reported separately above.
      label: metrics.thinking_ms !== undefined ? "First answer token" : "First token",
      value: formatDuration(metrics.first_token_ms),
    });
  }
  if (metrics.thinking_ms !== undefined) {
    const tokens =
      metrics.thinking_tokens !== undefined && metrics.thinking_tokens > 0
        ? ` (~${formatCount(metrics.thinking_tokens)} tok)`
        : "";
    details.push({
      label: "Thinking",
      value: `${formatDuration(metrics.thinking_ms)}${tokens}`,
    });
  }
  if (metrics.completion_tokens !== undefined) {
    details.push({
      label: "Generated",
      value: `${formatCount(metrics.completion_tokens)} tokens`,
    });
  }
  if (metrics.prompt_tokens !== undefined) {
    details.push({
      label: "Prompt",
      value: `${formatCount(metrics.prompt_tokens)} tokens`,
    });
  }
  if (metrics.prompt_eval_ms !== undefined) {
    // Ollama reports the full prompt token count but only the *uncached*
    // evaluation time. Deriving a rate from the two would show absurd values
    // such as 100k tok/s once the prompt cache is warm, so show time only.
    details.push({
      label: "Prompt eval",
      value: formatDuration(metrics.prompt_eval_ms),
    });
  }
  if (metrics.eval_ms !== undefined) {
    const speed =
      metrics.tokens_per_second !== undefined
        ? ` (${metrics.tokens_per_second.toFixed(1)} tok/s)`
        : "";
    details.push({
      label: "Generation",
      value: `${formatDuration(metrics.eval_ms)}${speed}`,
    });
  }
  if (metrics.load_ms !== undefined) {
    details.push({
      label: "Model load",
      value: formatDuration(metrics.load_ms),
    });
  }
  if (metrics.total_ms !== undefined) {
    details.push({ label: "Total", value: formatDuration(metrics.total_ms) });
  }

  return details;
}
