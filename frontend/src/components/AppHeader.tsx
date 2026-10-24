import type { Identity } from "../types";
import { hideToTray } from "../lib/window";
import { AgentAvatar } from "./AgentAvatar";
import { CloseIcon, RefreshIcon, TrashIcon } from "./icons";

type AppHeaderProps = {
  identity: Identity | null;
  isWorking: boolean;
  isBusy: boolean;
  canClear: boolean;
  onRefresh: () => void;
  onClear: () => void;
};

export function AppHeader({
  identity,
  isWorking,
  isBusy,
  canClear,
  onRefresh,
  onClear,
}: AppHeaderProps) {
  return (
    <header
      data-tauri-drag-region="deep"
      className="flex shrink-0 select-none items-center gap-2.5 border-b border-white/[0.055] px-4 py-3 sm:px-5"
    >
      <AgentAvatar size="md" />

      <div className="min-w-0 flex-1">
        <h1 className="truncate text-[13.5px] font-medium tracking-tight text-white/95">
          AI Agent
        </h1>
        <p className="truncate text-[10.5px] text-white/35">
          {identity ? `${identity.model} · Local` : "Local · Private"}
        </p>
      </div>

      <span
        className={`hidden items-center gap-1.5 rounded-full border border-white/[0.06] px-2 py-1 text-[10px] text-white/40 min-[400px]:flex ${
          isWorking ? "bg-violet-300/[0.07]" : "bg-white/[0.025]"
        }`}
      >
        <span
          className={`size-1.5 rounded-full ${
            isWorking
              ? "animate-pulse bg-violet-300"
              : "bg-emerald-300/90"
          }`}
        />
        {isWorking ? "Working" : "Ready"}
      </span>

      <button
        type="button"
        onClick={onRefresh}
        disabled={isBusy}
        aria-label="Reload conversation"
        title="Reload conversation"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white/35 transition-all duration-200 ease-out hover:bg-white/[0.06] hover:text-white/75 disabled:cursor-not-allowed disabled:opacity-30"
      >
        <RefreshIcon
          className={`size-4 ${isBusy ? "animate-spin [animation-duration:1.2s]" : ""}`}
        />
      </button>

      <button
        type="button"
        onClick={onClear}
        disabled={isBusy || !canClear}
        aria-label="Clear this chat (knowledge base is kept)"
        title="Clear this chat (knowledge base is kept)"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white/35 transition-all duration-200 ease-out hover:bg-white/[0.06] hover:text-white/75 disabled:cursor-not-allowed disabled:opacity-30"
      >
        <TrashIcon className="size-4" />
      </button>

      <button
        type="button"
        onClick={() => void hideToTray()}
        aria-label="Hide to tray"
        title="Hide to tray"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white/35 transition-all duration-200 ease-out hover:bg-rose-400/10 hover:text-rose-200"
      >
        <CloseIcon className="size-4" />
      </button>
    </header>
  );
}
