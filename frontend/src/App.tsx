import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const CONVERSATION_STORAGE_KEY = "personal-agent.conversation-id";

type ChatRole = "user" | "assistant" | "status";

type StatusKind =
  | "thinking"
  | "tool"
  | "result-ok"
  | "result-error"
  | "approval";

type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  statusKind?: StatusKind;
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

type ActionItem = {
  tool: string;
  description: string;
  arguments?: Record<string, unknown>;
};

type PendingAction = {
  approval_id: string;
  conversation_id: string;
  actions: ActionItem[];
  message?: string;
  expires_in_seconds?: number;
};

type AgentEvent = {
  type:
    | "status"
    | "tool_result"
    | "action_required"
    | "final"
    | "error"
    | "assistant";
  conversation_id?: string;
  phase?: string;
  message?: string;
  summary?: string;
  ok?: boolean;
  reply?: string;
  content?: string;
  approval_id?: string;
  actions?: ActionItem[];
  status?: number;
  expires_in_seconds?: number;
};

type StreamCallbacks = {
  onStatus: (message: string, phase?: string) => void;
  onToolResult: (ok: boolean, summary: string) => void;
  onAssistant: (content: string) => void;
  onActionRequired: (action: PendingAction) => void;
  onFinal: (reply: string, conversationId?: string) => void;
  onError: (message: string) => void;
};

function buildWelcomeMessage(name: string): ChatMessage {
  return {
    id: "welcome",
    role: "assistant",
    content: `Hi! I'm ${name}. I can work with your files and applications. I'll always ask for your approval before changing anything. How can I help?`,
  };
}

const NETWORK_ERROR_MESSAGE =
  "I couldn't reach the local backend. Make sure FastAPI is running on 127.0.0.1:8000.";
const STREAM_CLOSED_MESSAGE =
  "The agent connection closed before it finished. Please try again.";
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

function dispatchEvent(event: AgentEvent, callbacks: StreamCallbacks): boolean {
  switch (event.type) {
    case "status":
      if (event.message) {
        callbacks.onStatus(event.message, event.phase);
      }
      return false;
    case "tool_result":
      callbacks.onToolResult(
        Boolean(event.ok),
        event.summary ?? "Tool completed.",
      );
      return false;
    case "assistant":
      if (event.content) {
        callbacks.onAssistant(event.content);
      }
      return false;
    case "action_required":
      if (!event.approval_id || !Array.isArray(event.actions)) {
        callbacks.onError(UNEXPECTED_RESPONSE_MESSAGE);
        return true;
      }
      callbacks.onActionRequired({
        approval_id: event.approval_id,
        conversation_id: event.conversation_id ?? "",
        actions: event.actions,
        message: event.message,
        expires_in_seconds: event.expires_in_seconds,
      });
      return true;
    case "final":
      if (typeof event.reply !== "string") {
        callbacks.onError(UNEXPECTED_RESPONSE_MESSAGE);
      } else {
        callbacks.onFinal(event.reply, event.conversation_id);
      }
      return true;
    case "error":
      callbacks.onError(event.message ?? "The agent failed.");
      return true;
    default:
      return false;
  }
}

/** Read an SSE response produced by POST and dispatch parsed events. */
async function consumeEventStream(
  response: Response,
  callbacks: StreamCallbacks,
): Promise<boolean> {
  if (!response.body) {
    throw new Error("The response has no body.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;

  const processBlock = (block: string) => {
    const dataLines = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim());

    if (dataLines.length === 0) {
      return;
    }

    let event: AgentEvent;
    try {
      event = JSON.parse(dataLines.join("\n")) as AgentEvent;
    } catch {
      return;
    }

    if (dispatchEvent(event, callbacks)) {
      terminal = true;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      processBlock(block);
      boundary = buffer.indexOf("\n\n");
    }
  }

  buffer += decoder.decode();
  buffer = buffer.replace(/\r\n/g, "\n").trim();
  if (buffer) {
    processBlock(buffer);
  }

  return terminal;
}

const STATUS_STYLES: Record<StatusKind, string> = {
  thinking: "border-white/10 bg-white/5 text-slate-400",
  tool: "border-sky-400/20 bg-sky-400/5 text-sky-300",
  "result-ok": "border-emerald-400/20 bg-emerald-400/5 text-emerald-300",
  "result-error": "border-rose-400/20 bg-rose-400/5 text-rose-300",
  approval: "border-amber-400/20 bg-amber-400/5 text-amber-300",
};

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    buildWelcomeMessage("your local agent"),
  ]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(() =>
    readStoredConversationId(),
  );
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isStreaming, pendingAction]);

  const rememberConversation = (id?: string) => {
    if (!id) {
      return;
    }
    setConversationId(id);
    storeConversationId(id);
  };

  const addMessage = (role: ChatRole, content: string, statusKind?: StatusKind) => {
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role, content, statusKind },
    ]);
  };

  const addAssistant = (content: string) => addMessage("assistant", content);
  const addStatus = (content: string, statusKind: StatusKind) =>
    addMessage("status", content, statusKind);

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
      let restoredPending: PendingAction | null = null;

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

        try {
          const response = await fetch(
            `${API_BASE_URL}/chat/pending/${storedId}`,
          );
          if (response.ok) {
            const data = (await response.json()) as {
              pending?: PendingAction | null;
            };
            restoredPending = data.pending ?? null;
          }
        } catch {
          // No pending check available; the send path still works.
        }
      }

      if (cancelled) {
        return;
      }

      if (restoredPending) {
        setPendingAction(restoredPending);
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

  const streamCallbacks = (): StreamCallbacks => ({
    onStatus: (message, phase) => {
      if (phase === "tool_started") {
        addStatus(message, "tool");
      } else if (phase === "approval") {
        addStatus(message, "approval");
      } else {
        addStatus(message, "thinking");
      }
    },
    onToolResult: (ok, summary) => {
      addStatus(summary, ok ? "result-ok" : "result-error");
    },
    onAssistant: (content) => {
      addAssistant(content);
    },
    onActionRequired: (action) => {
      setPendingAction(action);
      rememberConversation(action.conversation_id);
      addStatus("Waiting for your approval.", "approval");
    },
    onFinal: (reply, id) => {
      addAssistant(reply);
      rememberConversation(id);
      setPendingAction(null);
    },
    onError: (message) => {
      addAssistant(message);
      setPendingAction(null);
    },
  });

  const handleStreamResponse = async (response: Response): Promise<void> => {
    if (!response.ok) {
      let body: unknown = null;

      try {
        body = await response.json();
      } catch {
        // Non-JSON error body; fall back to the status in the message.
      }

      addAssistant(describeHttpError(response.status, response.statusText, body));
      return;
    }

    const terminal = await consumeEventStream(response, streamCallbacks());
    if (!terminal) {
      addAssistant(STREAM_CLOSED_MESSAGE);
    }
  };

  const sendMessage = async (text: string) => {
    if (!text || isStreaming || isLoadingHistory || pendingAction) {
      return;
    }

    addMessage("user", text);
    setInput("");
    setIsStreaming(true);

    try {
      let response: Response;

      try {
        response = await fetch(`${API_BASE_URL}/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            conversation_id: conversationId,
          }),
        });
      } catch {
        addAssistant(NETWORK_ERROR_MESSAGE);
        return;
      }

      await handleStreamResponse(response);
    } catch {
      addAssistant(NETWORK_ERROR_MESSAGE);
    } finally {
      setIsStreaming(false);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const text = input.trim();
    if (!text || isStreaming || isLoadingHistory || pendingAction) {
      return;
    }

    void sendMessage(text);
  };

  const handleApproval = async (approved: boolean) => {
    const action = pendingAction;
    if (!action || isStreaming) {
      return;
    }

    setPendingAction(null);
    setIsStreaming(true);

    try {
      let response: Response;

      try {
        response = await fetch(`${API_BASE_URL}/chat/approve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            approval_id: action.approval_id,
            approved,
          }),
        });
      } catch {
        addAssistant(NETWORK_ERROR_MESSAGE);
        return;
      }

      await handleStreamResponse(response);
    } catch {
      addAssistant(NETWORK_ERROR_MESSAGE);
    } finally {
      setIsStreaming(false);
    }
  };

  const handleClearConversation = async () => {
    if (isStreaming || isLoadingHistory) {
      return;
    }

    const previousId = conversationId;

    setMessages([buildWelcomeMessage(identity?.name ?? "your local agent")]);
    setConversationId(null);
    setPendingAction(null);
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
  const initials = identity ? identity.name.slice(0, 2).toUpperCase() : "PA";
  const isBusy = isStreaming || isLoadingHistory;
  const inputDisabled = isBusy || pendingAction !== null;

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
              ? `${identity.model} · Local · HITL`
              : "Local · Private · Tray enabled"}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void handleClearConversation()}
          disabled={isBusy || !hasUserMessages}
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
        {messages.map((message) => {
          if (message.role === "status") {
            const kind = message.statusKind ?? "thinking";
            return (
              <div key={message.id} className="flex justify-center">
                <div
                  className={`flex max-w-[90%] items-center gap-2 rounded-full border px-3 py-1 text-[11px] leading-5 ${STATUS_STYLES[kind]}`}
                >
                  <span
                    className={`size-1.5 shrink-0 rounded-full bg-current ${
                      kind === "thinking" ? "animate-pulse" : ""
                    }`}
                  />
                  <span className="break-words">{message.content}</span>
                </div>
              </div>
            );
          }

          return (
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
          );
        })}

        {pendingAction && (
          <div className="rounded-2xl border border-amber-400/30 bg-amber-400/5 p-4">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-amber-300">
              <span className="flex size-5 items-center justify-center rounded-full border border-amber-400/40 text-[11px]">
                !
              </span>
              Action Required
            </div>
            <p className="mt-2 text-xs leading-5 text-slate-400">
              The agent wants to modify your system. Review the exact actions
              before approving.
            </p>
            <ul className="mt-3 space-y-2">
              {pendingAction.actions.map((action) => (
                <li
                  key={`${action.tool}-${action.description}`}
                  className="rounded-xl border border-white/10 bg-black/20 px-3 py-2"
                >
                  <p className="break-all text-xs font-medium text-slate-200">
                    {action.description}
                  </p>
                  <p className="mt-0.5 font-mono text-[10px] text-slate-500">
                    {action.tool}
                  </p>
                </li>
              ))}
            </ul>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => void handleApproval(true)}
                disabled={isStreaming}
                className="h-9 flex-1 rounded-xl bg-emerald-400 text-xs font-semibold text-slate-950 transition hover:bg-emerald-300 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Approve
              </button>
              <button
                type="button"
                onClick={() => void handleApproval(false)}
                disabled={isStreaming}
                className="h-9 flex-1 rounded-xl border border-rose-400/30 bg-rose-400/10 text-xs font-semibold text-rose-300 transition hover:bg-rose-400/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Deny
              </button>
            </div>
          </div>
        )}

        {isStreaming && (
          <div className="flex justify-start">
            <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-md border border-white/10 bg-white/5 px-3.5 py-3">
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400" />
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400 [animation-delay:150ms]" />
              <span className="size-1.5 animate-pulse rounded-full bg-slate-400 [animation-delay:300ms]" />
              <span className="sr-only">Agent is working</span>
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
              pendingAction
                ? "Approve or deny the pending action..."
                : isLoadingHistory
                  ? "Loading conversation..."
                  : isStreaming
                    ? "Agent is working..."
                    : "Send a message..."
            }
            aria-label="Message"
            maxLength={8000}
            autoFocus
            disabled={inputDisabled}
            className="h-11 min-w-0 flex-1 rounded-xl border border-white/10 bg-white/5 px-3 text-sm text-slate-200 placeholder:text-slate-500 focus:border-emerald-400/40 focus:outline-none focus:ring-2 focus:ring-emerald-400/10 disabled:cursor-not-allowed disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!input.trim() || inputDisabled}
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
          Ollama · {identity?.model ?? "hermes3"} · Streaming + HITL ·{" "}
          {API_BASE_URL.replace(/^https?:\/\//, "")}
        </p>
      </footer>
    </div>
  );
}

export default App;
