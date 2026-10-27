import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUp,
  Loader2,
  Search,
  Settings2,
  Square,
  Trash2,
  X,
} from "lucide-react";

import { hidePalette, onSummon, prettyShortcut, setPaletteHeight } from "../lib/desktop";
import { shortModelName } from "../lib/display";
import { CONTENT_ENTRANCE, EASE_IN, EASE_OUT, SPRING } from "../lib/motion";
import type { DesktopBackdrop } from "../lib/desktop";
import { QUICK_PROMPTS } from "../lib/prompts";
import type { ChatMessage, Identity, PendingAction } from "../types";
import { ApprovalCard } from "./ApprovalCard";
import { DisplayPickerCard } from "./DisplayPickerCard";
import { MarkdownContent } from "./MarkdownContent";

export const PALETTE_COLLAPSED = 136;
export const PALETTE_EXPANDED = 620;
export const PALETTE_SETTINGS = 748;
/** Height of the chrome around the scrolling answer body: header, footer, status bar. */
const PALETTE_CHROME = 152;

type ModernPaletteProps = {
  messages: ChatMessage[];
  identity: Identity | null;
  pendingAction: PendingAction | null;
  isStreaming: boolean;
  isLoadingHistory: boolean;
  canSend: boolean;
  isBusy: boolean;
  backdrop: DesktopBackdrop;
  shortcut: string;
  settingsOpen: boolean;
  stress: boolean;
  children?: React.ReactNode;
  onSend: (text: string) => void;
  onInterrupt: () => void;
  onApprove: () => void;
  onDeny: () => void;
  onChooseDisplay: (display: number) => void;
  onCancelDisplay: () => void;
  onClear: () => void;
  onOpenSettings: () => void;
  onCloseSettings: () => void;
};

const VISION_PATTERN = /screen|vision|screenshot|display|set[- ]of[- ]mark|\bsom\b/i;

function toolVerb(tool?: string, label?: string): string {
  // Prefer the tool name; labels are prose and can mention "folder" for a move.
  const name = tool ?? "";
  if (VISION_PATTERN.test(`${name} ${label ?? ""}`)) return "Reading the screen";
  if (/shell|powershell|command|terminal|cmd/i.test(name)) return "Running a command";
  if (/web|fetch|http|browse/i.test(name)) return "Fetching from the web";
  if (/search|glob|grep|find|list_dir/i.test(name)) return "Searching your files";
  if (/move|copy|rename|organize|create|delete|write/i.test(name)) {
    return "Tidying your files";
  }
  if (/approval|waiting|permission/i.test(name)) return "Waiting for you";
  return "Working";
}

/** Minimal three-dot indicator: no labels, no raw tool logs. */
function WorkingDots({ tone = "violet" }: { tone?: "violet" | "emerald" }) {
  const color = tone === "emerald" ? "bg-emerald-200/80" : "bg-violet-200/80";
  return (
    <span className="flex items-center gap-1" aria-hidden>
      {[0, 1, 2].map((index) => (
        <motion.span
          key={index}
          className={`size-[3.5px] rounded-full ${color}`}
          animate={{ opacity: [0.25, 1, 0.25], y: [0, -1.5, 0] }}
          transition={{
            duration: 1.1,
            repeat: Infinity,
            ease: "easeInOut",
            delay: index * 0.14,
          }}
        />
      ))}
    </span>
  );
}

export function ModernPalette({
  messages,
  identity,
  pendingAction,
  isStreaming,
  isLoadingHistory,
  canSend,
  isBusy,
  backdrop,
  shortcut,
  settingsOpen,
  stress,
  children,
  onSend,
  onInterrupt,
  onApprove,
  onDeny,
  onChooseDisplay,
  onCancelDisplay,
  onClear,
  onOpenSettings,
  onCloseSettings,
}: ModernPaletteProps) {
  const [value, setValue] = useState("");
  const [contentNode, setContentNode] = useState<HTMLDivElement | null>(null);
  const [stressText, setStressText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const expanded = settingsOpen || pendingAction !== null || messages.length > 0 || stress;

  const liveStatus = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role === "status" && message.status) return message.status;
      if (message.role === "assistant" && message.status) return message.status;
    }
    return null;
  }, [messages]);

  const analyzing =
    (isStreaming || stress) &&
    (stress || VISION_PATTERN.test(`${liveStatus?.tool ?? ""} ${liveStatus?.label ?? ""}`));

  const working = isStreaming || stress;

  const turn = useMemo(() => {
    let lastUser = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "user") {
        lastUser = index;
        break;
      }
    }
    const after = messages.slice(lastUser + 1);
    const answer = after
      .filter((message) => message.role === "assistant")
      .map((message) => message.content)
      .join("\n\n")
      .trim();
    const question = lastUser >= 0 ? messages[lastUser].content : "";
    return { question, answer };
  }, [messages]);

  const answer = stress ? stressText : turn.answer;

  // Synthetic streaming run for the QA rendering matrix. It exercises token
  // growth plus palette expansion without touching the backend.
  useEffect(() => {
    if (!stress) {
      setStressText("");
      return;
    }
    const words =
      "Compa is measuring layout stability while a long answer streams into the palette. ".repeat(
        14,
      );
    const chunks = words.split(" ");
    let index = 0;
    const timer = window.setInterval(() => {
      index += 3;
      setStressText(chunks.slice(0, index).join(" "));
      if (index >= chunks.length) {
        window.clearInterval(timer);
      }
    }, 24);
    return () => window.clearInterval(timer);
  }, [stress]);

  useEffect(() => {
    if (!expanded) {
      return;
    }
    const node = scrollRef.current;
    if (!node) {
      return;
    }
    const card = node.querySelector<HTMLElement>("[data-approval-card]");
    if (card) {
      card.scrollIntoView({ block: "start" });
      return;
    }
    node.scrollTop = node.scrollHeight;
  }, [contentNode, expanded, answer, isStreaming, pendingAction, settingsOpen]);

  useEffect(() => {
    const timer = window.setTimeout(() => inputRef.current?.focus(), 60);
    return () => window.clearTimeout(timer);
  }, []);

  // Summoning from the tray or the global shortcut should land in the input.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void onSummon(() => {
      inputRef.current?.focus();
    }).then((off) => {
      unlisten = off;
    });
    return () => unlisten?.();
  }, []);

  // The palette grows with its content instead of always opening a full sheet.
  // While streaming it only ever grows, at most once every 600 ms, so the
  // native window animation never fights token rendering. A callback ref is
  // used because AnimatePresence mounts the body a frame after `expanded`.
  const measuredTarget = useRef(PALETTE_COLLAPSED);
  useEffect(() => {
    if (settingsOpen) {
      void setPaletteHeight(PALETTE_SETTINGS);
      return;
    }
    if (!expanded) {
      measuredTarget.current = PALETTE_COLLAPSED;
      void setPaletteHeight(PALETTE_COLLAPSED);
      return;
    }
    const element = contentNode;
    if (!element) {
      return;
    }

    let last = 0;
    let timer = 0;
    let disposed = false;

    const measure = () => {
      if (disposed) {
        return;
      }
      const wait = Math.max(0, 600 - (performance.now() - last));
      if (wait > 0) {
        if (!timer) {
          timer = window.setTimeout(() => {
            timer = 0;
            measure();
          }, wait);
        }
        return;
      }
      last = performance.now();
      const natural = element.scrollHeight + PALETTE_CHROME;
      const target = Math.min(PALETTE_EXPANDED, Math.max(working ? 220 : 150, natural));
      if (Math.abs(target - measuredTarget.current) < 24) {
        return;
      }
      measuredTarget.current = target;
      void setPaletteHeight(target);
    };

    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => {
      disposed = true;
      observer.disconnect();
      window.clearTimeout(timer);
    };
  }, [contentNode, expanded, settingsOpen, working]);

  // One window resize per phase change; streaming only grows the palette.

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || !canSend) {
      return;
    }
    setValue("");
    onSend(text);
  }, [canSend, onSend, value]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      // Ctrl+, opens settings, matching Raycast and Windows conventions.
      if (event.ctrlKey && event.key === ",") {
        event.preventDefault();
        onOpenSettings();
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submit();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        if (value) {
          setValue("");
        } else if (settingsOpen) {
          onCloseSettings();
        } else {
          void hidePalette();
        }
      }
    },
    [onCloseSettings, onOpenSettings, settingsOpen, submit, value],
  );

  const verb = toolVerb(liveStatus?.tool, liveStatus?.label);

  return (
    <div
      className="palette-root relative flex h-dvh w-full flex-col"
      data-backdrop={backdrop}
      data-analyzing={analyzing ? "true" : "false"}
    >
      <motion.div
        className="palette-glow"
        aria-hidden
        animate={{ opacity: analyzing ? 1 : 0 }}
        transition={{ duration: 0.35, ease: "easeOut" }}
      />

      <motion.div
        className="palette-card relative flex min-h-0 flex-1 flex-col overflow-hidden"
        initial={{ opacity: 0, y: -8, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={SPRING}
      >
        <header
          data-tauri-drag-region="deep"
          className="flex h-[58px] shrink-0 items-center gap-2.5 px-4"
        >
          <Search className="size-[15px] shrink-0 text-white/54" strokeWidth={2.2} />
          <input
            ref={inputRef}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isLoadingHistory}
            spellCheck={false}
            placeholder={
              isLoadingHistory
                ? "Waking Compa…"
                : "Ask Compa, search your PC, or run a command"
            }
            aria-label="Ask Compa"
            className="min-w-0 flex-1 bg-transparent text-[14px] leading-5 text-white/90 caret-violet-300 outline-none placeholder:text-white/48"
          />

          <AnimatePresence mode="popLayout" initial={false}>
            {working ? (
              <motion.button
                key="stop"
                type="button"
                onClick={onInterrupt}
                aria-label="Stop"
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.92, transition: { duration: 0.12, ease: EASE_IN } }}
                className="flex size-7 items-center justify-center rounded-full border border-white/10 bg-white/[0.04] text-white/60 transition-colors hover:text-white/90"
              >
                <Square className="size-3" />
              </motion.button>
            ) : (
              <motion.button
                key="send"
                type="button"
                onClick={submit}
                disabled={!value.trim() || !canSend}
                aria-label="Send"
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.92, transition: { duration: 0.12, ease: EASE_IN } }}
                className={`flex size-7 items-center justify-center rounded-full border transition-colors ${
                  value.trim() && canSend
                    ? "border-violet-300/30 bg-violet-300/20 text-violet-50"
                    : "border-white/[0.06] bg-white/[0.02] text-white/30"
                }`}
              >
                <ArrowUp className="size-3.5" strokeWidth={2.4} />
              </motion.button>
            )}
          </AnimatePresence>

          <span className="mx-0.5 h-4 w-px bg-white/[0.07]" aria-hidden />

          {turn.question && !settingsOpen && (
            <button
              type="button"
              onClick={onClear}
              disabled={isBusy}
              aria-label="Clear this chat"
              title="Clear this chat"
              className="flex size-7 items-center justify-center rounded-lg text-white/48 transition-colors hover:bg-white/[0.06] hover:text-white/70 disabled:opacity-30"
            >
              <Trash2 className="size-3.5" />
            </button>
          )}

          <button
            type="button"
            onClick={settingsOpen ? onCloseSettings : onOpenSettings}
            aria-label="Settings"
            aria-pressed={settingsOpen}
            title="Settings"
            className={`flex size-7 items-center justify-center rounded-lg transition-colors ${
              settingsOpen
                ? "bg-white/[0.08] text-white/80"
                : "text-white/48 hover:bg-white/[0.06] hover:text-white/70"
            }`}
          >
            <Settings2 className="size-3.5" />
          </button>

          <button
            type="button"
            onClick={() => void hidePalette()}
            aria-label="Hide to tray"
            title="Hide to tray"
            className="flex size-7 items-center justify-center rounded-lg text-white/48 transition-colors hover:bg-white/[0.06] hover:text-white/70"
          >
            <X className="size-3.5" />
          </button>
        </header>

        <AnimatePresence initial={false} mode="wait">
          {expanded ? (
            <motion.div
              key="body"
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 2, transition: { duration: 0.13, ease: EASE_IN } }}
              transition={{ duration: 0.18, ease: EASE_OUT }}
              className="flex min-h-0 flex-1 flex-col border-t border-white/[0.055]"
            >
              {settingsOpen ? (
                <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
              ) : (
                <>
                  <div ref={scrollRef} className="palette-scroll min-h-0 flex-1 overflow-y-auto px-5 py-4">
                    <div ref={setContentNode}>
                    {pendingAction && pendingAction.kind === "display" ? (
                  <DisplayPickerCard
                    pending={pendingAction}
                    busy={isStreaming}
                    onChoose={onChooseDisplay}
                    onCancel={onCancelDisplay}
                  />
                ) : pendingAction ? (
                  <ApprovalCard
                    pending={pendingAction}
                    busy={isStreaming}
                    onApprove={onApprove}
                    onDeny={onDeny}
                  />
                ) : (
                  <div className="space-y-3">
                    {turn.question && (
                      <motion.p
                        initial={{ opacity: 0, y: -2 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: 0.16, ease: EASE_OUT }}
                        className="truncate text-[11.5px] text-white/54"
                      >
                        {turn.question}
                      </motion.p>
                    )}

                    {answer ? (
                      <div className="relative">
                        <MarkdownContent
                          content={answer}
                          className="palette-markdown text-[13px] leading-[1.62] text-white/85"
                        />
                        {working && <span className="palette-caret" aria-hidden />}
                      </div>
                    ) : working ? (
                      <div className="flex h-6 items-center" aria-label={verb}>
                        <WorkingDots />
                      </div>
                    ) : (
                      <p className="text-[12px] text-white/54">No reply yet.</p>
                    )}

                    {working && (
                      <div className="flex items-center gap-2 text-[10.5px] text-white/54">
                        {liveStatus?.state === "success" ? (
                          <span className="size-1.5 rounded-full bg-emerald-300/70" />
                        ) : (
                          <Loader2 className="size-2.5 animate-spin text-violet-200/70" />
                        )}
                        <span>{verb}</span>
                      </div>
                    )}
                  </div>
                )}
                    </div>
              </div>

                  <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-white/[0.05] px-4 py-1.5 text-[10px] text-white/48">
                    <span className="truncate">{working ? "working" : "ready"}</span>
                    <span className="shrink-0">Enter to send · Esc to hide</span>
                  </footer>
                </>
              )}
            </motion.div>
          ) : (
            <motion.div
              key="empty"
              variants={CONTENT_ENTRANCE}
              initial="hidden"
              animate="visible"
              exit="exit"
              className="flex min-h-0 flex-1 items-center px-4"
            >
              <div className="flex flex-wrap gap-1.5">
                {QUICK_PROMPTS.slice(0, 3).map((quick) => (
                  <button
                    key={quick.label}
                    type="button"
                    disabled={!canSend}
                    onClick={() => onSend(quick.prompt)}
                    className="rounded-full border border-white/[0.06] bg-white/[0.02] px-2.5 py-1 text-[10.5px] text-white/58 transition-colors duration-150 ease-out hover:border-white/10 hover:text-white/75 disabled:opacity-40"
                  >
                    {quick.label}
                  </button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex h-[26px] shrink-0 items-center justify-between gap-3 px-4 text-[10px] text-white/48">
          <span className="truncate">
            {identity ? shortModelName(identity.model) : "Local · offline"}
          </span>
          <span className="shrink-0 font-mono">
            {prettyShortcut(shortcut)} to summon
          </span>
        </div>
      </motion.div>
    </div>
  );
}
