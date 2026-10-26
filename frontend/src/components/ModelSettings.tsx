import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Boxes,
  CheckCircle2,
  Cpu,
  Download,
  HardDriveDownload,
  Settings2,
  Trash2,
  X,
} from "lucide-react";

import {
  cancelInstall,
  deleteModel,
  fetchCatalog,
  fetchEngineStatus,
  installModel,
  installRuntime,
  setActiveModel,
  setEngineType,
  subscribeInstall,
  subscribeRuntime,
} from "../lib/modelsApi";
import type {
  CatalogModel,
  EngineStatus,
  InstallProgress,
  RuntimeProgress,
} from "../types";

function formatBytes(value: number): string {
  if (!value) {
    return "0 MB";
  }
  if (value < 1024 ** 3) {
    return `${(value / 1024 ** 2).toFixed(value < 10 * 1024 ** 2 ? 1 : 0)} MB`;
  }
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function formatSpeed(value: number): string {
  if (!value) {
    return "";
  }
  return `${(value / 1024 ** 2).toFixed(1)} MB/s`;
}

function formatEta(seconds: number | null): string {
  if (seconds === null || seconds === undefined) {
    return "";
  }
  if (seconds < 60) {
    return `${Math.max(1, Math.round(seconds))}s left`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s left`;
}

function fitLabel(model: CatalogModel): string {
  if (model.fit === "gpu") {
    return "fits in VRAM";
  }
  if (model.fit === "partial") {
    return "partial offload";
  }
  return "CPU";
}

function ProgressBar({
  percent,
  label,
}: {
  percent: number;
  label: string;
}) {
  return (
    <div className="mt-2">
      <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className="h-full rounded-full bg-gradient-to-r from-violet-300/70 via-violet-200/60 to-emerald-300/70 transition-[width] duration-300 ease-out"
          style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
        />
      </div>
      <p className="mt-1 flex justify-between gap-2 text-[9.5px] text-white/35">
        <span className="truncate">{label}</span>
        <span className="shrink-0 tabular-nums">{percent.toFixed(1)}%</span>
      </p>
    </div>
  );
}

type ModelSettingsProps = {
  onClose: () => void;
};

export const ModelSettings = memo(function ModelSettings({
  onClose,
}: ModelSettingsProps) {
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [jobs, setJobs] = useState<Record<string, InstallProgress>>({});
  const [runtimeJob, setRuntimeJob] = useState<RuntimeProgress | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const aborters = useRef<Set<AbortController>>(new Set());

  const load = useCallback(async () => {
    try {
      const [engineStatus, catalog] = await Promise.all([
        fetchEngineStatus(),
        fetchCatalog(),
      ]);
      setStatus(engineStatus);
      setModels(catalog.models);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Could not load models");
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      for (const controller of aborters.current) {
        controller.abort();
      }
      aborters.current.clear();
    };
  }, [load]);

  const watchInstall = useCallback(
    async (model: CatalogModel, jobId: string) => {
      const controller = new AbortController();
      aborters.current.add(controller);
      try {
        await subscribeInstall(
          jobId,
          (progress) => {
            setJobs((current) => ({ ...current, [model.id]: progress }));
          },
          controller.signal,
        );
      } catch {
        // The stream ends when the job finishes; a broken frame is harmless.
      } finally {
        aborters.current.delete(controller);
        setJobs((current) => {
          const next = { ...current };
          delete next[model.id];
          return next;
        });
        await load();
      }
    },
    [load],
  );

  const startInstall = useCallback(
    async (model: CatalogModel) => {
      setBusy(model.id);
      setError(null);
      try {
        const job = await installModel(model.id);
        await watchInstall(model, job.id);
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "Install failed");
      } finally {
        setBusy(null);
      }
    },
    [watchInstall],
  );

  const startRuntimeInstall = useCallback(async () => {
    setBusy("runtime");
    setError(null);
    try {
      const job = await installRuntime();
      const controller = new AbortController();
      aborters.current.add(controller);
      await subscribeRuntime(
        job.id,
        (progress) => setRuntimeJob(progress),
        controller.signal,
      );
      aborters.current.delete(controller);
      setRuntimeJob(null);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Runtime install failed");
    } finally {
      setBusy(null);
    }
  }, [load]);

  const activate = useCallback(
    async (task: "text" | "vision", modelId: string) => {
      setBusy(`${task}:${modelId}`);
      setError(null);
      try {
        await setActiveModel(task, modelId);
        await load();
      } catch (exc) {
        setError(
          exc instanceof Error ? exc.message : "Could not switch the model",
        );
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const switchEngine = useCallback(
    async (type: "ollama" | "llamacpp") => {
      setBusy("engine");
      setError(null);
      try {
        await setEngineType(type);
        await load();
      } catch (exc) {
        setError(
          exc instanceof Error ? exc.message : "Could not switch the engine",
        );
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const remove = useCallback(
    async (model: CatalogModel) => {
      if (!window.confirm(`Delete ${model.name} from this computer?`)) {
        return;
      }
      setBusy(`delete:${model.id}`);
      try {
        await deleteModel(model.id);
        await load();
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : "Delete failed");
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const textModels = useMemo(
    () => models.filter((model) => model.task === "text"),
    [models],
  );
  const visionModels = useMemo(
    () => models.filter((model) => model.task === "vision"),
    [models],
  );
  const installedText = textModels.filter((model) => model.installed);
  const installedVision = visionModels.filter((model) => model.installed);
  const hardware = status?.hardware;

  const renderCard = (model: CatalogModel) => {
    const job = jobs[model.id];
    const installing = Boolean(job) || busy === model.id;
    const isActive =
      status?.active.text === model.id || status?.active.vision === model.id;

    return (
      <article
        key={model.id}
        className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-3"
      >
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1">
            <h3 className="flex flex-wrap items-center gap-1.5 text-[12.5px] font-medium text-white/85">
              {model.name}
              <span className="rounded-full border border-white/[0.07] bg-white/[0.03] px-1.5 py-px font-mono text-[9px] text-white/40">
                {model.parameters} · {model.quantization}
              </span>
              {isActive && (
                <span className="flex items-center gap-1 rounded-full border border-emerald-300/20 bg-emerald-300/10 px-1.5 py-px text-[9px] text-emerald-200/80">
                  <CheckCircle2 className="size-2.5" />
                  Active
                </span>
              )}
              {model.installed && !isActive && (
                <span className="rounded-full border border-white/[0.08] bg-white/[0.03] px-1.5 py-px text-[9px] text-white/45">
                  Installed
                </span>
              )}
            </h3>
            <p className="mt-1 text-[10.5px] leading-4 text-white/40">
              {model.description}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[9.5px] text-white/30">
              <span className="rounded-full border border-violet-300/15 bg-violet-300/[0.06] px-1.5 py-px text-violet-100/70">
                {model.badge}
              </span>
              <span>{formatBytes(model.total_bytes)}</span>
              <span>·</span>
              <span>{fitLabel(model)}</span>
              <span>·</span>
              <span>{model.context.toLocaleString("en-US")} ctx</span>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {model.installed ? (
              <>
                <button
                  type="button"
                  onClick={() => void remove(model)}
                  disabled={busy !== null || isActive}
                  title={
                    isActive
                      ? "Switch to another model before deleting this one"
                      : "Delete from disk"
                  }
                  className="flex size-7 items-center justify-center rounded-md text-white/30 transition-colors duration-150 ease-out hover:bg-rose-400/10 hover:text-rose-200/80 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => void startInstall(model)}
                disabled={busy !== null}
                className="flex items-center gap-1.5 rounded-lg border border-violet-300/20 bg-violet-300/10 px-2.5 py-1 text-[10.5px] text-violet-100/85 transition-colors duration-150 ease-out hover:bg-violet-300/15 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Download className="size-3" />
                Install
              </button>
            )}
          </div>
        </div>

        {job && (
          <>
            <ProgressBar
              percent={job.percent}
              label={
                job.state === "verifying"
                  ? `Verifying ${job.file || "checksum"}`
                  : `${job.message || "Downloading"} ${formatBytes(
                      job.downloaded,
                    )} / ${formatBytes(job.total)} ${formatSpeed(
                      job.speed_bps,
                    )} ${formatEta(job.eta_seconds)}`
              }
            />
            {job.state === "downloading" && (
              <button
                type="button"
                onClick={() => void cancelInstall(job.id)}
                className="mt-1 text-[9.5px] text-white/35 underline decoration-white/20 underline-offset-2 hover:text-white/60"
              >
                Cancel
              </button>
            )}
          </>
        )}
        {installing && !job && (
          <p className="mt-2 text-[9.5px] text-white/35">Starting…</p>
        )}
      </article>
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center gap-2 border-b border-white/[0.055] px-4 py-3 sm:px-5">
        <Settings2 className="size-4 text-white/45" />
        <h2 className="text-[13px] font-medium text-white/90">Model Settings</h2>
        <span className="rounded-full border border-white/[0.07] bg-white/[0.03] px-1.5 py-px font-mono text-[9px] uppercase text-white/40">
          {status?.engine ?? "…"}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close model settings"
          className="ml-auto flex size-8 items-center justify-center rounded-lg text-white/35 transition-colors duration-150 ease-out hover:bg-white/[0.06] hover:text-white/80"
        >
          <X className="size-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
        <div className="mx-auto w-full max-w-3xl space-y-4">
          {error && (
            <p className="rounded-lg border border-rose-300/20 bg-rose-400/[0.06] px-3 py-2 text-[10.5px] text-rose-100/80">
              {error}
            </p>
          )}

          {status?.engine === "ollama" && (
            <p className="rounded-lg border border-amber-300/15 bg-amber-300/[0.05] px-3 py-2 text-[10.5px] leading-4 text-amber-100/70">
              Chat currently runs through Ollama. Install the llama.cpp runtime
              and a text model below, then switch the engine for native Vulkan
              inference.
            </p>
          )}

          <section className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex size-7 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.04] text-white/45">
                <Cpu className="size-3.5" />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-[11.5px] text-white/80">
                  {hardware?.gpu_name || "No Vulkan GPU detected"}
                  {hardware?.vram_gb
                    ? ` · ${hardware.vram_gb.toFixed(1)} GB VRAM`
                    : " · CPU only"}
                </p>
                <p className="truncate text-[9.5px] text-white/30">
                  {hardware?.ram_gb.toFixed(1)} GB RAM · {hardware?.cpu}
                </p>
              </div>
              {status && !status.runtime.installed && (
                <button
                  type="button"
                  onClick={() => void startRuntimeInstall()}
                  disabled={busy !== null}
                  className="flex items-center gap-1.5 rounded-lg border border-white/[0.1] bg-white/[0.04] px-2.5 py-1 text-[10.5px] text-white/75 hover:bg-white/[0.07] disabled:opacity-40"
                >
                  <HardDriveDownload className="size-3" />
                  Install llama.cpp runtime
                </button>
              )}
              {status?.runtime.installed && (
                <span className="flex items-center gap-1 rounded-full border border-emerald-300/15 bg-emerald-300/[0.06] px-2 py-1 text-[9.5px] text-emerald-200/70">
                  <CheckCircle2 className="size-3" />
                  llama.cpp {status.runtime.version}
                </span>
              )}
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-white/[0.05] pt-2">
              <span className="text-[9px] tracking-wide text-white/30 uppercase">
                Inference engine
              </span>
              <div className="flex overflow-hidden rounded-lg border border-white/[0.08] bg-black/20">
                {(
                  [
                    ["ollama", "Ollama"],
                    ["llamacpp", "llama.cpp · Vulkan"],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={status?.engine === value}
                    disabled={busy !== null}
                    onClick={() => void switchEngine(value)}
                    className={`px-2.5 py-1 text-[10px] transition-colors duration-150 ease-out disabled:opacity-40 ${
                      status?.engine === value
                        ? "bg-violet-300/15 text-violet-100/90"
                        : "text-white/40 hover:text-white/70"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="text-[9px] text-white/25">
                {status?.engine === "llamacpp"
                  ? "Native Vulkan / CPU inference"
                  : "Ollama daemon"}
              </span>
            </div>

            {runtimeJob && (
              <ProgressBar
                percent={runtimeJob.percent}
                label={`llama.cpp runtime · ${formatBytes(
                  runtimeJob.downloaded,
                )} / ${formatBytes(runtimeJob.total)}`}
              />
            )}

            {status?.text_plan && (
              <p className="mt-2 border-t border-white/[0.05] pt-2 text-[9.5px] leading-4 text-white/30">
                Text plan: {status.text_plan.n_gpu_layers}/
                {status.text_plan.total_layers} layers on GPU ·{" "}
                {status.text_plan.weights_gb.toFixed(1)} GB weights ·{" "}
                {status.text_plan.kv_gb.toFixed(2)} GB KV ·{" "}
                {status.text_plan.reason}
              </p>
            )}
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <label className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-3">
              <span className="text-[10px] tracking-wide text-white/35 uppercase">
                Text model
              </span>
              <select
                value={status?.active.text ?? ""}
                onChange={(event) => void activate("text", event.target.value)}
                disabled={busy !== null || installedText.length === 0}
                className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-[#1b1b1d] px-2 py-1.5 text-[11px] text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-300/40 disabled:opacity-40"
              >
                {installedText.length === 0 && (
                  <option value="">No text model installed</option>
                )}
                {installedText.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name} · {model.quantization}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[9px] text-white/25">
                Kept alive for instant replies (keep_alive -1).
              </p>
            </label>

            <label className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-3">
              <span className="text-[10px] tracking-wide text-white/35 uppercase">
                Vision model
              </span>
              <select
                value={status?.active.vision ?? ""}
                onChange={(event) => void activate("vision", event.target.value)}
                disabled={busy !== null || installedVision.length === 0}
                className="mt-1.5 w-full rounded-lg border border-white/[0.08] bg-[#1b1b1d] px-2 py-1.5 text-[11px] text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-300/40 disabled:opacity-40"
              >
                {installedVision.length === 0 && (
                  <option value="">No vision model installed</option>
                )}
                {installedVision.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name} · {model.quantization}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[9px] text-white/25">
                Unloaded after each screen task (keep_alive 0).
              </p>
            </label>
          </section>

          <section className="space-y-2">
            <h3 className="flex items-center gap-1.5 text-[10px] tracking-wide text-white/35 uppercase">
              <Boxes className="size-3" />
              Text models
            </h3>
            {textModels.map(renderCard)}
          </section>

          <section className="space-y-2">
            <h3 className="flex items-center gap-1.5 text-[10px] tracking-wide text-white/35 uppercase">
              <Boxes className="size-3" />
              Vision models
            </h3>
            {visionModels.map(renderCard)}
          </section>

          <p className="pb-2 text-[9px] leading-4 text-white/25">
            Downloads come from HuggingFace and are verified with SHA256 before
            they become selectable. Models and the llama.cpp runtime live in the
            app data folder, not in the repository.
          </p>
        </div>
      </div>
    </div>
  );
});
