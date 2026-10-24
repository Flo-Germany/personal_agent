export type ChatRole = "user" | "assistant" | "status" | "files";

export type StatusPhase = "thinking" | "tool_started" | "approval" | "generating";

export type TokenMetrics = {
  live?: boolean;
  model?: string;
  first_token_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  tokens_per_second?: number;
  prompt_tokens_per_second?: number;
  prompt_eval_ms?: number;
  eval_ms?: number;
  load_ms?: number;
  total_ms?: number;
};

export type ToolStatusState =
  | "thinking"
  | "executing"
  | "waiting"
  | "success"
  | "error"
  | "cancelled";

export type ToolStatusData = {
  state: ToolStatusState;
  label: string;
  tool?: string;
  detail?: string;
  exitCode?: number;
  metrics?: string;
  metricsDetail?: string;
};

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  status?: ToolStatusData;
  files?: string[];
  metrics?: TokenMetrics;
  streaming?: boolean;
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
  auto_approve_remaining?: boolean;
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

export type AgentAssistantStartEvent = {
  type: "assistant_start";
  conversation_id?: string;
};

export type AgentAssistantDeltaEvent = {
  type: "assistant_delta";
  conversation_id?: string;
  content: string;
};

export type AgentAssistantEndEvent = {
  type: "assistant_end";
  conversation_id?: string;
  content?: string;
};

export type AgentAssistantResetEvent = {
  type: "assistant_reset";
  conversation_id?: string;
};

export type AgentMetricsEvent = TokenMetrics & {
  type: "metrics";
  conversation_id?: string;
};

export type AgentActionRequiredEvent = {
  type: "action_required";
  conversation_id?: string;
  approval_id: string;
  actions: ActionItem[];
  message?: string;
  expires_in_seconds?: number;
  auto_approve_remaining?: boolean;
};

export type AgentFinalEvent = {
  type: "final";
  conversation_id?: string;
  reply: string;
  streamed?: boolean;
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
  | AgentAssistantStartEvent
  | AgentAssistantDeltaEvent
  | AgentAssistantEndEvent
  | AgentAssistantResetEvent
  | AgentMetricsEvent
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
  onAssistantStart: () => void;
  onAssistantDelta: (content: string) => void;
  onAssistantEnd: (content?: string) => void;
  onAssistantReset: () => void;
  onMetrics: (metrics: TokenMetrics) => void;
  onAssistant: (content: string) => void;
  onActionRequired: (action: PendingAction) => void;
  onFinal: (
    reply: string,
    conversationId?: string,
    streamed?: boolean,
  ) => void;
  onError: (message: string) => void;
};
