import { memo } from "react";
import { ArrowRight, Check, ListChecks, ShieldCheck } from "lucide-react";

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
  const planText = (pending.message ?? "").trim();
  const actionLabel =
    actions.length === 1 ? "Starting with" : `Starting with ${actions.length} actions`;

  return (
    <div className="message-in overflow-hidden rounded-2xl border border-amber-200/15 bg-[linear-gradient(180deg,rgba(251,191,36,0.055),rgba(255,255,255,0.012))] shadow-[0_18px_50px_-28px_rgba(0,0,0,0.9)]">
      <div className="p-3.5">
        <div className="flex items-start gap-2.5">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-amber-200/20 bg-amber-300/10 text-amber-100/90">
            <ShieldIcon className="size-3.5" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-[12px] font-medium tracking-wide text-amber-50">
                Approval required
              </h2>
              <span className="shrink-0 rounded-full border border-amber-200/15 bg-amber-300/[0.06] px-2 py-0.5 text-[8.5px] font-medium tracking-wider text-amber-100/50 uppercase">
                1 approval
              </span>
            </div>
            <p className="mt-1 text-[11px] leading-5 text-white/45">
              Review the steps. Approving lets the agent finish the rest of
              this request automatically.
            </p>
          </div>
        </div>

        <section className="mt-3.5">
          <div className="flex items-center gap-1.5">
            <span className="flex size-4 items-center justify-center rounded-full bg-emerald-300/10 text-emerald-200/70">
              <Check className="size-2.5" strokeWidth={2.5} />
            </span>
            <p className="text-[9.5px] font-medium tracking-wider text-white/30 uppercase">
              {actionLabel}
            </p>
          </div>

          <ol className="mt-2 space-y-1.5">
            {actions.map((action, index) => (
              <li
                key={`${action.tool}-${action.description}-${index}`}
                className="flex items-start gap-2 rounded-xl border border-white/[0.06] bg-black/25 px-2.5 py-2"
              >
                <span className="mt-px flex size-4 shrink-0 items-center justify-center rounded-full border border-emerald-300/15 bg-emerald-300/[0.06] font-mono text-[8.5px] text-emerald-100/60">
                  {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="break-words whitespace-pre-wrap text-[11.5px] leading-5 text-white/80">
                    {action.description}
                  </p>
                  <p className="mt-0.5 font-mono text-[9px] text-white/25">
                    {action.tool}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </section>

        {plan.length > 0 ? (
          <section className="mt-3.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <span className="flex size-4 items-center justify-center rounded-full bg-violet-300/10 text-violet-200/70">
                  <ListChecks className="size-2.5" />
                </span>
                <p className="text-[9.5px] font-medium tracking-wider text-white/30 uppercase">
                  Planned sequence
                </p>
              </div>
              <span className="text-[8.5px] text-white/20">
                full sequence · {plan.length} steps
              </span>
            </div>

            <ol className="relative mt-2 max-h-44 space-y-1 overflow-y-auto pl-0.5 pr-1">
              {plan.map((step, index) => (
                <li
                  key={`${step}-${index}`}
                  className="relative flex items-start gap-2"
                >
                  <span className="mt-0 flex size-4 shrink-0 items-center justify-center rounded-full border border-violet-200/10 bg-violet-300/[0.05] font-mono text-[8px] text-violet-100/45">
                    {index + 1}
                  </span>
                  <p className="min-w-0 flex-1 pb-1 text-[10.5px] leading-4 text-white/45">
                    {step}
                  </p>
                </li>
              ))}
            </ol>
          </section>
        ) : planText ? (
          <section className="mt-3.5">
            <div className="flex items-center gap-1.5">
              <span className="flex size-4 items-center justify-center rounded-full bg-violet-300/10 text-violet-200/70">
                <ListChecks className="size-2.5" />
              </span>
              <p className="text-[9.5px] font-medium tracking-wider text-white/30 uppercase">
                Plan
              </p>
            </div>
            <p className="mt-1.5 max-h-28 overflow-auto rounded-xl border border-white/[0.05] bg-white/[0.015] px-2.5 py-2 text-[10.5px] leading-4 whitespace-pre-wrap text-white/40">
              {planText}
            </p>
          </section>
        ) : (
          <div className="mt-3.5 flex items-start gap-2 rounded-xl border border-white/[0.05] bg-white/[0.015] px-2.5 py-2">
            <ArrowRight className="mt-0.5 size-3 shrink-0 text-violet-200/45" />
            <p className="text-[10.5px] leading-4 text-white/35">
              The agent will continue with any remaining steps of this request
              automatically.
            </p>
          </div>
        )}

        <div className="mt-3 flex items-start gap-2 rounded-xl border border-amber-200/10 bg-amber-300/[0.025] px-2.5 py-2">
          <ShieldCheck className="mt-px size-3 shrink-0 text-amber-100/55" />
          <p className="text-[9.5px] leading-4 text-amber-100/40">
            One approval covers the whole request. No further prompts between
            steps.
          </p>
        </div>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={onApprove}
            disabled={busy}
            className="flex h-9 flex-1 items-center justify-center gap-1.5 rounded-xl bg-emerald-300 text-[12px] font-semibold text-[#0c0c0c] transition-all duration-200 ease-out hover:bg-emerald-200 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Check className="size-3.5" strokeWidth={2.5} />
            Approve all
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
    </div>
  );
});
