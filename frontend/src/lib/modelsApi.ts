import { API_BASE_URL } from "./api";
import type {
  CatalogModel,
  EngineHardware,
  EngineStatus,
  InstallProgress,
  RuntimeProgress,
} from "../types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Keep the status text.
    }
    throw new Error(detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function fetchEngineStatus(): Promise<EngineStatus> {
  return request<EngineStatus>("/engine/status");
}

export function fetchCatalog(): Promise<{
  models: CatalogModel[];
  hardware: EngineHardware;
  engine: string;
}> {
  return request("/models/catalog");
}

export function installModel(modelId: string): Promise<InstallProgress> {
  return request<InstallProgress>("/models/install", {
    method: "POST",
    body: JSON.stringify({ model_id: modelId }),
  });
}

export function cancelInstall(jobId: string): Promise<{ cancelled: boolean }> {
  return request("/models/cancel", {
    method: "POST",
    body: JSON.stringify({ job_id: jobId }),
  });
}

export function setActiveModel(
  task: "text" | "vision",
  modelId: string,
): Promise<Record<string, unknown>> {
  return request("/models/active", {
    method: "POST",
    body: JSON.stringify({ task, model_id: modelId }),
  });
}

export function deleteModel(modelId: string): Promise<{ deleted: boolean }> {
  return request(`/models/${encodeURIComponent(modelId)}`, { method: "DELETE" });
}

export function installRuntime(): Promise<{ id: string; state: string }> {
  return request("/engine/runtime/install", { method: "POST" });
}

export function setEngineType(
  type: "ollama" | "llamacpp",
): Promise<Record<string, unknown>> {
  return request("/engine/type", {
    method: "POST",
    body: JSON.stringify({ type }),
  });
}

/** Read a Server-Sent Events response and hand each JSON payload to onEvent. */
export async function consumeProgress<T>(
  path: string,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!response.ok || !response.body) {
    throw new Error(`Progress stream failed with HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = block
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (data) {
          try {
            onEvent(JSON.parse(data) as T);
          } catch {
            // Ignore malformed frames.
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export function subscribeInstall(
  jobId: string,
  onEvent: (progress: InstallProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  return consumeProgress<InstallProgress>(
    `/models/progress/${encodeURIComponent(jobId)}`,
    onEvent,
    signal,
  );
}

export function subscribeRuntime(
  jobId: string,
  onEvent: (progress: RuntimeProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  return consumeProgress<RuntimeProgress>(
    `/engine/runtime/progress/${encodeURIComponent(jobId)}`,
    onEvent,
    signal,
  );
}
