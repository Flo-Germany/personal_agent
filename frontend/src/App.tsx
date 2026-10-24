import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

const WELCOME_MESSAGE: ChatMessage = {
  id: "welcome",
  role: "assistant",
  content:
    "Hi! I'm your local agent. The backend is currently running in mock mode.",
};

const NETWORK_ERROR_MESSAGE =
  "I couldn't reach the local backend. Make sure FastAPI is running on 127.0.0.1:8000.";
const UNEXPECTED_RESPONSE_MESSAGE =
  "The local backend returned an unexpected response.";

/** Pull a human-readable message out of a FastAPI error body. */
function extractErrorDetail(body: unknown): string | null {
  if (body === null || typeof body !== "object" || !("detail" in body)) {
    return null;
  }

  const detail = (body as { detail: unknown }).detail;

  if (typeof detail === "string") {
    return detail;
  }

  if (!Array.isArray(detail)) {
    return null;
  }

  const messages = detail
    .map((item) => {
      if (item === null || typeof item !== "object" || !("msg" in item)) {
        return null;
      }

      const msg = (item as { msg: unknown }).msg;
      return typeof msg === "string" ? msg : null;
    })
    .filter((msg): msg is string => msg !== null);

  return messages.length > 0 ? messages.join(" ") : null;
}

function describeHttpError(
  status: number,
  statusText: string,
  body: unknown,
): string {
  const detail = extractErrorDetail(body);

  if (status === 422) {
    return detail
      ? `The backend rejected the message: ${detail}`
      : "The backend rejected the message (422 Unprocessable Entity).";
  }

  const label = `HTTP ${status}${statusText ? ` ${statusText}` : ""}`;
  return detail
    ? `The backend returned an error (${label}): ${detail}`
    : `The backend returned an error (${label}).`;
}

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME_MESSAGE]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isSending]);

  const addAssistantMessage = (content: string) => {
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "assistant", content },
    ]);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const text = input.trim();
    if (!text || isSending) {
      return;
    }

    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", content: text },
    ]);
    setInput("");
    setIsSending(true);

    try {
      let response: Response;

      try {
        response = await fetch(`${API_BASE_URL}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
        });
      } catch {
        // fetch rejects on connection failures, DNS errors, and CORS blocks.
        addAssistantMessage(NETWORK_ERROR_MESSAGE);
        return;
      }

      if (!response.ok) {
        let body: unknown = null;

        try {
          body = await response.json();
        } catch {
          // Non-JSON error body; fall back to the status in the message.
        }

        addAssistantMessage(
          describeHttpError(response.status, response.statusText, body),
        );
        return;
      }

      let data: { reply?: unknown };

      try {
        data = (await response.json()) as { reply?: unknown };
      } catch {
        addAssistantMessage(UNEXPECTED_RESPONSE_MESSAGE);
        return;
      }

      if (typeof data.reply !== "string") {
        addAssistantMessage(UNEXPECTED_RESPONSE_MESSAGE);
        return;
      }

      addAssistantMessage(data.reply);
    } finally {
      setIsSending(false);
    }
  };

  return (
    <div className="flex h-screen flex-col bg-[#0b0f14] text-slate-100">
      <header className="flex items-center gap-3 border-b border-white/10 px-4 py-3">
        <div className="flex size-9 items-center justify-center rounded-xl bg-emerald-400/15 text-sm font-semibold tracking-tight text-emerald-300 ring-1 ring-emerald-400/30">
          PA
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="text-sm font-semibold tracking-tight">Personal Agent</h1>
          <p className="text-[11px] text-slate-400">
            Local · Private · Tray enabled
          </p>
        </div>
        <span className="flex items-center gap-1.5 rounded-full border border-amber-400/20 bg-amber-400/10 px-2 py-1 text-[10px] font-medium text-amber-300">
          <span className="size-1.5 rounded-full bg-amber-400" />
          Mock API
        </span>
      </header>

      <main
        aria-live="polite"
        className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-5"
      >
        {messages.map((message) => (
          <div
            key={message.id}
            className={`flex ${
              message.role === "user" ? "justify-end" : "justify-start"
            }`}
          >
            <div
              className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm leading-6 ${
                message.role === "user"
                  ? "rounded-br-md bg-emerald-400 text-slate-950"
                  : "rounded-bl-md border border-white/10 bg-white/5 text-slate-200"
              }`}
            >
              {message.content}
            </div>
          </div>
        ))}

        {isSending && (
          <div className="flex justify-start">
            <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-md border border-white/10 bg-white/5 px-3.5 py-3">
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400" />
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400 [animation-delay:150ms]" />
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400 [animation-delay:300ms]" />
              <span className="sr-only">Waiting for backend</span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </main>

      <footer className="border-t border-white/10 p-4">
        <form onSubmit={handleSubmit} className="flex items-center gap-2">
          <input
            type="text"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Send a message..."
            aria-label="Message"
            maxLength={8000}
            autoFocus
            className="h-11 min-w-0 flex-1 rounded-xl border border-white/10 bg-white/5 px-3 text-sm text-slate-200 placeholder:text-slate-500 focus:border-emerald-400/40 focus:outline-none focus:ring-2 focus:ring-emerald-400/10"
          />
          <button
            type="submit"
            disabled={!input.trim() || isSending}
            aria-label="Send message"
            className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-emerald-400 text-slate-950 transition hover:bg-emerald-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="size-5"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="m5 12 14-7-4 14-3-6-7-1Z"
              />
            </svg>
          </button>
        </form>
        <p className="mt-2 text-center text-[10px] text-slate-600">
          FastAPI mock · {API_BASE_URL.replace(/^https?:\/\//, "")}
        </p>
      </footer>
    </div>
  );
}

export default App;
