import { useEffect, useRef } from "react";

import { AppHeader } from "./components/AppHeader";
import { ApprovalCard } from "./components/ApprovalCard";
import { ChatComposer } from "./components/ChatComposer";
import { EmptyState } from "./components/EmptyState";
import { MessageItem, TypingIndicator } from "./components/MessageItem";
import { StatusPill } from "./components/StatusPill";
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
    clearChat,
    refresh,
  } = useAgentChat();

  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isStreaming, pendingAction]);

  const isEmpty = messages.length === 0;

  return (
    <div className="app-frame flex h-dvh w-full min-w-0 flex-col overflow-hidden rounded-[26px] p-px">
      <div className="flex h-full w-full min-w-0 flex-col overflow-hidden rounded-[25px] bg-[#141414]">
        <AppHeader
          identity={identity}
          isWorking={isStreaming}
          isBusy={isBusy}
          canClear={!isEmpty}
          onRefresh={() => void refresh()}
          onClear={() => void clearChat()}
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
            <EmptyState
              disabled={!canSend}
              onPrompt={(prompt) => void sendMessage(prompt)}
            />
          ) : (
            <div className="mx-auto w-full max-w-3xl space-y-5 px-4 py-5 sm:px-6">
              {messages.map((message) =>
                message.role === "status" ? (
                  <StatusPill
                    key={message.id}
                    kind={message.statusKind ?? "thinking"}
                    content={message.content}
                  />
                ) : (
                  <MessageItem key={message.id} message={message} />
                ),
              )}

              {pendingAction && (
                <ApprovalCard
                  pending={pendingAction}
                  busy={isStreaming}
                  onApprove={() => void approve(true)}
                  onDeny={() => void approve(false)}
                />
              )}

              {isStreaming && <TypingIndicator />}

              <div ref={messagesEndRef} className="h-px" />
            </div>
          )}
        </main>

        <ChatComposer
          onSend={(text) => void sendMessage(text)}
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
