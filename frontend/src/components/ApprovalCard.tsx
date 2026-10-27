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
    <div data-approval-card className="message-in overflow-hidden rounded-2xl border border-warning/15 bg-[linear-gradient(180deg,rgba(251,191,36,0.05),rgba(255,255,255,0.012))] shadow-[0_16px_44px_-28px_var(--compa-shadow-strong)]">
      <div className="p-3">
        <div className="flex items-start gap-2">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full border border-warning/20 bg-warning/10 text-warning-ink">
            <ShieldIcon className="size-3" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-[11.5px] leading-4 font-medium tracking-wide text-warning-ink">
                Approval required
              </h2>
              <span className="shrink-0 rounded-full border border-fg/10 bg-fg/[0.04] px-1.5 py-px text-[8px] font-medium tracking-wider text-fg/75 uppercase">
                1 approval
              </span>
            </div>
            <p className="mt-0.5 text-[10px] leading-4 text-fg/68">
              {pending.auto_approve_remaining
                ? "One approval runs every step below."
                : "Review the exact actions below."}
            </p>
          </div>
        </div>

        <ol className="relative mt-3 space-y-2">
          <span className="absolute top-5 bottom-5 left-[8.5px] w-px bg-fg/[0.06]" />

          <li className="relative flex gap-2.5">
            <span className="z-10 flex size-[18px] shrink-0 items-center justify-center rounded-full border border-positive/35 bg-positive/20 text-positive-ink">
              <Check className="size-2.5" strokeWidth={2.5} />
            </span>
            <div className="min-w-0 flex-1 pb-0.5">
              <div className="flex items-center gap-1.5">
                <span className="text-[8.5px] font-semibold tracking-[0.12em] text-fg/75 uppercase">
                  Now
                </span>
                <span className="text-[8.5px] text-fg/68">
                  runs immediately
                </span>
              </div>

              <div className="mt-1 space-y-1">
                {actions.map((action, index) => (
                  <div
                    key={`${action.tool}-${action.description}-${index}`}
                    className="rounded-lg border border-positive/10 bg-positive/[0.025] px-2 py-1.5"
                  >
                    <p className="break-words whitespace-pre-wrap text-[11px] leading-4 text-fg/80">
                      {action.description}
                    </p>
                    <span className="mt-0.5 inline-block rounded bg-sunken px-1 py-px font-mono text-[8px] text-fg/68">
                      {action.tool}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </li>

          {following.map((step, index) => (
            <li key={`${step}-${index}`} className="relative flex gap-2.5">
              <span className="z-10 flex size-[18px] shrink-0 items-center justify-center rounded-full border border-fg/10 bg-fg/[0.05] font-mono text-[9px] text-fg/78">
                {index + 2}
              </span>
              <div className="min-w-0 flex-1 pb-0.5">
                {index === 0 && (
                  <span className="text-[8.5px] font-semibold tracking-[0.12em] text-fg/75 uppercase">
                    Then
                  </span>
                )}
                <p className="mt-0.5 text-[10.5px] leading-4 text-fg/68">
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
            className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg bg-positive text-[11.5px] font-semibold text-white transition-all duration-200 ease-out hover:brightness-110 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Check className="size-3" strokeWidth={2.5} />
            Approve all
          </button>
          <button
            type="button"
            onClick={onDeny}
            disabled={busy}
            className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-fg/10 text-[11.5px] font-medium text-fg/68 transition-all duration-200 ease-out hover:border-danger/25 hover:bg-danger/10 hover:text-danger-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            <CloseIcon className="size-3" />
            Deny
          </button>
        </div>
      </div>
    </div>
  );
});
