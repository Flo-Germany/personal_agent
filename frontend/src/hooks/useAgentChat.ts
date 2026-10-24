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
  postInterrupt,
  readStoredConversationId,
  storeConversationId,
  waitForBackend,
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
  TaskStep,
  TokenMetrics,
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
  let identity = await fetchIdentity(signal);

  if (!identity) {
    await waitForBackend(signal);
    identity = await fetchIdentity(signal);
  }

  const [history, pending] = await Promise.all([
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
  const activeConversationIdRef = useRef<string | null>(null);
  const interruptingRef = useRef(false);
  const loadControllerRef = useRef<AbortController | null>(null);
  const streamingRef = useRef(false);
  const mountedRef = useRef(true);
  const activeStatusIdRef = useRef<string | null>(null);
  const lastStatusStateRef = useRef<ToolStatusData["state"] | null>(null);
  const lastStatusRef = useRef<ToolStatusData | null>(null);
  const statusTasksRef = useRef<TaskStep[]>([]);
  const statusStartedAtRef = useRef<number | null>(null);
  const statusFinishedAtRef = useRef<number | null>(null);
  const plannedTotalRef = useRef(0);
  const streamingMessageIdRef = useRef<string | null>(null);
  const lastStreamedIdRef = useRef<string | null>(null);
  const lastStreamedContentRef = useRef("");

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
    lastStatusRef.current = status;

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

  const resetTaskRefs = useCallback(() => {
    statusTasksRef.current = [];
    statusStartedAtRef.current = null;
    statusFinishedAtRef.current = null;
    plannedTotalRef.current = 0;
  }, []);

  const syncTaskStatus = useCallback(
    (overrides: Partial<ToolStatusData> = {}) => {
      const tasks = statusTasksRef.current;
      const running = tasks.find((task) => task.state === "running");
      const failed = tasks.filter((task) => task.state === "failed").length;
      const completed = tasks.filter((task) => task.state === "done").length;
      const base: ToolStatusData = lastStatusRef.current ?? {
        state: "executing",
        label: "Working...",
      };
      const state =
        overrides.state ??
        (failed > 0 ? "error" : running ? "executing" : "success");
      const label =
        overrides.label ??
        (running
          ? running.label
          : failed > 0
            ? `${completed} done · ${failed} failed`
            : `${completed} task${completed === 1 ? "" : "s"} completed`);

      updateStatus({
        ...base,
        ...overrides,
        state,
        label,
        tasks,
        plannedTotal: plannedTotalRef.current || undefined,
        startedAt: statusStartedAtRef.current ?? undefined,
        finishedAt: statusFinishedAtRef.current ?? undefined,
      });
    },
    [updateStatus],
  );

  const failRunningTasks = useCallback(
    (message: string, label = "Agent error"): boolean => {
      const tasks = statusTasksRef.current;
      if (tasks.length === 0) {
        return false;
      }

      const finishedAt = Date.now();
      statusTasksRef.current = tasks.map((task) =>
        task.state === "running" || task.state === "pending"
          ? { ...task, state: "failed", detail: message, finishedAt }
          : task,
      );
      statusFinishedAtRef.current = finishedAt;
      syncTaskStatus({ state: "error", label, finishedAt });
      return true;
    },
    [syncTaskStatus],
  );

  const resetStreamingRefs = useCallback(() => {
    streamingMessageIdRef.current = null;
    lastStreamedIdRef.current = null;
    lastStreamedContentRef.current = "";
  }, []);

  const applyMetrics = useCallback((metrics: TokenMetrics) => {
    if (!mountedRef.current) {
      return;
    }

    const id = streamingMessageIdRef.current ?? lastStreamedIdRef.current;
    if (!id) {
      return;
    }

    setMessages((current) => {
      if (!current.some((message) => message.id === id)) {
        return current;
      }
      return current.map((message) =>
        message.id === id ? { ...message, metrics } : message,
      );
    });
  }, []);

  const finalizeStreaming = useCallback(() => {
    const id = streamingMessageIdRef.current;
    streamingMessageIdRef.current = null;
    lastStreamedIdRef.current = null;
    lastStreamedContentRef.current = "";

    if (!id || !mountedRef.current) {
      return;
    }

    setMessages((current) =>
      current.map((message) =>
        message.id === id ? { ...message, streaming: false } : message,
      ),
    );
  }, []);

  const handleInterrupted = useCallback(() => {
    if (!mountedRef.current) {
      return;
    }

    finalizeStreaming();
    const finishedAt = Date.now();
    const tasks = statusTasksRef.current;

    if (tasks.length > 0) {
      statusTasksRef.current = tasks.map((task) =>
        task.state === "running" || task.state === "pending"
          ? { ...task, state: "cancelled" as const, finishedAt }
          : task,
      );
      statusFinishedAtRef.current = finishedAt;
      syncTaskStatus({
        state: "cancelled",
        label: "Interrupted",
        finishedAt,
      });
    } else {
      updateStatus({
        state: "cancelled",
        label: "Interrupted",
        tasks: [],
        startedAt: statusStartedAtRef.current ?? undefined,
        finishedAt,
      });
    }

    activeStatusIdRef.current = null;
    lastStatusStateRef.current = null;
    lastStatusRef.current = null;
    resetTaskRefs();
    pendingFilesRef.current.clear();
    usedSemanticSearchRef.current = false;
    setPendingAction(null);
    interruptingRef.current = false;
  }, [
    finalizeStreaming,
    resetTaskRefs,
    syncTaskStatus,
    updateStatus,
  ]);

  const beginStatus = useCallback(() => {
    resetStreamingRefs();
    resetTaskRefs();
    statusStartedAtRef.current = Date.now();
    activeStatusIdRef.current = null;
    updateStatus({
      state: "thinking",
      label: "Thinking...",
      startedAt: statusStartedAtRef.current,
    });
  }, [resetStreamingRefs, resetTaskRefs, updateStatus]);

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
      activeConversationIdRef.current = readStoredConversationId();
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
          finalizeStreaming();
          const detail = describeHttpError(
            response.status,
            response.statusText,
            body,
          );
          if (!failRunningTasks(detail, "Agent error")) {
            updateStatus({
              state: "error",
              label: "Agent error",
              detail,
              tasks: [],
              startedAt: statusStartedAtRef.current ?? undefined,
              finishedAt: Date.now(),
            });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          lastStatusRef.current = null;
          resetTaskRefs();
        }
        return;
      }

      const callbacks: StreamCallbacks = {
        onStatus: (message, phase, tool) => {
          if (!mountedRef.current) {
            return;
          }

          if (phase === "tool_started") {
            if (tool === "propose_plan") {
              return;
            }
            const task: TaskStep = {
              id: crypto.randomUUID(),
              label: message,
              tool,
              state: "running",
              startedAt: Date.now(),
            };
            statusTasksRef.current = [...statusTasksRef.current, task];
            syncTaskStatus({ state: "executing", label: message });
            return;
          }

          if (phase === "approval") {
            if (message.toLowerCase().includes("cancel")) {
              const finishedAt = Date.now();
              statusTasksRef.current = statusTasksRef.current.map((task) =>
                task.state === "running"
                  ? { ...task, state: "cancelled", finishedAt }
                  : task,
              );
              syncTaskStatus({ state: "cancelled", label: "Action cancelled" });
            } else {
              syncTaskStatus({ state: "executing", label: message });
            }
            return;
          }

          if (statusTasksRef.current.length > 0) {
            syncTaskStatus({
              state: phase === "generating" ? "executing" : "thinking",
              label: phase === "generating" ? "Writing answer..." : message,
            });
            return;
          }

          const previous = lastStatusStateRef.current;
          if (
            phase !== "generating" &&
            (previous === "success" ||
              previous === "error" ||
              previous === "waiting" ||
              previous === "cancelled")
          ) {
            // Keep the meaningful result visible while the model reads it.
            return;
          }
          updateStatus({
            state: "thinking",
            label: message,
            tasks: [],
            startedAt: statusStartedAtRef.current ?? undefined,
          });
        },
        onToolResult: (ok, summary, tool, result) => {
          if (!mountedRef.current) {
            return;
          }
          if (tool === "propose_plan") {
            return;
          }
          const files = extractToolFiles(tool, result);
          files.forEach((file) => pendingFilesRef.current.add(file));

          if (tool === "semantic_image_search") {
            usedSemanticSearchRef.current = true;
          }

          const cancelled = Boolean(result?.cancelled);
          const taskState: TaskStep["state"] = cancelled
            ? "cancelled"
            : ok
              ? "done"
              : "failed";
          const failure = toolFailureDetails(result);
          const finishedAt = Date.now();
          const tasks = [...statusTasksRef.current];
          let index = -1;

          for (let cursor = tasks.length - 1; cursor >= 0; cursor -= 1) {
            const task = tasks[cursor];
            if (
              task.state === "running" &&
              (tool === undefined || task.tool === tool)
            ) {
              index = cursor;
              break;
            }
          }

          const failureLabel =
            tool === "run_powershell_action"
              ? "Action failed"
              : tool === "run_powershell_query"
                ? "Query failed"
                : "Tool failed";

          if (index >= 0) {
            const task = tasks[index];
            tasks[index] = {
              ...task,
              state: taskState,
              label: ok ? summary || task.label : task.label,
              tool: task.tool ?? tool,
              detail:
                taskState === "failed"
                  ? (failure.detail ?? summary)
                  : taskState === "cancelled"
                    ? summary
                    : undefined,
              exitCode: taskState === "failed" ? failure.exitCode : undefined,
              finishedAt,
            };
          } else {
            tasks.push({
              id: crypto.randomUUID(),
              label:
                taskState === "failed"
                  ? failureLabel
                  : summary || (tool ? `Completed ${tool}` : "Task completed"),
              tool,
              state: taskState,
              detail:
                taskState === "failed"
                  ? (failure.detail ?? summary)
                  : taskState === "cancelled"
                    ? summary
                    : undefined,
              exitCode: taskState === "failed" ? failure.exitCode : undefined,
              startedAt: finishedAt,
              finishedAt,
            });
          }

          statusTasksRef.current = tasks;
          syncTaskStatus({
            state:
              taskState === "failed"
                ? "error"
                : taskState === "cancelled"
                  ? "cancelled"
                  : "executing",
            label:
              taskState === "failed"
                ? failureLabel
                : taskState === "cancelled"
                  ? "Action cancelled"
                  : summary || "Task completed",
          });
        },
        onAssistant: (content) => {
          if (mountedRef.current) {
            addAssistant(content);
          }
        },
        onAssistantStart: () => {
          if (!mountedRef.current) {
            return;
          }
          const id = crypto.randomUUID();
          streamingMessageIdRef.current = id;
          lastStreamedIdRef.current = id;
          lastStreamedContentRef.current = "";
          setMessages((current) => [
            ...current,
            {
              id,
              role: "assistant",
              content: "",
              streaming: true,
            },
          ]);
        },
        onAssistantDelta: (content) => {
          if (!mountedRef.current) {
            return;
          }
          const id = streamingMessageIdRef.current;
          if (!id) {
            return;
          }
          lastStreamedContentRef.current += content;
          setMessages((current) =>
            current.map((message) =>
              message.id === id
                ? { ...message, content: message.content + content }
                : message,
            ),
          );
        },
        onAssistantEnd: (content) => {
          if (!mountedRef.current) {
            return;
          }
          const id = streamingMessageIdRef.current;
          if (!id) {
            return;
          }
          const finalContent = content ?? lastStreamedContentRef.current;
          lastStreamedContentRef.current = finalContent;
          streamingMessageIdRef.current = null;
          setMessages((current) =>
            current.map((message) =>
              message.id === id
                ? { ...message, content: finalContent, streaming: false }
                : message,
            ),
          );
        },
        onAssistantReset: () => {
          const id = lastStreamedIdRef.current;
          if (streamingMessageIdRef.current === id) {
            streamingMessageIdRef.current = null;
          }
          lastStreamedIdRef.current = null;
          lastStreamedContentRef.current = "";
          if (!id || !mountedRef.current) {
            return;
          }
          setMessages((current) =>
            current.filter((message) => message.id !== id),
          );
        },
        onMetrics: applyMetrics,
        onCancelled: handleInterrupted,
        onActionRequired: (action) => {
          if (!mountedRef.current) {
            return;
          }
          setPendingAction(action);
          rememberConversation(action.conversation_id);
          plannedTotalRef.current = Math.max(
            action.plan?.length ?? 0,
            action.actions.length,
          );

          if (statusTasksRef.current.length > 0) {
            syncTaskStatus({
              state: "waiting",
              label: "Waiting for approval",
            });
          } else {
            updateStatus({
              state: "waiting",
              label: "Waiting for approval",
              tool: action.actions[0]?.tool,
              tasks: [],
              plannedTotal: plannedTotalRef.current || undefined,
              startedAt: statusStartedAtRef.current ?? undefined,
            });
          }
        },
        onFinal: (reply, id, streamed) => {
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

          const streamedId = lastStreamedIdRef.current;
          const alreadyStreamed =
            streamedId !== null &&
            (streamed === true ||
              lastStreamedContentRef.current.trim() === text.trim());

          if (text.trim()) {
            if (alreadyStreamed && streamedId) {
              // Replace the raw stream with the normalized final text.
              setMessages((current) =>
                current.map((message) =>
                  message.id === streamedId
                    ? { ...message, content: text, streaming: false }
                    : message,
                ),
              );
            } else {
              addAssistant(text);
            }
          }

          if (missingFiles.length > 0) {
            addAttachments(missingFiles);
          }

          statusFinishedAtRef.current = Date.now();
          const tasks = statusTasksRef.current;
          const statusId = activeStatusIdRef.current;
          const statusState = lastStatusStateRef.current;
          const completed = tasks.filter((task) => task.state === "done").length;
          const failedCount = tasks.filter(
            (task) => task.state === "failed",
          ).length;
          const cancelledCount = tasks.filter(
            (task) => task.state === "cancelled",
          ).length;
          const summaryParts: string[] = [];
          if (completed > 0) {
            summaryParts.push(`${completed} completed`);
          }
          if (failedCount > 0) {
            summaryParts.push(`${failedCount} failed`);
          }
          if (cancelledCount > 0) {
            summaryParts.push(`${cancelledCount} cancelled`);
          }

          if (tasks.length > 0) {
            syncTaskStatus({
              state:
                failedCount > 0
                  ? "error"
                  : cancelledCount > 0
                    ? "cancelled"
                    : "success",
              label:
                failedCount > 0 || cancelledCount > 0
                  ? summaryParts.join(" · ")
                  : "All tasks completed",
              finishedAt: statusFinishedAtRef.current ?? undefined,
            });
          } else if (statusState === "thinking" && statusId) {
            // A simple text answer does not need a permanent "Completed" row.
            setMessages((current) =>
              current.filter((message) => message.id !== statusId),
            );
          } else if (statusState === "executing" && statusId) {
            updateStatus({
              state: "success",
              label: "Completed",
              tasks: [],
              startedAt: statusStartedAtRef.current ?? undefined,
              finishedAt: statusFinishedAtRef.current,
            });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          lastStatusRef.current = null;
          resetTaskRefs();
          resetStreamingRefs();
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          rememberConversation(id);
          setPendingAction(null);
        },
        onError: (message) => {
          if (!mountedRef.current) {
            return;
          }
          finalizeStreaming();
          if (!failRunningTasks(message, "Agent error")) {
            updateStatus({
              state: "error",
              label: "Agent error",
              detail: message,
              tasks: [],
              startedAt: statusStartedAtRef.current ?? undefined,
              finishedAt: Date.now(),
            });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          lastStatusRef.current = null;
          resetTaskRefs();
          pendingFilesRef.current.clear();
          usedSemanticSearchRef.current = false;
          addAssistant(message);
          setPendingAction(null);
        },
      };

      const terminal = await consumeEventStream(response, callbacks, signal);
      if (!terminal && mountedRef.current) {
        finalizeStreaming();
        if (!failRunningTasks(STREAM_CLOSED_MESSAGE, "Connection closed")) {
          updateStatus({
            state: "error",
            label: "Connection closed",
            detail: STREAM_CLOSED_MESSAGE,
            tasks: [],
            startedAt: statusStartedAtRef.current ?? undefined,
            finishedAt: Date.now(),
          });
        }
        activeStatusIdRef.current = null;
        lastStatusStateRef.current = null;
        lastStatusRef.current = null;
        resetTaskRefs();
        addAssistant(STREAM_CLOSED_MESSAGE);
      }
    },
    [
      addAssistant,
      addAttachments,
      applyMetrics,
      failRunningTasks,
      finalizeStreaming,
      handleInterrupted,
      rememberConversation,
      resetStreamingRefs,
      resetTaskRefs,
      syncTaskStatus,
      updateStatus,
    ],
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
        const streamConversationId = response.headers.get(
          "X-Conversation-ID",
        );
        if (streamConversationId) {
          activeConversationIdRef.current = streamConversationId;
          rememberConversation(streamConversationId);
        }
        await handleStreamResponse(response, controller.signal);
      } catch (error) {
        if (interruptingRef.current) {
          handleInterrupted();
        } else if (!isAbortError(error) && mountedRef.current) {
          finalizeStreaming();
          if (!failRunningTasks(NETWORK_ERROR_MESSAGE, "Connection failed")) {
            updateStatus({
              state: "error",
              label: "Connection failed",
              detail: NETWORK_ERROR_MESSAGE,
              tasks: [],
              startedAt: statusStartedAtRef.current ?? undefined,
              finishedAt: Date.now(),
            });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          lastStatusRef.current = null;
          resetTaskRefs();
          addAssistant(NETWORK_ERROR_MESSAGE);
        }
      } finally {
        if (activeStreamRef.current === controller) {
          activeStreamRef.current = null;
        }
        interruptingRef.current = false;
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
      failRunningTasks,
      finalizeStreaming,
      handleInterrupted,
      handleStreamResponse,
      isLoadingHistory,
      pendingAction,
      rememberConversation,
      resetTaskRefs,
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
      resetStreamingRefs();
      syncTaskStatus({
        state: approved ? "executing" : "cancelled",
        label: approved ? "Executing approved action..." : "Action cancelled",
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
        const streamConversationId = response.headers.get(
          "X-Conversation-ID",
        );
        if (streamConversationId) {
          activeConversationIdRef.current = streamConversationId;
          rememberConversation(streamConversationId);
        }
        await handleStreamResponse(response, controller.signal);
      } catch (error) {
        if (interruptingRef.current) {
          handleInterrupted();
        } else if (!isAbortError(error) && mountedRef.current) {
          finalizeStreaming();
          if (!failRunningTasks(NETWORK_ERROR_MESSAGE, "Connection failed")) {
            updateStatus({
              state: "error",
              label: "Connection failed",
              detail: NETWORK_ERROR_MESSAGE,
              tasks: [],
              startedAt: statusStartedAtRef.current ?? undefined,
              finishedAt: Date.now(),
            });
          }
          activeStatusIdRef.current = null;
          lastStatusStateRef.current = null;
          lastStatusRef.current = null;
          resetTaskRefs();
          addAssistant(NETWORK_ERROR_MESSAGE);
        }
      } finally {
        if (activeStreamRef.current === controller) {
          activeStreamRef.current = null;
        }
        interruptingRef.current = false;
        streamingRef.current = false;
        if (mountedRef.current) {
          setIsStreaming(false);
        }
      }
    },
    [
      addAssistant,
      failRunningTasks,
      handleInterrupted,
      handleStreamResponse,
      pendingAction,
      rememberConversation,
      resetStreamingRefs,
      resetTaskRefs,
      syncTaskStatus,
      updateStatus,
    ],
  );

  const interrupt = useCallback(async () => {
    if (!streamingRef.current || interruptingRef.current) {
      return;
    }

    interruptingRef.current = true;
    const id =
      activeConversationIdRef.current ??
      conversationId ??
      pendingAction?.conversation_id ??
      null;

    try {
      if (id) {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 2000);
        try {
          await postInterrupt(id, controller.signal);
        } finally {
          window.clearTimeout(timer);
        }
      }
    } catch {
      // The local abort below still stops the stream.
    }

    activeStreamRef.current?.abort();
  }, [conversationId, pendingAction]);

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
    lastStatusRef.current = null;
    resetTaskRefs();
    resetStreamingRefs();
    activeConversationIdRef.current = null;
    interruptingRef.current = false;
    pendingFilesRef.current.clear();
    usedSemanticSearchRef.current = false;
    storeConversationId(null);

    if (!previousId) {
      return;
    }

    await deleteConversation(previousId);
  }, [conversationId, isLoadingHistory, resetStreamingRefs, resetTaskRefs]);

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
      lastStatusRef.current = null;
      activeConversationIdRef.current = readStoredConversationId();
      interruptingRef.current = false;
      resetTaskRefs();
      resetStreamingRefs();
    } finally {
      if (loadControllerRef.current === controller) {
        loadControllerRef.current = null;
        if (mountedRef.current) {
          setIsLoadingHistory(false);
        }
      }
    }
  }, [resetStreamingRefs, resetTaskRefs]);

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
      interrupt,
      clearChat,
      refresh,
    }),
    [
      approve,
      clearChat,
      conversationId,
      identity,
      interrupt,
      isLoadingHistory,
      isStreaming,
      messages,
      pendingAction,
      refresh,
      sendMessage,
    ],
  );
}
