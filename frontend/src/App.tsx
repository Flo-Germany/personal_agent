import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AppHeader } from "./components/AppHeader";
import { ApprovalCard } from "./components/ApprovalCard";
import { AttachmentBlock } from "./components/AttachmentBlock";
import { ChatComposer } from "./components/ChatComposer";
import { DesktopSettingsSection } from "./components/DesktopSettings";
import { DisplayPickerCard } from "./components/DisplayPickerCard";
import { EmptyState } from "./components/EmptyState";
import { MessageItem } from "./components/MessageItem";
import { ModelSettings } from "./components/ModelSettings";
import { ModernPalette } from "./components/ModernPalette";
import { SearchResults } from "./components/SearchResults";
import { ToolStatus } from "./components/ToolStatus";
import { useAgentChat } from "./hooks/useAgentChat";
import { useDesktop } from "./hooks/useDesktop";
import { useFrameStats } from "./hooks/useFrameStats";
import { onAnalyze, onQaRender, onSummon, onUi, takePendingAnalyze } from "./lib/desktop";

const STRESS_DURATION_MS = 3400;

function App() {
  const [view, setView] = useState<"chat" | "settings">("chat");
  const [stress, setStress] = useState(false);
  const desktop = useDesktop();
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
    refreshIdentity,
  } = useAgentChat();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastMessage = messages[messages.length - 1];
  const lastContentLength = lastMessage?.content.length ?? 0;
  // Reasoning grows inside the status row / message while content stays empty,
  // so it needs its own scroll trigger.
  const lastThinkingLength =
    lastMessage?.status?.thinking?.length ?? lastMessage?.thinking?.length ?? 0;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: isStreaming ? "auto" : "smooth",
      block: "end",
    });
  }, [
    messages.length,
    lastContentLength,
    lastThinkingLength,
    isStreaming,
    pendingAction,
  ]);

  const isEmpty = useMemo(() => messages.length === 0, [messages.length]);

  // -------------------------------------------------- Explorer / file analyze
  const pendingAnalyze = useRef<string | null>(null);
  const [analyzeTick, setAnalyzeTick] = useState(0);

  useEffect(() => {
    void takePendingAnalyze().then((path) => {
      if (path) {
        pendingAnalyze.current = path;
        setAnalyzeTick((tick) => tick + 1);
      }
    });
    let unlisten: (() => void) | undefined;
    void onAnalyze((path) => {
      pendingAnalyze.current = path;
      setAnalyzeTick((tick) => tick + 1);
    }).then((off) => {
      unlisten = off;
    });
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    if (!analyzeTick || isLoadingHistory || !canSend) {
      return;
    }
    const path = pendingAnalyze.current;
    if (!path) {
      return;
    }
    pendingAnalyze.current = null;
    setView("chat");
    void sendMessage(
      `Analyze the file at ${path} and tell me what it is and what matters.`,
    );
  }, [analyzeTick, canSend, isLoadingHistory, sendMessage]);

  // ------------------------------------------------------- QA rendering probe
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void onQaRender(() => setStress(true)).then((off) => {
      unlisten = off;
    });
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    if (!stress) {
      return;
    }
    const timer = window.setTimeout(() => setStress(false), STRESS_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [stress]);

  useFrameStats(isStreaming || stress, stress ? "stress" : "live");

  // QA hook and future deep links: `--qa view=settings`.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void onUi((next) => setView(next === "settings" ? "settings" : "chat")).then((off) => {
      unlisten = off;
    });
    return () => unlisten?.();
  }, []);

  // Summoning refreshes a chat that changed while the window was hidden.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void onSummon(() => {
      if (!isStreaming) {
        void refresh();
      }
    }).then((off) => {
      unlisten = off;
    });
    return () => unlisten?.();
  }, [isStreaming, refresh]);

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

  const desktopSection = (
    <DesktopSettingsSection
      settings={desktop.settings}
      state={desktop.state}
      contextMenuCommand={desktop.contextMenuCommand}
      busy={desktop.busy}
      error={desktop.error}
      onUpdate={(patch) => void desktop.update(patch)}
      onRefreshState={() => void desktop.refreshState()}
    />
  );

  const settingsView = (
    <ModelSettings
      extra={desktopSection}
      onClose={() => setView("chat")}
      onModelChanged={() => void refreshIdentity()}
    />
  );

  // The palette is sized by Rust before it becomes visible, so nothing is
  // rendered until the persisted mode is known.
  if (!desktop.ready) {
    return <div className="h-dvh w-full" />;
  }

  if (desktop.isModern) {
    return (
      <ModernPalette
        messages={messages}
        identity={identity}
        pendingAction={pendingAction}
        isStreaming={isStreaming}
        isLoadingHistory={isLoadingHistory}
        canSend={canSend}
        isBusy={isBusy}
        backdrop={desktop.settings.backdrop}
        shortcut={desktop.settings.shortcut}
        settingsOpen={view === "settings"}
        stress={stress}
        onSend={handleSend}
        onInterrupt={handleInterrupt}
        onApprove={handleApprove}
        onDeny={handleDeny}
        onChooseDisplay={handleChooseDisplay}
        onCancelDisplay={handleCancelDisplay}
        onClear={handleClear}
        onOpenSettings={() => setView("settings")}
        onCloseSettings={() => setView("chat")}
      >
        {settingsView}
      </ModernPalette>
    );
  }

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
          onOpenSettings={() => setView("settings")}
        />

        {view === "settings" ? (
          <div className="min-h-0 flex-1">{settingsView}</div>
        ) : (
          <>
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
                    ) : message.role === "search" && message.search ? (
                      <SearchResults key={message.id} data={message.search} />
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
          </>
        )}
      </div>
    </div>
  );
}

export default App;
