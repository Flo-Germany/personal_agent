import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  NETWORK_ERROR_MESSAGE,
  STREAM_CLOSED_MESSAGE,
  consumeEventStream,
  deleteConversation,
  describeHttpError,
  fetchHistory,
  fetchIdentity,
  fetchPending,
  isAbortError,
  postApproval,
  postChatStream,
  readStoredConversationId,
  storeConversationId,
} from "../lib/api";
import {
  extractToolFiles,
  replyMentionsPath,
  stripExternalImages,
} from "../lib/localFiles";
import type {
  ChatMessage,
  ChatRole,
  Identity,
  PendingAction,
  StreamCallbacks,
  ToolResult,
  ToolStatusData,
} from "../types";

type Snapshot = {
  identity: Identity | null;
  messages: ChatMessage[];
  pending: PendingAction | null;
};

async function loadSnapshot(
  storedId: string | null,
  signal?: AbortSignal,
): Promise<Snapshot> {
  const [identity, history, pending] = await Promise.all([
    fetchIdentity(signal),
    storedId ? fetchHistory(storedId, signal) : Promise.resolve([]),
    storedId ? fetchPending(storedId, signal) : Promise.resolve(null),
  ]);

  return {
    identity,
    messages: history.map((message) => ({
      id: crypto.randomUUID(),
      role: message.role,
      content: message.content,
    })),
    pending,
  };
}

function toolFailureDetails(result?: ToolResult): {
  detail?: string;
  exitCode?: number;
} {
  if (!result) {
    return {};
  }

  const exitCode =
    typeof result.exit_code === "number" ? result.exit_code : undefined;
  const parts: string[] = [];

  if (exitCode !== undefined) {
    parts.push(`Exit code: ${exitCode}`);
  }

  for (const key of ["error", "stderr", "hint", "stdout"] as const) {
    const value = result[key];
    if (typeof value === "string" && value.trim()) {
      parts.push(value.trim());
    }
  }

  if (parts.length === 0) {
    return { exitCode };
  }

  const detail = parts.join("\n");
  return {
    exitCode,
    detail: detail.length > 4000 ? `${detail.slice(0, 4000)}\n…` : detail,
  };
}

export function useAgentChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(() =>
    readStoredConversationId(),
  );
  const [isStreaming, setIsStreaming] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);
  const pendingFilesRef = useRef<Set<string>>(new Set());
  const usedSemanticSearchRef = useRef(false);
  const activeStreamRef = useRef<AbortController | null>(null);
  const loadControllerRef = useRef<AbortController | null>(null);
  const streamingRef = useRef(false);
  const mountedRef = useRef(true);
  const activeStatusIdRef = useRef<string | null>(null);
  const lastStatusStateRef = useRef<ToolStatusData["state"] | null>(null);

  const addMessage = useCallback(
    (role: ChatRole, content: string, files?: string[]) => {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role,
          content,
          files,
        },
      ]);
    },
    [],
  );

  const addAssistant = useCallback(
    (content: string) => addMessage("assistant", content),
    [addMessage],
  );

  const updateStatus = useCallback((status: ToolStatusData) => {
    if (!activeStatusIdRef.current) {
      activeStatusIdRef.current = crypto.randomUUID();
    }
    const statusId = activeStatusIdRef.current;
    lastStatusStateRef.current = status.state;

    if (!mountedRef.current) {
      return;
    }

    setMessages((current) => {
      const exists = current.some((message) => message.id === statusId);
      if (!exists) {
        return [
          ...current,
          {
            id: statusId,
            role: "status",
            content: "",
            status,
          },
        ];
      }

      return current.map((message) =>
        message.id === statusId ? { ...message, status } : message,
      );
    });
  }, []);

  const beginStatus = useCallback(() => {
    activeStatusIdRef.current = null;
    updateStatus({ state: "thinking", label: "Thinking..." });
  }, [updateStatus]);

  const addAttachments = useCallback(
    (files: string[]) => {
      const unique = [...new Set(files)];
      if (unique.length > 0) {
        addMessage("files", "", unique);
      }
    },
    [addMessage],
  );

  const rememberConversation = useCallback((id?: string) => {
    if (!id) {
      return;
    }
    setConversationId(id);
    storeConversationId(id);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    loadControllerRef.current = controller;

    const load = async () => {
      const snapshot = await loadSnapshot(
        readStoredConversationId(),
        controller.signal,
      );
      if (!mountedRef.current || controller.signal.aborted) {
        return;
      }

      setIdentity(snapshot.identity);
      setMessages(snapshot.messages);
      setPendingAction(snapshot.pending);
      setIsLoadingHistory(false);
    };

    void load();

    return () => {
      mountedRef.current = false;
      controller.abort();
      activeStreamRef.current?.abort();
      if (loadControllerRef.current === controller) {
        loadControllerRef.current = null;
      }
    };
  }, []);

  const handleStreamResponse = useCallback(
    async (response: Response, signal: AbortSignal): Promise<void> => {
      if (!response.ok) {
        let body: unknown = null;

        try {
          body = await response.json();
        } catch {
          // Non-JSON error body; fall back to the status in the message.
        }

        if (mountedRef.current) {
          updateStatus({
            state: "error",
            label: "Agent error",
            detail: describeHttpError(
              response.status,
              response.statusText,
              body,
            ),
          });
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
        }
        return;
      }

      const callbacks: StreamCallbacks = {
        onStatus: (message, phase) => {
          if (!mountedRef.current) {
            return;
          }
          if (phase === "tool_started") {
            updateStatus({ state: "executing", label: message });
          } else if (phase === "approval") {
            if (message.toLowerCase().includes("cancel")) {
              updateStatus({ state: "cancelled", label: "Action cancelled" });
            } else {
              updateStatus({ state: "executing", label: message });
            }
          } else {
            const previous = lastStatusStateRef.current;
            if (
              previous === "success" ||
              previous === "error" ||
              previous === "waiting" ||
              previous === "cancelled"
            ) {
              // Keep the meaningful result visible while the model reads it.
              return;
            }
            updateStatus({ state: "thinking", label: message });
          }
        },
        onToolResult: (ok, summary, tool, result) => {
          if (!mountedRef.current) {
            return;
          }
          const files = extractToolFiles(tool, result);
          files.forEach((file) => pendingFilesRef.current.add(file));

          if (tool === "semantic_image_search") {
            usedSemanticSearchRef.current = true;
          }

          if (ok) {
            updateStatus({
              state: "success",
              label: summary,
              tool,
            });
          } else if (result?.cancelled) {
            updateStatus({
              state: "cancelled",
              label: "Action cancelled",
              tool,
              detail: summary,
            });
          } else {
            const { detail, exitCode } = toolFailureDetails(result);
            const label =
              tool === "run_powershell_action"
                ? "Action failed"
                : tool === "run_powershell_query"
                  ? "Query failed"
                  : "Tool failed";
            updateStatus({
              state: "error",
              label,
              tool,
              detail: detail ?? summary,
              exitCode,
            });
          }
        },
        onAssistant: (content) => {
          if (mountedRef.current) {
            addAssistant(content);
          }
        },
        onActionRequired: (action) => {
          if (!mountedRef.current) {
            return;
          }
          setPendingAction(action);
          rememberConversation(action.conversation_id);
          updateStatus({
            state: "waiting",
            label: "Waiting for approval",
            tool: action.actions[0]?.tool,
            detail: action.actions.map((item) => item.description).join("\n"),
          });
        },
        onFinal: (reply, id) => {
          if (!mountedRef.current) {
            return;
          }
          let text = reply;

          if (usedSemanticSearchRef.current) {
            text = stripExternalImages(text);
          }

          const missingFiles = [...pendingFilesRef.current].filter(
            (path) => !replyMentionsPath(text, path),
          );

          if (text.trim()) {
            addAssistant(text);
          }

          if (missingFiles.length > 0) {
            addAttachments(missingFiles);
          }

          const statusId = activeStatusIdRef.current;
          const statusState = lastStatusStateRef.current;
          if (statusState === "thinking" && statusId) {
            // A simple text answer does not need a permanent "Completed" row.
            setMessages((current) =>
              current.filter((message) => message.id !== statusId),
            );
          } else if (statusState === "executing" && statusId) {
            updateStatus({ state: "success", label: "Completed" });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          rememberConversation(id);
          setPendingAction(null);
        },
        onError: (message) => {
          if (!mountedRef.current) {
            return;
          }
          updateStatus({
            state: "error",
            label: "Agent error",
            detail: message,
          });
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          addAssistant(message);
          setPendingAction(null);
        },
      };

      const terminal = await consumeEventStream(response, callbacks, signal);
      if (!terminal && mountedRef.current) {
        updateStatus({
          state: "error",
          label: "Connection closed",
          detail: STREAM_CLOSED_MESSAGE,
        });
        activeStatusIdRef.current = null;
        lastStatusStateRef.current = null;
        addAssistant(STREAM_CLOSED_MESSAGE);
      }
    },
    [addAssistant, addAttachments, rememberConversation, updateStatus],
  );

  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (
        !trimmed ||
        streamingRef.current ||
        isLoadingHistory ||
        pendingAction !== null
      ) {
        return;
      }

      addMessage("user", trimmed);
      beginStatus();
      pendingFilesRef.current.clear();
      usedSemanticSearchRef.current = false;
      streamingRef.current = true;
      setIsStreaming(true);

      const controller = new AbortController();
      activeStreamRef.current = controller;

      try {
        const response = await postChatStream(
          trimmed,
          conversationId,
          controller.signal,
        );
        await handleStreamResponse(response, controller.signal);
      } catch (error) {
        if (!isAbortError(error) && mountedRef.current) {
          updateStatus({
            state: "error",
            label: "Connection failed",
            detail: NETWORK_ERROR_MESSAGE,
          });
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          addAssistant(NETWORK_ERROR_MESSAGE);
        }
      } finally {
        if (activeStreamRef.current === controller) {
          activeStreamRef.current = null;
        }
        streamingRef.current = false;
        if (mountedRef.current) {
          setIsStreaming(false);
        }
      }
    },
    [
      addAssistant,
      addMessage,
      beginStatus,
      conversationId,
      handleStreamResponse,
      isLoadingHistory,
      pendingAction,
      updateStatus,
    ],
  );

  const approve = useCallback(
    async (approved: boolean) => {
      const action = pendingAction;
      if (!action || streamingRef.current) {
        return;
      }

      setPendingAction(null);
      updateStatus({
        state: "executing",
        label: "Executing approved action...",
        tool: action.actions[0]?.tool,
      });
      streamingRef.current = true;
      setIsStreaming(true);

      const controller = new AbortController();
      activeStreamRef.current = controller;

      try {
        const response = await postApproval(
          action.approval_id,
          approved,
          controller.signal,
        );
        await handleStreamResponse(response, controller.signal);
      } catch (error) {
        if (!isAbortError(error) && mountedRef.current) {
          updateStatus({
            state: "error",
            label: "Connection failed",
            detail: NETWORK_ERROR_MESSAGE,
          });
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          addAssistant(NETWORK_ERROR_MESSAGE);
        }
      } finally {
        if (activeStreamRef.current === controller) {
          activeStreamRef.current = null;
        }
        streamingRef.current = false;
        if (mountedRef.current) {
          setIsStreaming(false);
        }
      }
    },
    [addAssistant, handleStreamResponse, pendingAction, updateStatus],
  );

  const clearChat = useCallback(async () => {
    if (streamingRef.current || isLoadingHistory) {
      return;
    }

    const previousId = conversationId;

    setMessages([]);
    setConversationId(null);
    setPendingAction(null);
    activeStatusIdRef.current = null;
    lastStatusStateRef.current = null;
    pendingFilesRef.current.clear();
    usedSemanticSearchRef.current = false;
    storeConversationId(null);

    if (!previousId) {
      return;
    }

    await deleteConversation(previousId);
  }, [conversationId, isLoadingHistory]);

  const refresh = useCallback(async () => {
    if (streamingRef.current) {
      return;
    }

    const controller = new AbortController();
    loadControllerRef.current?.abort();
    loadControllerRef.current = controller;
    setIsLoadingHistory(true);

    try {
      const snapshot = await loadSnapshot(
        readStoredConversationId(),
        controller.signal,
      );
      if (!mountedRef.current || controller.signal.aborted) {
        return;
      }
      setIdentity((current) => snapshot.identity ?? current);
      setMessages(snapshot.messages);
      setPendingAction(snapshot.pending);
      activeStatusIdRef.current = null;
      lastStatusStateRef.current = null;
    } finally {
      if (loadControllerRef.current === controller) {
        loadControllerRef.current = null;
        if (mountedRef.current) {
          setIsLoadingHistory(false);
        }
      }
    }
  }, []);

  return useMemo(
    () => ({
      messages,
      identity,
      pendingAction,
      conversationId,
      isStreaming,
      isLoadingHistory,
      isBusy: isStreaming || isLoadingHistory,
      canSend: !isStreaming && !isLoadingHistory && pendingAction === null,
      sendMessage,
      approve,
      clearChat,
      refresh,
    }),
    [
      approve,
      clearChat,
      conversationId,
      identity,
      isLoadingHistory,
      isStreaming,
      messages,
      pendingAction,
      refresh,
      sendMessage,
    ],
  );
}
