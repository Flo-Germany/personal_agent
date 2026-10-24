export type ChatRole = "user" | "assistant" | "status";

export type StatusKind =
  | "thinking"
  | "tool"
  | "result-ok"
  | "result-error"
  | "approval";

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  statusKind?: StatusKind;
};

export type Identity = {
  name: string;
  personality: string;
  model: string;
};

export type HistoryMessage = {
  role: "user" | "assistant";
  content: string;
};

export type ActionItem = {
  tool: string;
  description: string;
  arguments?: Record<string, unknown>;
};

export type PendingAction = {
  approval_id: string;
  conversation_id: string;
  actions: ActionItem[];
  message?: string;
  expires_in_seconds?: number;
};

export type AgentEvent = {
  type:
    | "status"
    | "tool_result"
    | "action_required"
    | "final"
    | "error"
    | "assistant";
  conversation_id?: string;
  phase?: string;
  message?: string;
  summary?: string;
  ok?: boolean;
  reply?: string;
  content?: string;
  approval_id?: string;
  actions?: ActionItem[];
  status?: number;
  expires_in_seconds?: number;
};

export type StreamCallbacks = {
  onStatus: (message: string, phase?: string) => void;
  onToolResult: (ok: boolean, summary: string) => void;
  onAssistant: (content: string) => void;
  onActionRequired: (action: PendingAction) => void;
  onFinal: (reply: string, conversationId?: string) => void;
  onError: (message: string) => void;
};
