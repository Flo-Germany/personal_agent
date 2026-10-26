export type ChatRole =
  | "user"
  | "assistant"
  | "status"
  | "files"
  | "diagnosis"
  | "search";

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
  /** Time spent in the model's hidden reasoning phase. */
  thinking_ms?: number;
  /** Streamed thinking chunks; Ollama does not expose a separate count. */
  thinking_tokens?: number;
};

export type ToolStatusState =
  | "thinking"
  | "executing"
  | "waiting"
  | "success"
  | "error"
  | "cancelled";

export type TaskState =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "cancelled";

export type TaskStep = {
  id: string;
  label: string;
  tool?: string;
  state: TaskState;
  detail?: string;
  exitCode?: number;
  startedAt?: number;
  finishedAt?: number;
};

export type ToolStatusData = {
  state: ToolStatusState;
  label: string;
  tool?: string;
  detail?: string;
  exitCode?: number;
  metrics?: string;
  metricsDetail?: string;
  /** Live reasoning text while a thinking model is reasoning. */
  thinking?: string;
  /** When the live reasoning started, for the elapsed counter. */
  thinkingStartedAt?: number;
  tasks?: TaskStep[];
  plannedTotal?: number;
  startedAt?: number;
  finishedAt?: number;
};

export type DiagnosisFinding = {
  severity?: "critical" | "warning" | "info" | string;
  title?: string;
  detail?: string;
};

export type DiagnosisChart = {
  path: string;
  title?: string;
  description?: string;
};

export type DiagnosisReportData = {
  health_score?: number;
  findings?: DiagnosisFinding[];
  charts?: DiagnosisChart[];
  summary?: {
    os?: {
      caption?: string;
      version?: string;
      build?: string;
      last_boot?: string;
      system_drive?: string;
    };
    computer?: { manufacturer?: string; model?: string };
    cpu?: {
      name?: string;
      physical_cores?: number;
      logical_cores?: number;
      average_percent?: number;
      frequency_mhz?: number;
      per_core?: { min?: number; max?: number; average?: number };
    };
    memory?: {
      total_gb?: number;
      used_gb?: number;
      available_gb?: number;
      percent?: number;
      swap_percent?: number;
    };
    per_core_percent?: number[];
    timeline?: Array<{
      seconds?: number;
      cpu?: number;
      memory?: number;
    }>;
    gpus?: Array<{ name?: string; driver?: string; status?: string }>;
    volumes?: Array<{
      device?: string;
      total_gb?: number;
      used_gb?: number;
      free_gb?: number;
      percent?: number;
    }>;
    physical_disks?: Array<{
      name?: string;
      media?: string;
      health?: string;
      size_gb?: number;
    }>;
    smart?: Array<{
      disk?: string;
      temperature_c?: number;
      wear_percent?: number;
      power_on_hours?: number;
      read_errors?: number;
      write_errors?: number;
    }>;
    smart_available?: boolean;
    top_processes?: {
      by_memory?: Array<{
        name?: string;
        memory_gb?: number;
        cpu_percent?: number;
      }>;
      by_cpu?: Array<{
        name?: string;
        cpu_percent?: number;
        memory_gb?: number;
      }>;
    };
    services?: {
      total?: number;
      running?: number;
      stopped?: number;
      automatic_stopped_count?: number;
      automatic_stopped?: string[];
    };
    startup?: {
      count?: number;
      names?: string[];
    };
    security?: {
      defender?: {
        antivirus?: boolean;
        realtime?: boolean;
        signature_last_updated?: string;
      };
      firewall_enabled?: number;
      firewall_profiles?: number;
      firewall_state?: "enabled" | "partial" | "disabled" | "unknown" | string;
    };
    recent_events?: Array<{
      time?: string;
      id?: number;
      level?: string;
      provider?: string;
      message?: string;
    }>;
    pending_reboot?: boolean;
    uptime_hours?: number;
    benchmarks?: {
      cpu_single_thread_mbps?: number;
      cpu_multi_thread_mbps?: number;
      memory_copy_gbps?: number;
    };
    gpu_processes?: Array<{ name?: string; utilization?: number }>;
    network_rate?: { sent_mbps?: number; received_mbps?: number };
    disk_rate?: { read_mbps?: number; write_mbps?: number };
    sampling?: {
      seconds?: number;
      cpu_min?: number;
      cpu_max?: number;
      memory_max?: number;
    };
  };
  report_dir?: string;
  report_file?: string;
  message?: string;
};

export type SearchTreeNode = {
  name: string;
  path: string;
  type: "file" | "folder";
  matched: boolean;
  is_root?: boolean;
  modified?: string | null;
  size?: number | null;
  extension?: string | null;
  children_count?: number | null;
  has_children?: boolean | null;
  children?: SearchTreeNode[];
};

export type SearchResultItem = {
  path: string;
  name: string;
  type: "file" | "folder";
  extension?: string;
  size?: number | null;
  modified?: string | null;
  relative_path?: string;
  depth?: number;
  children_count?: number | null;
  has_children?: boolean | null;
};

export type SearchQuery = {
  name_contains?: string | null;
  extension?: string | null;
  kind?: "any" | "files" | "folders";
  recursive?: boolean;
  max_depth?: number | null;
};

export type SearchResultData = {
  results: SearchResultItem[];
  tree: SearchTreeNode[];
  query?: SearchQuery;
  summary?: {
    total?: number;
    files?: number;
    folders?: number;
    total_size?: number;
    newest_path?: string | null;
    newest_modified?: string | null;
    truncated?: boolean;
    limited?: boolean;
  };
  roots?: string[];
  duration_seconds?: number;
  truncated?: boolean;
  limited?: boolean;
  kind?: string;
  message?: string;
};

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  status?: ToolStatusData;
  files?: string[];
  diagnosis?: DiagnosisReportData;
  search?: SearchResultData;
  metrics?: TokenMetrics;
  streaming?: boolean;
  /** Reasoning the model emitted before the answer. */
  thinking?: string;
  thinkingMs?: number;
  thinkingTokens?: number;
  /** True while reasoning tokens are still arriving. */
  thinkingStreaming?: boolean;
  /** When the reasoning started, for the live counter. */
  thinkingStartedAt?: number;
};

export type Identity = {
  name: string;
  personality: string;
  model: string;
};

export type EngineHardware = {
  gpu_name: string;
  vram_gb: number;
  ram_gb: number;
  cpu: string;
  platform: string;
  vulkan: boolean;
  source: string;
  cpu_fallback?: boolean;
  cpu_note?: string;
};

export type EngineRuntimeStatus = {
  installed: boolean;
  version: string;
  directory: string;
  server: string;
  size_bytes: number;
};

export type ModelFileInfo = {
  role: string;
  filename: string;
  size: number;
  sha256: string;
  url: string;
};

export type CatalogModel = {
  id: string;
  name: string;
  task: "text" | "vision";
  family: string;
  parameters: string;
  quantization: string;
  badge: string;
  min_vram_gb: number;
  description: string;
  context: number;
  total_bytes: number;
  files: ModelFileInfo[];
  installed: boolean;
  active: boolean;
  fit: "gpu" | "partial" | "cpu";
  likely_fits_vram: boolean;
};

export type EngineServerStatus = {
  role: string;
  running: boolean;
  model_id?: string;
  port?: number;
  n_gpu_layers?: number;
  context?: number;
  uptime_seconds?: number;
  log?: string;
  loaded_model?: string | null;
  model_matches?: boolean;
};

export type EngineStatus = {
  engine: "ollama" | "llamacpp";
  hardware: EngineHardware;
  runtime: EngineRuntimeStatus;
  active: { text: string; vision: string };
  installed: string[];
  text_model: string | null;
  vision_model: string | null;
  text_plan: {
    n_gpu_layers: number;
    total_layers: number;
    weights_gb: number;
    kv_gb: number;
    budget_gb: number;
    vram_gb: number;
    fully_on_gpu: boolean;
    reason: string;
  } | null;
  servers: Record<string, EngineServerStatus>;
  runtime_job: {
    id: string;
    state: string;
    downloaded: number;
    total: number;
    percent: number;
    error: string;
  } | null;
};

export type InstallProgress = {
  id: string;
  model_id: string;
  state: "queued" | "downloading" | "verifying" | "done" | "error" | "cancelled";
  downloaded: number;
  total: number;
  speed_bps: number;
  eta_seconds: number | null;
  file: string;
  message: string;
  error: string;
  percent: number;
};

export type RuntimeProgress = {
  id: string;
  state: string;
  downloaded: number;
  total: number;
  percent: number;
  error: string;
};

export type HistoryMessage = {
  role: "user" | "assistant";
  content: string;
  thinking?: string | null;
  thinking_ms?: number | null;
  thinking_tokens?: number | null;
};

export type ActionItem = {
  tool: string;
  description: string;
  arguments?: Record<string, unknown>;
};

export type DisplayOption = {
  index: number;
  name?: string;
  width: number;
  height: number;
  left?: number;
  top?: number;
  primary?: boolean;
  position?: string;
  /** Optional data URL with a live preview of this display. */
  preview?: string;
};

export type PendingAction = {
  approval_id: string;
  conversation_id: string;
  actions: ActionItem[];
  message?: string;
  request?: string;
  plan?: string[];
  expires_in_seconds?: number;
  auto_approve_remaining?: boolean;
  kind?: "approval" | "display";
  displays?: DisplayOption[];
  question?: string;
};

export type ToolResult = Record<string, unknown>;

export type AgentStatusEvent = {
  type: "status";
  conversation_id?: string;
  phase?: StatusPhase;
  message: string;
  tool?: string;
  arguments?: Record<string, unknown>;
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

export type AgentAssistantThinkingEvent = {
  type: "assistant_thinking";
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

export type AgentCancelledEvent = {
  type: "cancelled";
  conversation_id?: string;
};

export type AgentActionRequiredEvent = {
  type: "action_required";
  conversation_id?: string;
  approval_id: string;
  actions: ActionItem[];
  message?: string;
  request?: string;
  plan?: string[];
  expires_in_seconds?: number;
  auto_approve_remaining?: boolean;
};

export type AgentDisplayRequiredEvent = {
  type: "display_required";
  conversation_id?: string;
  approval_id: string;
  displays: DisplayOption[];
  message?: string;
  request?: string;
  question?: string;
  expires_in_seconds?: number;
};

export type AgentFinalEvent = {
  type: "final";
  conversation_id?: string;
  reply: string;
  streamed?: boolean;
  thinking?: string;
  thinking_ms?: number;
  thinking_tokens?: number;
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
  | AgentAssistantThinkingEvent
  | AgentAssistantEndEvent
  | AgentAssistantResetEvent
  | AgentMetricsEvent
  | AgentCancelledEvent
  | AgentActionRequiredEvent
  | AgentDisplayRequiredEvent
  | AgentFinalEvent
  | AgentErrorEvent;

export type StreamCallbacks = {
  onStatus: (message: string, phase?: StatusPhase, tool?: string) => void;
  onToolResult: (
    ok: boolean,
    summary: string,
    tool?: string,
    result?: ToolResult,
  ) => void;
  onAssistantStart: () => void;
  onAssistantDelta: (content: string) => void;
  onThinking: (content: string) => void;
  onAssistantEnd: (content?: string) => void;
  onAssistantReset: () => void;
  onMetrics: (metrics: TokenMetrics) => void;
  onCancelled: () => void;
  onAssistant: (content: string) => void;
  onActionRequired: (action: PendingAction) => void;
  onFinal: (
    reply: string,
    conversationId?: string,
    streamed?: boolean,
    thinking?: { text?: string; ms?: number; tokens?: number },
  ) => void;
  onError: (message: string) => void;
};
