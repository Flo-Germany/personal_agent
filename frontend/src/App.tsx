import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const CONVERSATION_STORAGE_KEY = "personal-agent.conversation-id";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

type Identity = {
  name: string;
  personality: string;
  model: string;
};

type HistoryMessage = {
  role: "user" | "assistant";
  content: string;
};

type ChatResponse = {
  reply: string;
  conversation_id: string;
};

function buildWelcomeMessage(name: string): ChatMessage {
  return {
    id: "welcome",
    role: "assistant",
    content: `Hi! I'm ${name}. I run locally through Ollama. I'll remember this chat, and I keep a knowledge base about you across chats. How can I help?`,
  };
}

const NETWORK_ERROR_MESSAGE =
  "I couldn't reach the local backend. Make sure FastAPI is running on 127.0.0.1:8000.";
const UNEXPECTED_RESPONSE_MESSAGE =
  "The local backend returned an unexpected response.";

function readStoredConversationId(): string | null {
  try {
    return window.localStorage.getItem(CONVERSATION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeConversationId(conversationId: string | null) {
  try {
    if (conversationId) {
      window.localStorage.setItem(CONVERSATION_STORAGE_KEY, conversationId);
    } else {
      window.localStorage.removeItem(CONVERSATION_STORAGE_KEY);
    }
  } catch {
    // Storage can be unavailable in restricted webviews; chat still works.
  }
}

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

  if ((status === 502 || status === 503 || status === 504) && detail) {
    return detail;
  }

  const label = `HTTP ${status}${statusText ? ` ${statusText}` : ""}`;
  return detail
    ? `The backend returned an error (${label}): ${detail}`
    : `The backend returned an error (${label}).`;
}

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    buildWelcomeMessage("your local agent"),
  ]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(() =>
    readStoredConversationId(),
  );
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isSending]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      let loadedIdentity: Identity | null = null;

      try {
        const response = await fetch(`${API_BASE_URL}/identity`);
        if (response.ok) {
          loadedIdentity = (await response.json()) as Identity;
          if (!cancelled) {
            setIdentity(loadedIdentity);
          }
        }
      } catch {
        // The send path surfaces backend connectivity problems.
      }

      let history: ChatMessage[] = [];
      const storedId = readStoredConversationId();

      if (storedId) {
        try {
          const response = await fetch(
            `${API_BASE_URL}/chat/history/${storedId}`,
          );
          if (response.ok) {
            const data = (await response.json()) as {
              messages?: HistoryMessage[];
            };
            history = (data.messages ?? []).map((message) => ({
              id: crypto.randomUUID(),
              role: message.role,
              content: message.content,
            }));
          }
        } catch {
          // Keep the stored id; the history will load on the next start.
        }
      }

      if (cancelled) {
        return;
      }

      setMessages(
        history.length > 0
          ? history
          : [buildWelcomeMessage(loadedIdentity?.name ?? "your local agent")],
      );
      setIsLoadingHistory(false);
    };

    void load();

    return () => {
      cancelled = true;
    };
  }, []);

  const addAssistantMessage = (content: string) => {
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "assistant", content },
    ]);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const text = input.trim();
    if (!text || isSending || isLoadingHistory) {
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
          body: JSON.stringify({
            message: text,
            conversation_id: conversationId,
          }),
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

      let data: ChatResponse;

      try {
        data = (await response.json()) as ChatResponse;
      } catch {
        addAssistantMessage(UNEXPECTED_RESPONSE_MESSAGE);
        return;
      }

      if (typeof data.reply !== "string") {
        addAssistantMessage(UNEXPECTED_RESPONSE_MESSAGE);
        return;
      }

      if (typeof data.conversation_id === "string" && data.conversation_id) {
        setConversationId(data.conversation_id);
        storeConversationId(data.conversation_id);
      }

      addAssistantMessage(data.reply);
    } finally {
      setIsSending(false);
    }
  };

  const handleClearConversation = async () => {
    if (isSending || isLoadingHistory) {
      return;
    }

    const previousId = conversationId;

    setMessages([buildWelcomeMessage(identity?.name ?? "your local agent")]);
    setConversationId(null);
    storeConversationId(null);

    if (!previousId) {
      return;
    }

    try {
      await fetch(`${API_BASE_URL}/chat/history/${previousId}`, {
        method: "DELETE",
      });
    } catch {
      // The local view is reset even if the backend is unreachable.
    }
  };

  const hasUserMessages = messages.some((message) => message.role === "user");
  const displayName = identity?.name ?? "Personal Agent";
  const initials = identity
    ? identity.name.slice(0, 2).toUpperCase()
    : "PA";

  return (
    <div className="flex h-screen flex-col bg-[#0b0f14] text-slate-100">
      <header className="flex items-center gap-3 border-b border-white/10 px-4 py-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-emerald-400/15 text-sm font-semibold tracking-tight text-emerald-300 ring-1 ring-emerald-400/30">
          {initials}
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold tracking-tight">
            {displayName}
          </h1>
          <p className="truncate text-[11px] text-slate-400">
            {identity
              ? `${identity.model} · Local · Private`
              : "Local · Private · Tray enabled"}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void handleClearConversation()}
          disabled={isSending || isLoadingHistory || !hasUserMessages}
          aria-label="Clear this chat (knowledge base is kept)"
          title="Clear this chat (knowledge base is kept)"
          className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-white/20 hover:text-slate-200 disabled:cursor-not-allowed disabled:opacity-30"
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            className="size-4"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"
            />
          </svg>
        </button>
        <span className="flex shrink-0 items-center gap-1.5 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2 py-1 text-[10px] font-medium text-emerald-300">
          <span className="size-1.5 rounded-full bg-emerald-400" />
          Local LLM
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
              className={`max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2.5 text-sm leading-6 ${
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
              <span className="sr-only">Waiting for the local model</span>
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
            placeholder={
              isLoadingHistory ? "Loading conversation..." : "Send a message..."
            }
            aria-label="Message"
            maxLength={8000}
            autoFocus
            className="h-11 min-w-0 flex-1 rounded-xl border border-white/10 bg-white/5 px-3 text-sm text-slate-200 placeholder:text-slate-500 focus:border-emerald-400/40 focus:outline-none focus:ring-2 focus:ring-emerald-400/10"
          />
          <button
            type="submit"
            disabled={!input.trim() || isSending || isLoadingHistory}
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
          Ollama · {identity?.model ?? "hermes3"} ·{" "}
          {API_BASE_URL.replace(/^https?:\/\//, "")}
        </p>
      </footer>
    </div>
  );
}

export default App;
