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
  StatusKind,
  StreamCallbacks,
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

  const addMessage = useCallback(
    (
      role: ChatRole,
      content: string,
      statusKind?: StatusKind,
      files?: string[],
    ) => {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role,
          content,
          statusKind,
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

  const addStatus = useCallback(
    (content: string, statusKind: StatusKind) =>
      addMessage("status", content, statusKind),
    [addMessage],
  );

  const addAttachments = useCallback(
    (files: string[]) => {
      const unique = [...new Set(files)];
      if (unique.length > 0) {
        addMessage("files", "", undefined, unique);
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
          addAssistant(
            describeHttpError(response.status, response.statusText, body),
          );
        }
        return;
      }

      const callbacks: StreamCallbacks = {
        onStatus: (message, phase) => {
          if (!mountedRef.current) {
            return;
          }
          if (phase === "tool_started") {
            addStatus(message, "tool");
          } else if (phase === "approval") {
            addStatus(message, "approval");
          } else {
            addStatus(message, "thinking");
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

          addStatus(summary, ok ? "result-ok" : "result-error");
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
          addStatus("Waiting for your approval.", "approval");
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

          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          rememberConversation(id);
          setPendingAction(null);
        },
        onError: (message) => {
          if (!mountedRef.current) {
            return;
          }
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          addAssistant(message);
          setPendingAction(null);
        },
      };

      const terminal = await consumeEventStream(response, callbacks, signal);
      if (!terminal && mountedRef.current) {
        addAssistant(STREAM_CLOSED_MESSAGE);
      }
    },
    [addAssistant, addAttachments, addStatus, rememberConversation],
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
      conversationId,
      handleStreamResponse,
      isLoadingHistory,
      pendingAction,
    ],
  );

  const approve = useCallback(
    async (approved: boolean) => {
      const action = pendingAction;
      if (!action || streamingRef.current) {
        return;
      }

      setPendingAction(null);
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
    [addAssistant, handleStreamResponse, pendingAction],
  );

  const clearChat = useCallback(async () => {
    if (streamingRef.current || isLoadingHistory) {
      return;
    }

    const previousId = conversationId;

    setMessages([]);
    setConversationId(null);
    setPendingAction(null);
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
