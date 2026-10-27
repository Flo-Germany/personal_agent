import { memo } from "react";
import { Check } from "lucide-react";

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
  const actions = pending.actions;
  const plan = pending.plan ?? [];
  const following = plan.slice(actions.length);

  return (
    <div data-approval-card className="message-in overflow-hidden rounded-2xl border border-amber-200/15 bg-[linear-gradient(180deg,rgba(251,191,36,0.05),rgba(255,255,255,0.012))] shadow-[0_16px_44px_-28px_rgba(0,0,0,0.9)]">
      <div className="p-3">
        <div className="flex items-start gap-2">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full border border-amber-200/20 bg-amber-300/10 text-amber-100/90">
            <ShieldIcon className="size-3" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-[11.5px] leading-4 font-medium tracking-wide text-amber-50">
                Approval required
              </h2>
              <span className="shrink-0 rounded-full border border-amber-200/15 bg-amber-300/[0.06] px-1.5 py-px text-[8px] font-medium tracking-wider text-amber-100/50 uppercase">
                1 approval
              </span>
            </div>
            <p className="mt-0.5 text-[10px] leading-4 text-white/56">
              {pending.auto_approve_remaining
                ? "One approval runs every step below."
                : "Review the exact actions below."}
            </p>
          </div>
        </div>

        <ol className="relative mt-3 space-y-2">
          <span className="absolute top-5 bottom-5 left-[8.5px] w-px bg-white/[0.06]" />

          <li className="relative flex gap-2.5">
            <span className="z-10 flex size-[18px] shrink-0 items-center justify-center rounded-full border border-emerald-300/25 bg-emerald-300/15 text-emerald-200">
              <Check className="size-2.5" strokeWidth={2.5} />
            </span>
            <div className="min-w-0 flex-1 pb-0.5">
              <div className="flex items-center gap-1.5">
                <span className="text-[8.5px] font-semibold tracking-[0.12em] text-emerald-200/55 uppercase">
                  Now
                </span>
                <span className="text-[8.5px] text-white/48">
                  runs immediately
                </span>
              </div>

              <div className="mt-1 space-y-1">
                {actions.map((action, index) => (
                  <div
                    key={`${action.tool}-${action.description}-${index}`}
                    className="rounded-lg border border-emerald-300/10 bg-emerald-300/[0.025] px-2 py-1.5"
                  >
                    <p className="break-words whitespace-pre-wrap text-[11px] leading-4 text-white/75">
                      {action.description}
                    </p>
                    <span className="mt-0.5 inline-block rounded bg-black/20 px-1 py-px font-mono text-[8px] text-white/48">
                      {action.tool}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </li>

          {following.map((step, index) => (
            <li key={`${step}-${index}`} className="relative flex gap-2.5">
              <span className="z-10 flex size-[18px] shrink-0 items-center justify-center rounded-full border border-violet-200/15 bg-violet-300/[0.06] font-mono text-[8px] text-violet-100/50">
                {index + 2}
              </span>
              <div className="min-w-0 flex-1 pb-0.5">
                {index === 0 && (
                  <span className="text-[8.5px] font-semibold tracking-[0.12em] text-violet-200/72 uppercase">
                    Then
                  </span>
                )}
                <p className="mt-0.5 text-[10.5px] leading-4 text-white/50">
                  {step}
                </p>
              </div>
            </li>
          ))}
        </ol>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg bg-emerald-300 text-[11.5px] font-semibold text-[#0c0c0c] transition-all duration-200 ease-out hover:bg-emerald-200 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Check className="size-3" strokeWidth={2.5} />
            Approve all
          </button>
          <button
            type="button"
            onClick={onDeny}
            disabled={busy}
            className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 text-[11.5px] font-medium text-white/55 transition-all duration-200 ease-out hover:border-rose-300/25 hover:bg-rose-400/10 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <CloseIcon className="size-3" />
            Deny
          </button>
        </div>
      </div>
    </div>
  );
});
