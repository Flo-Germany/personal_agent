import { useCallback, useEffect, useMemo, useRef } from "react";

import { AppHeader } from "./components/AppHeader";
import { ApprovalCard } from "./components/ApprovalCard";
import { AttachmentBlock } from "./components/AttachmentBlock";
import { ChatComposer } from "./components/ChatComposer";
import { DisplayPickerCard } from "./components/DisplayPickerCard";
import { EmptyState } from "./components/EmptyState";
import { MessageItem } from "./components/MessageItem";
import { ToolStatus } from "./components/ToolStatus";
import { useAgentChat } from "./hooks/useAgentChat";

function App() {
  const {
    messages,
    identity,
    pendingAction,
    isStreaming,
    isLoadingHistory,
    isBusy,
    canSend,
    sendMessage,
    approve,
    chooseDisplay,
    interrupt,
    clearChat,
    refresh,
  } = useAgentChat();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastContentLength = messages[messages.length - 1]?.content.length ?? 0;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: isStreaming ? "auto" : "smooth",
      block: "end",
    });
  }, [messages.length, lastContentLength, isStreaming, pendingAction]);

  const isEmpty = useMemo(() => messages.length === 0, [messages.length]);

  const handleSend = useCallback(
    (text: string) => {
      void sendMessage(text);
    },
    [sendMessage],
  );

  const handlePrompt = useCallback(
    (prompt: string) => {
      void sendMessage(prompt);
    },
    [sendMessage],
  );

  const handleRefresh = useCallback(() => {
    void refresh();
  }, [refresh]);

  const handleClear = useCallback(() => {
    void clearChat();
  }, [clearChat]);

  const handleApprove = useCallback(() => {
    void approve(true);
  }, [approve]);

  const handleDeny = useCallback(() => {
    void approve(false);
  }, [approve]);

  const handleChooseDisplay = useCallback(
    (display: number) => {
      void chooseDisplay(display);
    },
    [chooseDisplay],
  );

  const handleCancelDisplay = useCallback(() => {
    void chooseDisplay(null);
  }, [chooseDisplay]);

  const handleInterrupt = useCallback(() => {
    void interrupt();
  }, [interrupt]);

  return (
    <div className="app-frame flex h-dvh w-full min-w-0 flex-col overflow-hidden rounded-[26px] p-px">
      <div className="flex h-full w-full min-w-0 flex-col overflow-hidden rounded-[25px] bg-[#141414]">
        <AppHeader
          identity={identity}
          isWorking={isStreaming}
          isBusy={isBusy}
          canClear={!isEmpty}
          onRefresh={handleRefresh}
          onClear={handleClear}
        />

        <main
          aria-live="polite"
          className="relative min-w-0 flex-1 overflow-y-auto"
        >
          {isEmpty && isLoadingHistory ? (
            <div className="flex min-h-full items-center justify-center gap-2 text-[12px] text-white/30">
              <span className="size-3 animate-spin rounded-full border border-white/15 border-t-white/50" />
              Loading conversation…
            </div>
          ) : isEmpty ? (
            <EmptyState disabled={!canSend} onPrompt={handlePrompt} />
          ) : (
            <div className="mx-auto w-full max-w-3xl space-y-5 px-4 py-5 sm:px-6">
              {messages.map((message) =>
                message.role === "status" ? (
                  <ToolStatus
                    key={message.id}
                    status={
                      message.status ?? {
                        state: "thinking",
                        label: "Thinking...",
                      }
                    }
                  />
                ) : message.role === "files" ? (
                  <AttachmentBlock
                    key={message.id}
                    files={message.files ?? []}
                  />
                ) : (
                  <MessageItem key={message.id} message={message} />
                ),
              )}

              {pendingAction && pendingAction.kind === "display" ? (
                <DisplayPickerCard
                  pending={pendingAction}
                  busy={isStreaming}
                  onChoose={handleChooseDisplay}
                  onCancel={handleCancelDisplay}
                />
              ) : pendingAction ? (
                <ApprovalCard
                  pending={pendingAction}
                  busy={isStreaming}
                  onApprove={handleApprove}
                  onDeny={handleDeny}
                />
              ) : null}

              <div ref={messagesEndRef} className="h-px" />
            </div>
          )}
        </main>

        <ChatComposer
          onSend={handleSend}
          onInterrupt={handleInterrupt}
          canSend={canSend}
          isStreaming={isStreaming}
          isLoadingHistory={isLoadingHistory}
          pending={pendingAction !== null}
        />
      </div>
    </div>
  );
}

export default App;
