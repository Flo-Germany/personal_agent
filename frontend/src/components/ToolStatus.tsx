import { memo, useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";

import type { ToolStatusData, ToolStatusState } from "../types";
import { AgentAvatar } from "./AgentAvatar";
import { AlertIcon, CheckIcon, CloseIcon, ShieldIcon } from "./icons";

const TONE: Record<ToolStatusState, string> = {
  thinking: "border-white/[0.06] bg-white/[0.02] text-white/45",
  executing: "border-violet-300/10 bg-violet-300/[0.04] text-violet-100/70",
  waiting: "border-amber-200/15 bg-amber-300/[0.04] text-amber-100/75",
  success: "border-emerald-300/10 bg-emerald-300/[0.035] text-emerald-200/65",
  error: "border-rose-300/15 bg-rose-400/[0.04] text-rose-200/80",
  cancelled: "border-white/[0.07] bg-white/[0.02] text-white/40",
};

function StatusIcon({ state }: { state: ToolStatusState }) {
  if (state === "thinking") {
    return (
      <span className="size-3 shrink-0 animate-spin rounded-full border border-white/20 border-t-white/60" />
    );
  }
  if (state === "executing") {
    return (
      <span className="size-3 shrink-0 animate-spin rounded-full border border-white/20 border-t-violet-200/80" />
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

type ToolStatusProps = {
  status: ToolStatusData;
};

export const ToolStatus = memo(function ToolStatus({
  status,
}: ToolStatusProps) {
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (status.state !== "error") {
      setExpanded(false);
    }
  }, [status.state]);

  const expandable = status.state === "error" && Boolean(status.detail?.trim());

  const row = (
    <span
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-[3px] text-[10.5px] leading-4 transition-colors duration-200 ease-out ${TONE[status.state]}`}
    >
      <StatusIcon state={status.state} />
      <span className="truncate">{status.label}</span>
      {status.state === "error" && status.exitCode !== undefined && (
        <span className="shrink-0 rounded-full bg-rose-400/10 px-1.5 py-px font-mono text-[9px] text-rose-200/65">
          exit {status.exitCode}
        </span>
      )}
      {expandable && (
        <ChevronDown
          className={`size-3 shrink-0 transition-transform duration-200 ease-out ${
            expanded ? "rotate-180" : ""
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
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            aria-label={expanded ? "Hide error details" : "Show error details"}
            className="max-w-full rounded-full text-left transition-opacity duration-200 ease-out hover:opacity-85 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-300/40"
          >
            {row}
          </button>
        ) : (
          row
        )}

        {expanded && (
          <pre className="mt-1 max-h-40 max-w-xl overflow-auto rounded-xl border border-rose-300/10 bg-rose-400/[0.025] px-2.5 py-2 font-mono text-[10px] leading-4 whitespace-pre-wrap break-words text-rose-100/55">
            {status.detail}
          </pre>
        )}
      </div>
    </div>
  );
});
