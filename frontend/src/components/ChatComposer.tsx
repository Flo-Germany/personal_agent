import { memo, useEffect, useRef, useState } from "react";
import { Square } from "lucide-react";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent } from "react";

import { QUICK_PROMPTS } from "../lib/prompts";
import { ArrowUpIcon, MicIcon, PlusIcon } from "./icons";

type SpeechResultEventLike = {
  results: ArrayLike<ArrayLike<{ transcript: string }>>;
};

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechResultEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
};

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function getSpeechRecognitionCtor(): SpeechRecognitionCtor | null {
  const candidate = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return candidate.SpeechRecognition ?? candidate.webkitSpeechRecognition ?? null;
}

type ChatComposerProps = {
  onSend: (text: string) => void;
  onInterrupt: () => void;
  canSend: boolean;
  isStreaming: boolean;
  isLoadingHistory: boolean;
  pending: boolean;
};

export const ChatComposer = memo(function ChatComposer({
  onSend,
  onInterrupt,
  canSend,
  isStreaming,
  isLoadingHistory,
  pending,
}: ChatComposerProps) {
  const [value, setValue] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [speechSupported] = useState(
    () => getSpeechRecognitionCtor() !== null,
  );

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) {
      return;
    }
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 144)}px`;
  }, [value]);

  useEffect(() => {
    return () => {
      recognitionRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!menuOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      if (
        menuRef.current &&
        !menuRef.current.contains(event.target as Node)
      ) {
        setMenuOpen(false);
      }
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  const submit = () => {
    const text = value.trim();
    if (!text || !canSend) {
      return;
    }
    onSend(text);
    setValue("");
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submit();
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      submit();
    }
  };

  const usePrompt = (prompt: string) => {
    setValue(prompt);
    setMenuOpen(false);
    requestAnimationFrame(() => {
      const element = textareaRef.current;
      if (element) {
        element.focus();
        element.setSelectionRange(prompt.length, prompt.length);
      }
    });
  };

  const toggleMic = () => {
    if (isListening) {
      recognitionRef.current?.stop();
      setIsListening(false);
      return;
    }

    const Ctor = getSpeechRecognitionCtor();
    if (!Ctor) {
      return;
    }

    const recognition = new Ctor();
    recognition.lang = navigator.language || "en-US";
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript ?? "";
      if (!transcript) {
        return;
      }
      setValue((current) =>
        current ? `${current.trimEnd()} ${transcript}` : transcript,
      );
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    recognition.onend = () => setIsListening(false);
    recognition.onerror = () => setIsListening(false);
    recognitionRef.current = recognition;

    try {
      recognition.start();
      setIsListening(true);
    } catch {
      setIsListening(false);
    }
  };

  const placeholder = pending
    ? "Respond to the card above…"
    : isLoadingHistory
      ? "Loading conversation…"
      : isStreaming
        ? "Agent is working…"
        : "Ask anything…";

  return (
    <div className="shrink-0 px-4 pt-1 pb-3 sm:px-5">
      <form
        onSubmit={handleSubmit}
        aria-label="Message"
        className="mx-auto w-full max-w-3xl"
      >
        <div className="focus-shell flex items-end gap-1 rounded-[22px] border border-fg/[0.09] bg-raised p-1.5 shadow-[0_14px_44px_-16px_var(--compa-shadow-strong)]">
          <div className="relative" ref={menuRef}>
            <button
              type="button"
              onClick={() => setMenuOpen((open) => !open)}
              aria-label="Quick prompts"
              aria-expanded={menuOpen}
              title="Quick prompts"
              className={`flex size-8 items-center justify-center rounded-full transition-all duration-200 ease-out hover:bg-fg/[0.07] hover:text-fg/80 ${
                menuOpen ? "bg-fg/[0.07] text-fg/80" : "text-fg/68"
              }`}
            >
              <PlusIcon
                className={`size-4 transition-transform duration-200 ease-out ${
                  menuOpen ? "rotate-45" : ""
                }`}
              />
            </button>

            {menuOpen && (
              <div className="message-in absolute bottom-full left-0 z-20 mb-2 w-56 overflow-hidden rounded-xl border border-fg/[0.09] bg-raised p-1 shadow-[0_18px_50px_var(--compa-shadow-soft)]">
                <p className="px-2.5 py-1.5 text-[10px] tracking-wider text-fg/68 uppercase">
                  Quick prompts
                </p>
                {QUICK_PROMPTS.map((item) => (
                  <button
                    key={item.label}
                    type="button"
                    onClick={() => usePrompt(item.prompt)}
                    className="w-full rounded-lg px-2.5 py-1.5 text-left text-[12px] text-fg/74 transition-colors duration-200 ease-out hover:bg-fg/[0.06] hover:text-fg/90"
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={toggleMic}
            disabled={!speechSupported}
            aria-label={isListening ? "Stop dictation" : "Start dictation"}
            title={
              speechSupported
                ? isListening
                  ? "Stop dictation"
                  : "Start dictation"
                : "Voice input is not available in this environment"
            }
            className={`flex size-8 items-center justify-center rounded-full transition-all duration-200 ease-out disabled:cursor-not-allowed disabled:opacity-30 ${
              isListening
                ? "bg-accent/15 text-accent-ink"
                : "text-fg/68 hover:bg-fg/[0.07] hover:text-fg/80"
            }`}
          >
            <MicIcon className={`size-4 ${isListening ? "animate-pulse" : ""}`} />
          </button>

          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            maxLength={8000}
            placeholder={placeholder}
            aria-label="Message"
            autoFocus
            disabled={!canSend}
            className="max-h-36 min-h-8 min-w-0 flex-1 resize-none bg-transparent px-1 py-1.5 text-[13px] leading-5 text-fg/90 placeholder:text-fg/75 focus:outline-none disabled:cursor-not-allowed"
          />

          {isStreaming ? (
            <button
              type="button"
              onClick={onInterrupt}
              aria-label="Stop agent"
              title="Stop agent"
              className="flex size-9 shrink-0 items-center justify-center rounded-full bg-danger text-white transition-all duration-200 ease-out hover:brightness-110 active:scale-95"
            >
              <Square
                className="size-3.5"
                fill="currentColor"
                strokeWidth={0}
              />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!value.trim() || !canSend}
              aria-label="Send message"
              title="Send message"
              className="flex size-9 shrink-0 items-center justify-center rounded-full bg-accent text-white transition-all duration-200 ease-out hover:bg-accent-strong active:scale-95 disabled:cursor-not-allowed disabled:bg-fg/10 disabled:text-fg/68"
            >
              <ArrowUpIcon className="size-4" strokeWidth={2} />
            </button>
          )}
        </div>

        <p className="mt-2 text-center text-[10px] text-fg/68">
          Actions always require your approval · Local model
        </p>
      </form>
    </div>
  );
});
