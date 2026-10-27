import { memo } from "react";
import { Monitor } from "lucide-react";

import type { PendingAction } from "../types";
import { CloseIcon } from "./icons";

type DisplayPickerCardProps = {
  pending: PendingAction;
  busy: boolean;
  onChoose: (display: number) => void;
  onCancel: () => void;
};

export const DisplayPickerCard = memo(function DisplayPickerCard({
  pending,
  busy,
  onChoose,
  onCancel,
}: DisplayPickerCardProps) {
  const displays = [...(pending.displays ?? [])].sort(
    (left, right) =>
      (left.left ?? 0) - (right.left ?? 0) || left.index - right.index,
  );

  return (
    <div className="message-in overflow-hidden rounded-2xl border border-sky-200/15 bg-[linear-gradient(180deg,rgba(56,189,248,0.055),rgba(255,255,255,0.012))] shadow-[0_16px_44px_-28px_rgba(0,0,0,0.9)]">
      <div className="p-3">
        <div className="flex items-start gap-2">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full border border-sky-200/20 bg-sky-300/10 text-sky-100/90">
            <Monitor className="size-3" strokeWidth={2} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-[11.5px] leading-4 font-medium tracking-wide text-sky-50">
                Which display should I analyze?
              </h2>
              <span className="shrink-0 rounded-full border border-sky-200/15 bg-sky-300/[0.06] px-1.5 py-px text-[8px] font-medium tracking-wider text-sky-100/50 uppercase">
                {displays.length} displays
              </span>
            </div>
            <p className="mt-0.5 text-[10px] leading-4 text-white/56">
              {pending.question
                ? `“${pending.question}”`
                : "Choose the screen the agent should look at."}
            </p>
          </div>
        </div>

        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {displays.map((display) => (
            <button
              key={display.index}
              type="button"
              onClick={() => onChoose(display.index)}
              disabled={busy}
              aria-label={`Analyze display ${display.index}`}
              className="group overflow-hidden rounded-xl border border-white/[0.07] bg-white/[0.02] p-1.5 text-left transition-all duration-200 ease-out hover:border-sky-300/30 hover:bg-sky-300/[0.05] focus-visible:border-sky-300/40 focus-visible:outline-none active:scale-[0.995] disabled:cursor-not-allowed disabled:opacity-40"
            >
              <div className="relative aspect-video overflow-hidden rounded-lg border border-white/[0.05] bg-black/50">
                {display.preview ? (
                  <img
                    src={display.preview}
                    alt={`Preview of display ${display.index}`}
                    className="size-full object-cover"
                    draggable={false}
                  />
                ) : (
                  <span className="flex size-full items-center justify-center text-white/48">
                    <Monitor className="size-6" strokeWidth={1.5} />
                  </span>
                )}

                <span className="absolute bottom-1 left-1 rounded bg-black/65 px-1.5 py-px text-[8.5px] font-medium text-white/80 backdrop-blur-sm">
                  Display {display.index}
                </span>

                {display.primary && (
                  <span className="absolute top-1 right-1 rounded-full border border-emerald-200/25 bg-emerald-300/15 px-1.5 py-px text-[7.5px] font-semibold tracking-wider text-emerald-100 uppercase backdrop-blur-sm">
                    Primary
                  </span>
                )}
              </div>

              <div className="mt-1.5 flex items-baseline justify-between gap-2 px-0.5">
                <span className="truncate text-[11px] font-medium text-white/70 group-hover:text-white/85">
                  Display {display.index}
                </span>
                <span className="shrink-0 font-mono text-[9px] text-white/54">
                  {display.width} × {display.height}
                </span>
              </div>
              {display.position && (
                <p className="mt-0.5 px-0.5 pb-0.5 text-[9px] leading-3 text-white/48">
                  {display.position}
                </p>
              )}
            </button>
          ))}
        </div>

        <div className="mt-3 flex">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 text-[11.5px] font-medium text-white/55 transition-all duration-200 ease-out hover:border-rose-300/25 hover:bg-rose-400/10 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <CloseIcon className="size-3" />
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
});
