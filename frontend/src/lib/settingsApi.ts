import { API_BASE_URL, describeHttpError } from "./api";

export type EditableSettings = {
  agent_name: string;
  language: "auto" | "de" | "en" | "zh";
  communication_style: "concise" | "conversational" | "technical";
  autonomy: "cautious" | "balanced" | "autonomous";
  context_window: number;
  keep_alive_seconds: number;
  cpu_threads: number | null;
  process_priority: "eco" | "normal" | "high";
  gpu_layers: number | null;
  vram_override_gb: number | null;
  kv_cache: "f16" | "q8_0" | "q4_0";
  vision_max_width: number;
  uia_depth: number;
  allow_foreground_fallback: boolean;
  config_path: string;
  engine_type: string;
  runtime_model: string;
};

export type EditablePatch = Partial<
  Omit<
    EditableSettings,
    "config_path" | "engine_type" | "runtime_model"
  >
>;

type EditableResponse = {
  ok: boolean;
  settings: EditableSettings;
  restart_required?: boolean;
  message?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Non-JSON errors still get a readable status message.
    }
    throw new Error(
      describeHttpError(response.status, response.statusText, body),
    );
  }
  return (await response.json()) as T;
}

export async function fetchEditableSettings(): Promise<EditableSettings> {
  const data = await request<EditableResponse>("/settings/editable");
  return data.settings;
}

export async function saveEditableSettings(
  patch: EditablePatch,
): Promise<{ settings: EditableSettings; restartRequired: boolean }> {
  const data = await request<EditableResponse>("/settings/editable", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  return {
    settings: data.settings,
    restartRequired: Boolean(data.restart_required),
  };
}

export async function restartEngine(): Promise<void> {
  await request<{ engine: string }>("/engine/restart", { method: "POST" });
}

export type Knowledge = {
  content: string;
  path: string;
  chars: number;
};

export async function fetchKnowledge(): Promise<Knowledge> {
  return request<Knowledge>("/knowledge");
}

export async function saveKnowledge(content: string): Promise<Knowledge> {
  const data = await request<{ ok: boolean; chars: number; path: string }>(
    "/knowledge",
    { method: "PUT", body: JSON.stringify({ content }) },
  );
  return { content, path: data.path, chars: data.chars };
}

export async function clearKnowledge(): Promise<Knowledge> {
  return request<Knowledge>("/knowledge", { method: "DELETE" });
}
