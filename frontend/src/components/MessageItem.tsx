import { memo } from "react";

import type { ChatMessage } from "../types";
import { AgentAvatar } from "./AgentAvatar";
import { MarkdownContent } from "./MarkdownContent";
import { TokenStats } from "./TokenStats";

export const MessageItem = memo(function MessageItem({
  message,
}: {
  message: ChatMessage;
}) {
  if (message.role === "user") {
    return (
      <div className="message-in flex justify-end">
        <div className="max-w-[86%] rounded-2xl rounded-br-md border border-white/[0.07] bg-white/[0.06] px-3.5 py-2.5 text-white/90">
          <MarkdownContent
            content={message.content}
            className="markdown-compact"
          />
        </div>
      </div>
    );
  }

  return (
    <div className="message-in flex gap-2.5">
      <AgentAvatar size="sm" className="mt-0.5" />
      <div className="min-w-0 flex-1 pt-0.5">
        <MarkdownContent content={message.content} />
        {message.metrics && (
          <TokenStats
            metrics={message.metrics}
            streaming={message.streaming}
          />
        )}
      </div>
    </div>
  );
});
