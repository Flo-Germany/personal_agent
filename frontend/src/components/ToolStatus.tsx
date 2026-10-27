import { memo, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Circle,
  Loader2,
} from "lucide-react";

import type { TaskStep, ToolStatusData, ToolStatusState } from "../types";
import { AgentAvatar } from "./AgentAvatar";
import { AlertIcon, CheckIcon, CloseIcon, ShieldIcon } from "./icons";

const TONE: Record<ToolStatusState, string> = {
  thinking: "border-fg/[0.06] bg-fg/[0.02] text-fg/80",
  executing: "border-accent/10 bg-accent/[0.04] text-accent-ink",
  waiting: "border-warning/15 bg-warning/[0.04] text-warning-ink",
  success: "border-positive/10 bg-positive/[0.035] text-positive-ink",
  error: "border-danger/15 bg-danger/[0.04] text-danger-ink",
  cancelled: "border-fg/[0.07] bg-fg/[0.02] text-fg/68",
};

const CARD_TONE: Record<ToolStatusState, string> = {
  thinking: "border-fg/[0.07] bg-fg/[0.018]",
  executing: "border-accent/12 bg-accent/[0.03]",
  waiting: "border-warning/15 bg-warning/[0.03]",
  success: "border-positive/10 bg-positive/[0.022]",
  error: "border-danger/15 bg-danger/[0.025]",
  cancelled: "border-fg/[0.07] bg-fg/[0.018]",
};

const ICON_TONE: Record<ToolStatusState, string> = {
  thinking: "border-fg/10 bg-fg/[0.04] text-fg/68",
  executing: "border-accent/15 bg-accent/10 text-accent-ink",
  waiting: "border-warning/20 bg-warning/10 text-warning-ink",
  success: "border-positive/15 bg-positive/10 text-positive-ink",
  error: "border-danger/20 bg-danger/10 text-danger-ink",
  cancelled: "border-fg/10 bg-fg/[0.04] text-fg/68",
};

function StatusIcon({ state }: { state: ToolStatusState }) {
  if (state === "thinking") {
    return (
      <span className="size-3 shrink-0 animate-spin rounded-full border border-fg/20 border-t-white/60" />
    );
  }
  if (state === "executing") {
    return (
      <span className="size-3 shrink-0 animate-spin rounded-full border border-fg/20 border-t-accent" />
    );
  }
  if (state === "waiting") {
    return <ShieldIcon className="size-3 shrink-0" />;
  }
  if (state === "success") {
    return <CheckIcon className="size-3 shrink-0" />;
  }
  if (state === "cancelled") {
    return <CloseIcon className="size-3 shrink-0" />;
  }
  return <AlertIcon className="size-3 shrink-0" />;
}

function TaskIcon({ state }: { state: TaskStep["state"] }) {
  if (state === "running") {
    return <Loader2 className="size-3 shrink-0 animate-spin text-accent-ink" />;
  }
  if (state === "done") {
    return <Check className="size-3 shrink-0 text-positive-ink" />;
  }
  if (state === "failed") {
    return <AlertTriangle className="size-3 shrink-0 text-danger-ink" />;
  }
  if (state === "cancelled") {
    return <CloseIcon className="size-3 shrink-0 text-fg/68" />;
  }
  return <Circle className="size-2.5 shrink-0 text-fg/68" />;
}

function formatTaskDuration(task: TaskStep): string | null {
  if (task.startedAt === undefined || task.finishedAt === undefined) {
    return null;
  }
  const seconds = (task.finishedAt - task.startedAt) / 1000;
  if (seconds < 0.05) {
    return null;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

function formatTotalDuration(status: ToolStatusData): string | null {
  if (status.startedAt === undefined || status.finishedAt === undefined) {
    return null;
  }
  const seconds = (status.finishedAt - status.startedAt) / 1000;
  if (seconds < 0.1) {
    return null;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

type ToolStatusProps = {
  status: ToolStatusData;
};

export const ToolStatus = memo(function ToolStatus({
  status,
}: ToolStatusProps) {
  const tasks = status.tasks ?? [];
  const hasTasks = tasks.length > 0;
  const running = tasks.find((task) => task.state === "running");
  const completed = tasks.filter((task) => task.state === "done").length;
  const failed = tasks.filter((task) => task.state === "failed").length;
  const active = status.state === "thinking" || status.state === "executing";
  const total = Math.max(tasks.length, status.plannedTotal ?? 0);
  const totalDuration = formatTotalDuration(status);
  const [expanded, setExpanded] = useState(hasTasks && active);
  const [simpleExpanded, setSimpleExpanded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const thinkingRef = useRef<HTMLDivElement>(null);

  // Follow the newest reasoning line while the model thinks.
  useEffect(() => {
    const body = thinkingRef.current;
    if (body) {
      body.scrollTop = body.scrollHeight;
    }
  }, [status.thinking]);

  // A live counter makes the hidden reasoning/prompt-evaluation phase legible:
  // the user sees how long the model is taking instead of a spinner with no
  // sense of progress.
  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [active]);

  const liveElapsed =
    active && status.startedAt !== undefined
      ? Math.max(0, now - status.startedAt) / 1000
      : null;
  const liveDuration =
    liveElapsed !== null
      ? `${liveElapsed.toFixed(liveElapsed < 10 ? 1 : 0)}s`
      : null;

  useEffect(() => {
    if (!hasTasks) {
      return;
    }
    if (active) {
      setExpanded(true);
    } else if (failed > 0) {
      // Keep failures visible after the turn finishes.
      setExpanded(true);
    } else {
      setExpanded(false);
    }
  }, [active, failed, hasTasks]);

  useEffect(() => {
    if (status.state !== "error") {
      setSimpleExpanded(false);
    }
  }, [status.state]);

  if (!hasTasks) {
    const expandable =
      status.state === "error" && Boolean(status.detail?.trim());

    const row = (
      <span
        className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-[3px] text-[10.5px] leading-4 transition-colors duration-200 ease-out ${TONE[status.state]}`}
      >
        <StatusIcon state={status.state} />
        <span className="truncate">{status.label}</span>
        {liveDuration && (
          <span className="shrink-0 font-mono tabular-nums text-fg/68">
            {liveDuration}
          </span>
        )}
        {status.state === "error" && status.exitCode !== undefined && (
          <span className="shrink-0 rounded-full bg-danger/10 px-1.5 py-px font-mono text-[9px] text-danger-ink">
            exit {status.exitCode}
          </span>
        )}
        {expandable && (
          <ChevronDown
            className={`size-3 shrink-0 transition-transform duration-200 ease-out ${
              simpleExpanded ? "rotate-180" : ""
            }`}
          />
        )}
      </span>
    );

    return (
      <div className="message-in flex items-start gap-2.5">
        <AgentAvatar size="sm" className="mt-0.5" />
        <div className="min-w-0 flex-1">
          {expandable ? (
            <button
              type="button"
              onClick={() => setSimpleExpanded((value) => !value)}
              aria-expanded={simpleExpanded}
              aria-label={
                simpleExpanded ? "Hide error details" : "Show error details"
              }
              className="max-w-full rounded-full text-left transition-opacity duration-200 ease-out hover:opacity-85 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/40"
            >
              {row}
            </button>
          ) : (
            row
          )}

          {status.thinking && active && (
            <div
              ref={thinkingRef}
              className="mt-1 max-h-28 max-w-xl overflow-y-auto rounded-lg border border-fg/[0.05] bg-fg/[0.015] px-2.5 py-1.5 font-mono text-[9.5px] leading-4 whitespace-pre-wrap break-words text-fg/68"
            >
              {status.thinking}
            </div>
          )}

          {simpleExpanded && (
            <pre className="mt-1 max-h-40 max-w-xl overflow-auto rounded-xl border border-danger/10 bg-danger/[0.025] px-2.5 py-2 font-mono text-[10px] leading-4 whitespace-pre-wrap break-words text-danger-ink">
              {status.detail}
            </pre>
          )}
        </div>
      </div>
    );
  }

  const headline = running?.label ?? status.label;
  const sublineParts: string[] = [];
  if (active) {
    sublineParts.push(`${completed} of ${Math.max(total, 1)} steps`);
    sublineParts.push(liveDuration ? `live · ${liveDuration}` : "live");
  } else {
    sublineParts.push(
      `${tasks.length} step${tasks.length === 1 ? "" : "s"}`,
    );
  }
  if (totalDuration) {
    sublineParts.push(totalDuration);
  }
  const progress = Math.min(
    100,
    Math.round((completed / Math.max(total, 1)) * 100),
  );

  return (
    <div className="message-in flex items-start gap-2.5">
      <AgentAvatar size="sm" className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <div
          className={`overflow-hidden rounded-xl border transition-colors duration-200 ease-out ${CARD_TONE[status.state]}`}
        >
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            aria-label={expanded ? "Hide task list" : "Show task list"}
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-fg/15"
          >
            <span
              className={`flex size-6 shrink-0 items-center justify-center rounded-full border ${ICON_TONE[status.state]}`}
            >
              <StatusIcon state={status.state} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[11.5px] leading-4 font-medium text-fg/80">
                {headline}
              </span>
              <span className="mt-0.5 flex items-center gap-1.5 text-[9.5px] leading-3 text-fg/68">
                <span className="truncate">{sublineParts.join(" · ")}</span>
              </span>
            </span>
            <span className="shrink-0 rounded-full border border-fg/[0.06] bg-sunken px-1.5 py-0.5 font-mono text-[9px] tabular-nums text-fg/68">
              {completed}/{Math.max(total, 1)}
            </span>
            <ChevronDown
              className={`size-3.5 shrink-0 text-fg/68 transition-transform duration-200 ease-out ${
                expanded ? "rotate-180" : ""
              }`}
            />
          </button>

          {active && (
            <div className="mx-2 mb-1.5 h-1 overflow-hidden rounded-full bg-fg/[0.04]">
              <div
                className="h-full rounded-full bg-gradient-to-r from-accent/60 via-accent/55 to-positive/60 transition-[width] duration-500 ease-out"
                style={{ width: `${progress}%` }}
              />
            </div>
          )}

          {status.thinking && active && (
            <div
              ref={thinkingRef}
              className="mx-2 mb-1.5 max-h-28 overflow-y-auto rounded-md border border-fg/[0.04] bg-fg/[0.015] px-2 py-1 font-mono text-[9.5px] leading-4 whitespace-pre-wrap break-words text-fg/68"
            >
              {status.thinking}
            </div>
          )}

          {expanded && (
            <ol className="max-h-64 space-y-0.5 overflow-y-auto px-1 pb-1.5">
              {tasks.map((task, index) => {
                const duration = formatTaskDuration(task);
                const isLast = index === tasks.length - 1;
                return (
                  <li
                    key={task.id}
                    className="flex items-start gap-2 rounded-lg px-1.5 py-1"
                  >
                    <span className="mt-0.5">
                      <TaskIcon state={task.state} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start gap-2">
                        <p
                          className={`min-w-0 flex-1 text-[10.5px] leading-4 ${
                            task.state === "running"
                              ? "text-fg/80"
                              : task.state === "failed"
                                ? "text-danger-ink"
                                : task.state === "cancelled"
                                  ? "text-fg/68 line-through decoration-fg/15"
                                  : "text-fg/68"
                          }`}
                        >
                          {task.label}
                        </p>
                        {duration && (
                          <span className="shrink-0 font-mono text-[8.5px] tabular-nums text-fg/68">
                            {duration}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex items-center gap-2 text-[8.5px] text-fg/68">
                        {task.tool && (
                          <span className="truncate font-mono">
                            {task.tool}
                          </span>
                        )}
                        {task.state === "running" && isLast && (
                          <span className="shrink-0 text-accent-ink">
                            now
                          </span>
                        )}
                      </div>
                      {task.state === "failed" && task.detail && (
                        <pre className="mt-1 max-h-32 overflow-auto rounded-md border border-danger/10 bg-danger/[0.025] px-2 py-1.5 font-mono text-[9px] leading-4 whitespace-pre-wrap break-words text-danger-ink">
                          {task.detail}
                        </pre>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      </div>
    </div>
  );
});
