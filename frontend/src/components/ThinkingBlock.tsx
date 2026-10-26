import { memo, useEffect, useRef, useState } from "react";
import { Brain, ChevronDown } from "lucide-react";

type ThinkingBlockProps = {
  text: string;
  durationMs?: number;
  tokens?: number;
  /** True while reasoning tokens are still arriving. */
  streaming?: boolean;
  /** Wall-clock start of the reasoning phase, for the live counter. */
  startedAt?: number;
};

function formatSeconds(ms: number): string {
  const seconds = ms / 1000;
  if (seconds < 1) {
    return `${Math.max(1, Math.round(ms))} ms`;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
}

/**
 * The "tiny button" that makes the model's reasoning inspectable.
 *
 * Collapsed it is a single subdued pill with the thinking time; expanding it
 * reveals the streamed reasoning. While the model is still thinking the pill
 * counts up, so a slow first token never looks like a stuck request.
 */
export const ThinkingBlock = memo(function ThinkingBlock({
  text,
  durationMs,
  tokens,
  streaming = false,
  startedAt,
}: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(streaming);
  const [now, setNow] = useState(() => Date.now());
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!streaming) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [streaming]);

  // Follow the newest reasoning while it streams.
  useEffect(() => {
    if (streaming && expanded) {
      const body = bodyRef.current;
      if (body) {
        body.scrollTop = body.scrollHeight;
      }
    }
  }, [expanded, streaming, text]);

  // Collapse once the reasoning is complete: the button is the summary.
  useEffect(() => {
    if (!streaming) {
      setExpanded(false);
    }
  }, [streaming]);

  const elapsed =
    streaming && startedAt !== undefined
      ? Math.max(0, now - startedAt)
      : durationMs;

  if (!text.trim() && !streaming) {
    return null;
  }

  const label = streaming
    ? elapsed !== undefined
      ? `Thinking ${formatSeconds(elapsed)}`
      : "Thinking"
    : elapsed !== undefined
      ? `Thought for ${formatSeconds(elapsed)}`
      : "Thought process";

  return (
    <div className="mt-0.5 mb-1.5 min-w-0">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={expanded ? "Hide thought process" : "Show thought process"}
        className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-white/[0.05] bg-white/[0.02] px-2 py-[2px] text-[10px] leading-4 text-white/35 transition-colors duration-150 ease-out hover:border-white/[0.1] hover:text-white/55 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/20"
      >
        {streaming ? (
          <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-violet-300/80" />
        ) : (
          <Brain className="size-2.5 shrink-0 text-violet-200/45" />
        )}
        <span className="truncate font-mono tabular-nums">{label}</span>
        {tokens !== undefined && tokens > 0 && (
          <span className="shrink-0 font-mono tabular-nums text-white/25">
            · ~{tokens} tok
          </span>
        )}
        <ChevronDown
          className={`size-2.5 shrink-0 transition-transform duration-150 ease-out ${
            expanded ? "rotate-180" : ""
          }`}
        />
      </button>

      {expanded && (
        <div className="mt-1 max-w-xl overflow-hidden rounded-lg border border-white/[0.05] bg-white/[0.015]">
          <div
            ref={bodyRef}
            className="max-h-56 overflow-y-auto px-2.5 py-1.5 font-mono text-[10px] leading-4 whitespace-pre-wrap break-words text-white/40"
          >
            {text.trim() ? text : "…"}
          </div>
        </div>
      )}
    </div>
  );
});
