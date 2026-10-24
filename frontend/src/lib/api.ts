import type {
  AgentEvent,
  HistoryMessage,
  Identity,
  PendingAction,
  StreamCallbacks,
} from "../types";

export const API_BASE_URL =
  import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

export const CONVERSATION_STORAGE_KEY = "personal-agent.conversation-id";

export const NETWORK_ERROR_MESSAGE =
  "I couldn't reach the local backend. Make sure FastAPI is running on 127.0.0.1:8000.";
export const STREAM_CLOSED_MESSAGE =
  "The agent connection closed before it finished. Please try again.";
export const UNEXPECTED_RESPONSE_MESSAGE =
  "The local backend returned an unexpected response.";

export function readStoredConversationId(): string | null {
  try {
    return window.localStorage.getItem(CONVERSATION_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeConversationId(conversationId: string | null) {
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
        event.tool,
        event.result,
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
export async function consumeEventStream(
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

export async function fetchIdentity(): Promise<Identity | null> {
  try {
    const response = await fetch(`${API_BASE_URL}/identity`);
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as Identity;
  } catch {
    return null;
  }
}

export async function fetchHistory(
  conversationId: string,
): Promise<HistoryMessage[]> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/chat/history/${conversationId}`,
    );
    if (!response.ok) {
      return [];
    }
    const data = (await response.json()) as {
      messages?: HistoryMessage[];
    };
    return data.messages ?? [];
  } catch {
    return [];
  }
}

export async function fetchPending(
  conversationId: string,
): Promise<PendingAction | null> {
  try {
    const response = await fetch(
      `${API_BASE_URL}/chat/pending/${conversationId}`,
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
): Promise<Response> {
  return fetch(`${API_BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
    }),
  });
}

export function postApproval(
  approvalId: string,
  approved: boolean,
): Promise<Response> {
  return fetch(`${API_BASE_URL}/chat/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      approval_id: approvalId,
      approved,
    }),
  });
}
