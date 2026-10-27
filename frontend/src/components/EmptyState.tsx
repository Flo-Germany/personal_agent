import { memo } from "react";

import { QUICK_PROMPTS } from "../lib/prompts";
import { AgentAvatar } from "./AgentAvatar";
import { ArrowRightIcon } from "./icons";

type EmptyStateProps = {
  onPrompt: (prompt: string) => void;
  disabled: boolean;
};

export const EmptyState = memo(function EmptyState({
  onPrompt,
  disabled,
}: EmptyStateProps) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center px-5 py-10">
      <div className="relative mb-5">
        <div className="absolute inset-0 -z-10 scale-[1.9] rounded-full bg-[radial-gradient(circle,rgba(10,132,255,0.2),transparent_70%)] blur-xl" />
        <AgentAvatar size="lg" />
      </div>

      <h1 className="text-center text-[19px] leading-tight font-medium tracking-tight text-fg/95">
        How can I help?
      </h1>
      <p className="mt-1.5 max-w-xs text-center text-[12px] leading-5 text-fg/68">
        Search your files, plan something, or ask a question. Nothing changes
        without your approval.
      </p>

      <div className="mt-7 grid w-full max-w-md min-w-0 grid-cols-1 gap-2 min-[420px]:grid-cols-2">
        {QUICK_PROMPTS.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={() => onPrompt(item.prompt)}
            disabled={disabled}
            className="group flex min-w-0 items-center justify-between gap-2 rounded-full border border-fg/[0.08] bg-fg/[0.025] px-3.5 py-2.5 text-left text-[12px] text-fg/74 transition-all duration-200 ease-out hover:border-fg/[0.14] hover:bg-fg/[0.06] hover:text-fg/90 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <span className="truncate">{item.label}</span>
            <ArrowRightIcon className="size-3.5 shrink-0 text-fg/68 transition-all duration-200 ease-out group-hover:translate-x-0.5 group-hover:text-fg/90" />
          </button>
        ))}
      </div>
    </div>
  );
});
