export type ChatRole = "user" | "assistant" | "status" | "files";

export type StatusPhase = "thinking" | "tool_started" | "approval";

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
  files?: string[];
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

export type ToolResult = Record<string, unknown>;

export type AgentStatusEvent = {
  type: "status";
  conversation_id?: string;
  phase?: StatusPhase;
  message: string;
};

export type AgentToolResultEvent = {
  type: "tool_result";
  conversation_id?: string;
  tool: string;
  ok: boolean;
  summary?: string;
  result?: ToolResult;
};

export type AgentAssistantEvent = {
  type: "assistant";
  conversation_id?: string;
  content: string;
};

export type AgentActionRequiredEvent = {
  type: "action_required";
  conversation_id?: string;
  approval_id: string;
  actions: ActionItem[];
  message?: string;
  expires_in_seconds?: number;
};

export type AgentFinalEvent = {
  type: "final";
  conversation_id?: string;
  reply: string;
};

export type AgentErrorEvent = {
  type: "error";
  conversation_id?: string;
  message: string;
  status?: number;
};

export type AgentEvent =
  | AgentStatusEvent
  | AgentToolResultEvent
  | AgentAssistantEvent
  | AgentActionRequiredEvent
  | AgentFinalEvent
  | AgentErrorEvent;

export type StreamCallbacks = {
  onStatus: (message: string, phase?: StatusPhase) => void;
  onToolResult: (
    ok: boolean,
    summary: string,
    tool?: string,
    result?: ToolResult,
  ) => void;
  onAssistant: (content: string) => void;
  onActionRequired: (action: PendingAction) => void;
  onFinal: (reply: string, conversationId?: string) => void;
  onError: (message: string) => void;
};
