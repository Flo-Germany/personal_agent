import { memo, useState } from "react";
import { ChevronDown, Zap } from "lucide-react";

import { metricsDetails, metricsSummary } from "../lib/metrics";
import type { TokenMetrics } from "../types";

type TokenStatsProps = {
  metrics: TokenMetrics;
  streaming?: boolean;
};

/** Compact tokens/second footer under an assistant answer. */
export const TokenStats = memo(function TokenStats({
  metrics,
  streaming = false,
}: TokenStatsProps) {
  const [expanded, setExpanded] = useState(false);
  const summary = metricsSummary(metrics);
  const details = metricsDetails(metrics);

  if (!summary) {
    return null;
  }

  return (
    <div className="mt-1.5 min-w-0">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={expanded ? "Hide token details" : "Show token details"}
        disabled={details.length === 0}
        className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-fg/[0.05] bg-fg/[0.02] px-2 py-[2px] text-[10px] leading-4 text-fg/68 transition-colors duration-150 ease-out hover:border-fg/[0.1] hover:text-fg/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/20 disabled:hover:border-fg/[0.05] disabled:hover:text-fg/68"
      >
        {streaming ? (
          <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-positive/80" />
        ) : (
          <Zap className="size-2.5 shrink-0 text-warning-ink" />
        )}
        <span className="truncate font-mono tabular-nums">{summary}</span>
        {streaming && (
          <span className="shrink-0 text-[9px] uppercase tracking-wide text-positive-ink">
            · live
          </span>
        )}
        {details.length > 0 && (
          <ChevronDown
            className={`size-2.5 shrink-0 transition-transform duration-150 ease-out ${
              expanded ? "rotate-180" : ""
            }`}
          />
        )}
      </button>

      {expanded && details.length > 0 && (
        <dl className="mt-1 grid max-w-sm grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded-lg border border-fg/[0.05] bg-fg/[0.015] px-2.5 py-1.5 font-mono text-[9.5px] leading-4 text-fg/68">
          {details.map((detail) => (
            <div key={detail.label} className="contents">
              <dt className="text-fg/68">{detail.label}</dt>
              <dd className="truncate text-right tabular-nums text-fg/68">
                {detail.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
});
