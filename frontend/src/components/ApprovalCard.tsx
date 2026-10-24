import { memo } from "react";

import type { PendingAction } from "../types";
import { CloseIcon, ShieldIcon } from "./icons";

type ApprovalCardProps = {
  pending: PendingAction;
  busy: boolean;
  onApprove: () => void;
  onDeny: () => void;
};

export const ApprovalCard = memo(function ApprovalCard({
  pending,
  busy,
  onApprove,
  onDeny,
}: ApprovalCardProps) {
  return (
    <div className="message-in rounded-2xl border border-amber-200/15 bg-amber-300/[0.035] p-3.5">
      <div className="flex items-center gap-2">
        <span className="flex size-6 items-center justify-center rounded-full border border-amber-200/20 bg-amber-300/10 text-amber-200">
          <ShieldIcon className="size-3.5" />
        </span>
        <h2 className="text-[12px] font-medium tracking-wide text-amber-100">
          Approval required
        </h2>
      </div>

      <p className="mt-2 text-[11px] leading-5 text-white/45">
        The agent wants to change something on your system. Review the exact
        actions before continuing.
      </p>

      <ul className="mt-3 space-y-1.5">
        {pending.actions.map((action) => (
          <li
            key={`${action.tool}-${action.description}`}
            className="rounded-xl border border-white/[0.07] bg-black/25 px-3 py-2"
          >
            <p className="break-words text-[12px] leading-5 text-white/80">
              {action.description}
            </p>
            <p className="mt-0.5 font-mono text-[10px] text-white/30">
              {action.tool}
            </p>
          </li>
        ))}
      </ul>

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={onApprove}
          disabled={busy}
          className="h-9 flex-1 rounded-xl bg-emerald-300 text-[12px] font-semibold text-[#0c0c0c] transition-all duration-200 ease-out hover:bg-emerald-200 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={onDeny}
          disabled={busy}
          className="flex h-9 flex-1 items-center justify-center gap-1.5 rounded-xl border border-white/10 text-[12px] font-medium text-white/55 transition-all duration-200 ease-out hover:border-rose-300/25 hover:bg-rose-400/10 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <CloseIcon className="size-3.5" />
          Deny
        </button>
      </div>
    </div>
  );
});
