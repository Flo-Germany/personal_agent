import type {
  ActionItem,
  AgentEvent,
  DiagnosisReportData,
  HistoryMessage,
  Identity,
  PendingAction,
  SearchResultData,
  StatusPhase,
  StreamCallbacks,
} from "../types";

export const API_BASE_URL =
  import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

export const CONVERSATION_STORAGE_KEY = "personal-agent.conversation-id";

export const NETWORK_ERROR_MESSAGE =
  "I couldn't reach the local backend. It may still be starting - wait a moment and try again. If this keeps happening, check the launcher log in %LOCALAPPDATA%\\com.personalagent.desktop\\logs.";
export const STREAM_CLOSED_MESSAGE =
  "The agent connection closed before it finished. Please try again.";

export function readStoredConversationId(): string | null {
  try {
    return window.localStorage.getItem(CONVERSATION_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeConversationId(conversationId: string | null): void {
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

export function describeHttpError(
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

const STATUS_PHASES: readonly StatusPhase[] = [
  "thinking",
  "tool_started",
  "approval",
  "generating",
];

function isStatusPhase(value: unknown): value is StatusPhase {
  return (
    typeof value === "string" &&
    STATUS_PHASES.includes(value as StatusPhase)
  );
}

function isActionItem(value: unknown): value is ActionItem {
  return (
    isRecord(value) &&
    typeof value.tool === "string" &&
    typeof value.description === "string"
  );
}

function isDisplayOption(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.index === "number" &&
    typeof value.width === "number" &&
    typeof value.height === "number"
  );
}

function isAgentEvent(value: unknown): value is AgentEvent {
  if (!isRecord(value) || typeof value.type !== "string") {
    return false;
  }

  switch (value.type) {
    case "status":
      return (
        typeof value.message === "string" &&
        (value.phase === undefined || isStatusPhase(value.phase))
      );
    case "tool_result":
      return typeof value.tool === "string" && typeof value.ok === "boolean";
    case "assistant":
      return typeof value.content === "string";
    case "assistant_start":
      return true;
    case "assistant_delta":
      return typeof value.content === "string";
    case "assistant_thinking":
      return typeof value.content === "string";
    case "assistant_end":
      return value.content === undefined || typeof value.content === "string";
    case "assistant_reset":
      return true;
    case "metrics":
      return true;
    case "cancelled":
      return true;
    case "action_required":
      return (
        typeof value.approval_id === "string" &&
        Array.isArray(value.actions) &&
        value.actions.every(isActionItem)
      );
    case "display_required":
      return (
        typeof value.approval_id === "string" &&
        Array.isArray(value.displays) &&
        value.displays.every(isDisplayOption)
      );
    case "final":
      return (
        typeof value.reply === "string" &&
        (value.streamed === undefined || typeof value.streamed === "boolean")
      );
    case "error":
      return typeof value.message === "string";
    default:
      return false;
  }
}

function dispatchEvent(event: AgentEvent, callbacks: StreamCallbacks): boolean {
  switch (event.type) {
    case "status":
      callbacks.onStatus(event.message, event.phase, event.tool);
      return false;
    case "tool_result":
      callbacks.onToolResult(
        event.ok,
        event.summary ?? "Tool completed.",
        event.tool,
        event.result,
      );
      return false;
    case "assistant":
      callbacks.onAssistant(event.content);
      return false;
    case "assistant_start":
      callbacks.onAssistantStart();
      return false;
    case "assistant_delta":
      callbacks.onAssistantDelta(event.content);
      return false;
    case "assistant_thinking":
      callbacks.onThinking(event.content);
      return false;
    case "assistant_end":
      callbacks.onAssistantEnd(event.content);
      return false;
    case "assistant_reset":
      callbacks.onAssistantReset();
      return false;
    case "metrics":
      callbacks.onMetrics(event);
      return false;
    case "cancelled":
      callbacks.onCancelled();
      return true;
    case "action_required":
      callbacks.onActionRequired({
        approval_id: event.approval_id,
        conversation_id: event.conversation_id ?? "",
        actions: event.actions,
        message: event.message,
        request: event.request,
        plan: event.plan,
        expires_in_seconds: event.expires_in_seconds,
        auto_approve_remaining: event.auto_approve_remaining,
      });
      return true;
    case "display_required":
      callbacks.onActionRequired({
        kind: "display",
        approval_id: event.approval_id,
        conversation_id: event.conversation_id ?? "",
        actions: [],
        displays: event.displays,
        message: event.message,
        request: event.request,
        question: event.question,
        expires_in_seconds: event.expires_in_seconds,
      });
      return true;
    case "final":
      callbacks.onFinal(event.reply, event.conversation_id, event.streamed, {
        text: event.thinking,
        ms: event.thinking_ms,
        tokens: event.thinking_tokens,
      });
      return true;
    case "error":
      callbacks.onError(event.message);
      return true;
  }
}

/** Read an SSE response produced by POST and dispatch parsed events. */
export async function consumeEventStream(
  response: Response,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
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

    let parsed: unknown;
    try {
      parsed = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }

    if (!isAgentEvent(parsed)) {
      return;
    }

    if (dispatchEvent(parsed, callbacks)) {
      terminal = true;
    }
  };

  let pendingCR = false;
  try {
    while (true) {
      if (signal?.aborted) {
        throw new DOMException("The stream was aborted.", "AbortError");
      }

      // A terminal event means the turn is over even if the socket stays open.
      // Without this, Approve/Deny remain disabled forever.
      if (terminal) {
        try {
          await reader.cancel();
        } catch {
          // The body may already be closed.
        }
        break;
      }

      const chunk = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => {
          window.setTimeout(
            () => reject(new Error("The response stream stalled.")),
            60_000,
          );
        }),
      ]);
      const { value, done } = chunk;
      if (done) {
        break;
      }

      let text = decoder.decode(value, { stream: true });
      // Rejoin a CRLF that straddled two chunks before normalising.
      if (pendingCR) {
        text = "\r" + text;
        pendingCR = false;
      }
      if (text.endsWith("\r")) {
        text = text.slice(0, -1);
        pendingCR = true;
      }
      buffer += text.replace(/\r\n/g, "\n");

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        processBlock(block);
        boundary = buffer.indexOf("\n\n");
      }
    }

    if (!terminal) {
      buffer += (pendingCR ? "\r" : "") + decoder.decode();
      buffer = buffer.replace(/\r\n/g, "\n").trim();
      if (buffer) {
        processBlock(buffer);
      }
    }

    return terminal;
  } finally {
    reader.releaseLock();
  }
}

export async function fetchIdentity(
  signal?: AbortSignal,
): Promise<Identity | null> {
  try {
    const response = await fetch(`${API_BASE_URL}/identity`, { signal });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as Identity;
  } catch {
    return null;
  }
}

/**
 * Wait for the local backend to accept requests.
 *
 * The desktop app starts the backend as a hidden child process. The webview
 * may load a fraction of a second earlier, so the first identity request is
 * retried instead of rendering an empty conversation.
 */
export async function waitForBackend(
  signal?: AbortSignal,
  timeoutMs = 60_000,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    if (signal?.aborted) {
      return false;
    }

    try {
      const response = await fetch(`${API_BASE_URL}/health`, { signal });
      if (response.ok) {
        return true;
      }
    } catch {
      // The backend is still starting.
    }

    await new Promise((resolve) => setTimeout(resolve, 400));
  }

  return false;
}

export type HistorySnapshot = {
  messages: HistoryMessage[];
  diagnosis: DiagnosisReportData | null;
  searches: SearchResultData[];
};

export async function fetchHistory(
  conversationId: string,
  signal?: AbortSignal,
): Promise<HistorySnapshot> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/chat/history/${conversationId}`,
      { signal },
    );
    if (!response.ok) {
      return { messages: [], diagnosis: null, searches: [] };
    }
    const data = (await response.json()) as {
      messages?: HistoryMessage[];
      diagnosis?: DiagnosisReportData | null;
      searches?: SearchResultData[];
    };
    return {
      messages: data.messages ?? [],
      diagnosis: data.diagnosis ?? null,
      searches: Array.isArray(data.searches) ? data.searches : [],
    };
  } catch {
    return { messages: [], diagnosis: null, searches: [] };
  }
}

export async function fetchPending(
  conversationId: string,
  signal?: AbortSignal,
): Promise<PendingAction | null> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/chat/pending/${conversationId}`,
      { signal },
    );
    if (!response.ok) {
      return null;
    }
    const data = (await response.json()) as {
      pending?: PendingAction | null;
    };
    return data.pending ?? null;
  } catch {
    return null;
  }
}

export async function deleteConversation(
  conversationId: string,
): Promise<void> {
  try {
    await fetch(`${API_BASE_URL}/chat/history/${conversationId}`, {
      method: "DELETE",
    });
  } catch {
    // The local view is reset even if the backend is unreachable.
  }
}

export function postChatStream(
  message: string,
  conversationId: string | null,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${API_BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
    }),
    signal,
  });
}

export function postApproval(
  approvalId: string,
  approved: boolean,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${API_BASE_URL}/chat/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      approval_id: approvalId,
      approved,
    }),
    signal,
  });
}

export function postDisplayChoice(
  approvalId: string,
  display: number | null,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${API_BASE_URL}/chat/display`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      approval_id: approvalId,
      display,
    }),
    signal,
  });
}

export async function postInterrupt(
  conversationId: string,
  signal?: AbortSignal,
): Promise<void> {
  await fetch(`${API_BASE_URL}/chat/interrupt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId }),
    signal,
  });
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
