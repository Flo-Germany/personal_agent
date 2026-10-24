import { memo } from "react";

import type { StatusKind } from "../types";
import { AlertIcon, CheckIcon, ShieldIcon } from "./icons";

type StatusPillProps = {
  kind: StatusKind;
  content: string;
  active?: boolean;
};

const TONE: Record<StatusKind, string> = {
  thinking: "text-white/45",
  tool: "text-white/55",
  "result-ok": "text-emerald-300/80",
  "result-error": "text-rose-300/80",
  approval: "text-amber-200/80",
};

function StatusIcon({ kind, active }: { kind: StatusKind; active: boolean }) {
  switch (kind) {
    case "thinking":
      return active ? (
        <span className="size-3 shrink-0 animate-spin rounded-full border border-white/20 border-t-white/70" />
      ) : (
        <CheckIcon className="size-3 shrink-0 text-emerald-300/70" />
      );
    case "tool":
      return (
        <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-violet-300/80" />
      );
    case "result-ok":
      return <CheckIcon className="size-3 shrink-0" />;
    case "result-error":
      return <AlertIcon className="size-3 shrink-0" />;
    case "approval":
      return <ShieldIcon className="size-3 shrink-0" />;
  }
}

export const StatusPill = memo(function StatusPill({
  kind,
  content,
  active = false,
}: StatusPillProps) {
  return (
    <div className="message-in flex justify-center">
      <div
        className={`inline-flex max-w-[92%] items-center gap-1.5 rounded-full border border-white/[0.06] bg-white/[0.025] px-2.5 py-1 text-[11px] leading-4 ${TONE[kind]}`}
      >
        <StatusIcon kind={kind} active={active} />
        <span className="break-words">{content}</span>
      </div>
    </div>
  );
});
