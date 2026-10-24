import { useCallback, useEffect, useRef, useState } from "react";

import {
  NETWORK_ERROR_MESSAGE,
  STREAM_CLOSED_MESSAGE,
  consumeEventStream,
  deleteConversation,
  describeHttpError,
  fetchHistory,
  fetchIdentity,
  fetchPending,
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

async function loadSnapshot(storedId: string | null): Promise<Snapshot> {
  const [identity, history, pending] = await Promise.all([
    fetchIdentity(),
    storedId ? fetchHistory(storedId) : Promise.resolve([]),
    storedId ? fetchPending(storedId) : Promise.resolve(null),
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
    let cancelled = false;

    const load = async () => {
      const snapshot = await loadSnapshot(readStoredConversationId());
      if (cancelled) {
        return;
      }

      setIdentity(snapshot.identity);
      setMessages(snapshot.messages);
      setPendingAction(snapshot.pending);
      setIsLoadingHistory(false);
    };

    void load();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleStreamResponse = useCallback(
    async (response: Response): Promise<void> => {
      if (!response.ok) {
        let body: unknown = null;

        try {
          body = await response.json();
        } catch {
          // Non-JSON error body; fall back to the status in the message.
        }

        addAssistant(
          describeHttpError(response.status, response.statusText, body),
        );
        return;
      }

      const callbacks: StreamCallbacks = {
        onStatus: (message, phase) => {
          if (phase === "tool_started") {
            addStatus(message, "tool");
          } else if (phase === "approval") {
            addStatus(message, "approval");
          } else {
            addStatus(message, "thinking");
          }
        },
        onToolResult: (ok, summary, tool, result) => {
          const files = extractToolFiles(tool, result);
          files.forEach((file) => pendingFilesRef.current.add(file));

          if (tool === "semantic_image_search") {
            usedSemanticSearchRef.current = true;
          }

          addStatus(summary, ok ? "result-ok" : "result-error");
        },
        onAssistant: addAssistant,
        onActionRequired: (action) => {
          setPendingAction(action);
          rememberConversation(action.conversation_id);
          addStatus("Waiting for your approval.", "approval");
        },
        onFinal: (reply, id) => {
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
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          addAssistant(message);
          setPendingAction(null);
        },
      };

      const terminal = await consumeEventStream(response, callbacks);
      if (!terminal) {
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
        isStreaming ||
        isLoadingHistory ||
        pendingAction !== null
      ) {
        return;
      }

      addMessage("user", trimmed);
      pendingFilesRef.current.clear();
      usedSemanticSearchRef.current = false;
      setIsStreaming(true);

      try {
        let response: Response;

        try {
          response = await postChatStream(trimmed, conversationId);
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
    },
    [
      addAssistant,
      addMessage,
      conversationId,
      handleStreamResponse,
      isLoadingHistory,
      isStreaming,
      pendingAction,
    ],
  );

  const approve = useCallback(
    async (approved: boolean) => {
      const action = pendingAction;
      if (!action || isStreaming) {
        return;
      }

      setPendingAction(null);
      setIsStreaming(true);

      try {
        let response: Response;

        try {
          response = await postApproval(action.approval_id, approved);
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
    },
    [addAssistant, handleStreamResponse, isStreaming, pendingAction],
  );

  const clearChat = useCallback(async () => {
    if (isStreaming || isLoadingHistory) {
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
  }, [conversationId, isLoadingHistory, isStreaming]);

  const refresh = useCallback(async () => {
    if (isStreaming) {
      return;
    }

    setIsLoadingHistory(true);

    try {
      const snapshot = await loadSnapshot(readStoredConversationId());
      setIdentity((current) => snapshot.identity ?? current);
      setMessages(snapshot.messages);
      setPendingAction(snapshot.pending);
    } finally {
      setIsLoadingHistory(false);
    }
  }, [isStreaming]);

  return {
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
  };
}
