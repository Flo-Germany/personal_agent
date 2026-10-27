import { memo, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  FolderOpen,
  Info,
} from "lucide-react";

import type {
  DiagnosisChart,
  DiagnosisFinding,
  DiagnosisReportData,
} from "../types";
import { fileName } from "../lib/localFiles";
import { openLocalFile, revealLocalFile } from "../lib/tauriActions";

const SEVERITY: Record<
  string,
  { label: string; dot: string; text: string; chip: string }
> = {
  critical: {
    label: "Critical",
    dot: "bg-rose-400",
    text: "text-rose-200",
    chip: "border-rose-300/20 bg-rose-400/[0.07] text-rose-200/85",
  },
  warning: {
    label: "Warning",
    dot: "bg-amber-300",
    text: "text-amber-100",
    chip: "border-amber-200/20 bg-amber-300/[0.06] text-amber-100/85",
  },
  info: {
    label: "Info",
    dot: "bg-sky-300",
    text: "text-sky-100",
    chip: "border-sky-200/15 bg-sky-300/[0.05] text-sky-100/80",
  },
};

function severityOf(finding: DiagnosisFinding) {
  return SEVERITY[finding.severity ?? "info"] ?? SEVERITY.info;
}

function scoreTone(score: number) {
  if (score >= 85) {
    return {
      stroke: "#34d399",
      text: "text-emerald-300",
      dot: "bg-emerald-300",
      label: "Healthy",
    };
  }
  if (score >= 65) {
    return {
      stroke: "#fbbf24",
      text: "text-amber-300",
      dot: "bg-amber-300",
      label: "Needs attention",
    };
  }
  return {
    stroke: "#fb7185",
    text: "text-rose-300",
    dot: "bg-rose-300",
    label: "At risk",
  };
}

function formatGb(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "–";
  }
  return value >= 100 ? value.toFixed(0) : value.toFixed(1);
}

function formatUptime(hours?: number): { value: string; unit: string } {
  if (typeof hours !== "number" || !Number.isFinite(hours) || hours <= 0) {
    return { value: "–", unit: "uptime" };
  }
  if (hours >= 48) {
    return { value: (hours / 24).toFixed(1), unit: "days up" };
  }
  return { value: hours.toFixed(1), unit: "hours up" };
}

function prettyName(name?: string): string {
  if (!name) {
    return "–";
  }
  return name.replace(/^Microsoft\s+/i, "").trim();
}

const toneBar = (value: number) =>
  value >= 85
    ? "from-rose-400/80 to-rose-300/80"
    : value >= 60
      ? "from-amber-400/80 to-amber-300/80"
      : "from-emerald-400/80 to-emerald-300/70";

function MetricTile({
  label,
  value,
  unit,
  detail,
  percent,
  accent = "bg-white/[0.06]",
  bar,
}: {
  label: string;
  value: string;
  unit?: string;
  detail?: string;
  percent?: number;
  accent?: string;
  bar?: string;
}) {
  return (
    <div className="diagnosis-metric">
      <div className="flex items-center gap-1.5">
        <span className={`size-1.5 rounded-full ${accent}`} />
        <span className="diagnosis-metric-label">{label}</span>
      </div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span className="text-[17px] leading-none font-semibold tracking-tight text-white/90 tabular-nums">
          {value}
        </span>
        {unit && (
          <span className="text-[10px] font-medium text-white/58">{unit}</span>
        )}
      </div>
      {typeof percent === "number" && (
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.06]">
          <div
            className={`h-full rounded-full bg-gradient-to-r ${bar ?? toneBar(percent)}`}
            style={{ width: `${Math.max(0, Math.min(100, percent))}%` }}
          />
        </div>
      )}
      {detail && (
        <div className="mt-1.5 line-clamp-2 text-[10px] leading-snug text-white/56">
          {detail}
        </div>
      )}
    </div>
  );
}

function SectionHeading({
  children,
  hint,
}: {
  children: string;
  hint?: string;
}) {
  return (
    <div className="mb-2.5 flex items-baseline justify-between gap-2">
      <h4 className="m-0 text-[10px] font-semibold tracking-[0.12em] text-white/58 uppercase">
        {children}
      </h4>
      {hint && <span className="text-[10px] text-white/54">{hint}</span>}
    </div>
  );
}

function CoreChart({ values }: { values: number[] }) {
  const average =
    values.length > 0
      ? values.reduce((sum, value) => sum + value, 0) / values.length
      : 0;
  return (
    <div className="diagnosis-panel">
      <SectionHeading hint={`avg ${average.toFixed(0)}%`}>
        CPU per core
      </SectionHeading>
      <div className="relative flex h-24 items-end gap-[3px]">
        <div
          className="pointer-events-none absolute inset-x-0 border-t border-dashed border-sky-300/30"
          style={{ bottom: `${Math.min(100, average)}%` }}
        />
        {values.map((value, index) => (
          <div
            key={index}
            title={`Core ${index + 1}: ${value}%`}
            className="group relative flex-1 rounded-t-[3px] bg-gradient-to-t from-white/10 to-white/[0.16] transition-colors"
            style={{ height: `${Math.max(3, Math.min(100, value))}%` }}
          >
            <span
              className={`absolute inset-0 rounded-t-[3px] bg-gradient-to-t ${
                value >= 85
                  ? "from-rose-500/70 to-rose-300/80"
                  : value >= 60
                    ? "from-amber-500/60 to-amber-300/75"
                    : "from-emerald-500/55 to-emerald-300/70"
              }`}
            />
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex justify-between text-[9px] tracking-wide text-white/48">
        <span>core 1</span>
        <span>{values.length} logical cores</span>
      </div>
    </div>
  );
}

function TimelineChart({
  series,
}: {
  series: Array<{ seconds?: number; cpu?: number; memory?: number }>;
}) {
  const width = 100;
  const height = 34;
  const point = (value: number) =>
    height - (Math.max(0, Math.min(100, value)) / 100) * height;
  const line = (key: "cpu" | "memory") =>
    series
      .map((item, index) => {
        const x = series.length > 1 ? (index / (series.length - 1)) * width : 0;
        return `${x.toFixed(2)},${point(item[key] ?? 0).toFixed(2)}`;
      })
      .join(" ");
  const cpuValues = series.map((item) => item.cpu ?? 0);
  const memoryValues = series.map((item) => item.memory ?? 0);
  const duration =
    series.length > 1
      ? Math.round((series[series.length - 1].seconds ?? 0) - (series[0].seconds ?? 0))
      : 0;

  return (
    <div className="diagnosis-panel">
      <SectionHeading hint={duration ? `${duration}s window` : undefined}>
        Usage timeline
      </SectionHeading>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-24 w-full overflow-visible"
        role="img"
        aria-label="CPU and memory utilization over the sampling window"
      >
        {[0.25, 0.5, 0.75].map((fraction) => (
          <line
            key={fraction}
            x1="0"
            x2={width}
            y1={height * fraction}
            y2={height * fraction}
            stroke="rgba(255,255,255,0.05)"
            strokeWidth="0.4"
          />
        ))}
        <polyline
          points={`0,${height} ${line("cpu")} ${width},${height}`}
          fill="url(#diagnosis-cpu-fill)"
          stroke="none"
        />
        <polyline
          points={line("memory")}
          fill="none"
          stroke="#60a5fa"
          strokeWidth="1.1"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.85"
        />
        <polyline
          points={line("cpu")}
          fill="none"
          stroke="#34d399"
          strokeWidth="1.2"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <defs>
          <linearGradient id="diagnosis-cpu-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity="0.18" />
            <stop offset="100%" stopColor="#34d399" stopOpacity="0" />
          </linearGradient>
        </defs>
      </svg>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9.5px] text-white/56">
        <span className="flex items-center gap-1">
          <span className="h-0.5 w-2.5 rounded-full bg-emerald-400" />
          CPU peak {Math.max(...cpuValues, 0).toFixed(0)}%
        </span>
        <span className="flex items-center gap-1">
          <span className="h-0.5 w-2.5 rounded-full bg-sky-400" />
          RAM peak {Math.max(...memoryValues, 0).toFixed(0)}%
        </span>
      </div>
    </div>
  );
}

function StorageSection({
  volumes,
  disks,
  smart,
}: {
  volumes: NonNullable<DiagnosisReportData["summary"]>["volumes"];
  disks: NonNullable<DiagnosisReportData["summary"]>["physical_disks"];
  smart: NonNullable<DiagnosisReportData["summary"]>["smart"];
}) {
  return (
    <div className="diagnosis-panel">
      <SectionHeading hint={volumes?.length ? `${volumes.length} volume(s)` : undefined}>
        Storage
      </SectionHeading>
      <div className="space-y-2.5">
        {(volumes ?? []).map((volume, index) => {
          const percent = Math.max(0, Math.min(100, volume.percent ?? 0));
          return (
            <div key={`${volume.device ?? index}`}>
              <div className="mb-1 flex items-baseline justify-between gap-2 text-[11px]">
                <span className="flex items-center gap-1.5 font-medium text-white/80">
                  <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[9px] font-semibold tracking-wide text-white/60">
                    {volume.device?.replace(/\\$/, "") ?? "?"}
                  </span>
                  {formatGb(volume.free_gb)} GB free
                </span>
                <span className="text-white/56 tabular-nums">
                  {formatGb(volume.used_gb)} / {formatGb(volume.total_gb)} GB
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.05]">
                <div
                  className={`h-full rounded-full bg-gradient-to-r ${
                    percent >= 88
                      ? "from-rose-500/80 to-rose-300/80"
                      : percent >= 75
                        ? "from-amber-500/80 to-amber-300/70"
                        : "from-violet-400/80 to-emerald-300/70"
                  }`}
                  style={{ width: `${percent}%` }}
                />
              </div>
            </div>
          );
        })}
        {(!volumes || volumes.length === 0) && (
          <p className="text-[11px] text-white/56">No volume data.</p>
        )}
      </div>
      {((disks ?? []).length > 0 || (smart ?? []).length > 0) && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(disks ?? []).map((disk, index) => {
            const healthy = (disk.health ?? "").toLowerCase() === "healthy";
            return (
              <span
                key={`${disk.name ?? "disk"}-${index}`}
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9.5px] ${
                  healthy
                    ? "border-emerald-300/15 bg-emerald-300/[0.05] text-emerald-200/75"
                    : "border-amber-300/20 bg-amber-300/[0.06] text-amber-100/80"
                }`}
              >
                {healthy ? (
                  <Check className="size-2.5" />
                ) : (
                  <AlertTriangle className="size-2.5" />
                )}
                {disk.name?.slice(0, 32) ?? "Disk"} ·{" "}
                {healthy ? "healthy" : (disk.health ?? "unknown")}
              </span>
            );
          })}
          {(smart ?? []).map((entry, index) => {
            const hot =
              typeof entry.temperature_c === "number" &&
              entry.temperature_c >= 70;
            const worn =
              typeof entry.wear_percent === "number" && entry.wear_percent >= 80;
            const details = [
              typeof entry.temperature_c === "number"
                ? `${Math.round(entry.temperature_c)}°C`
                : null,
              typeof entry.wear_percent === "number"
                ? `${Math.round(entry.wear_percent)}% wear`
                : null,
              typeof entry.power_on_hours === "number"
                ? `${Math.round(entry.power_on_hours / 24)} d powered`
                : null,
            ].filter(Boolean);
            if (details.length === 0) {
              return null;
            }
            return (
              <span
                key={`${entry.disk ?? index}-smart`}
                title={entry.disk ?? "SMART counters"}
                className={`diagnosis-chip ${
                  hot || worn ? SEVERITY.warning.chip : SEVERITY.info.chip
                }`}
              >
                SMART {details.join(" · ")}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProcessList({
  title,
  rows,
  metric,
  format,
}: {
  title: string;
  rows: Array<{ name?: string; cpu_percent?: number; memory_gb?: number }>;
  metric: "memory_gb" | "cpu_percent";
  format: (value: number) => string;
}) {
  const max = Math.max(
    1,
    ...rows.map((row) => Number(row[metric] ?? 0)),
  );
  return (
    <div className="diagnosis-panel">
      <SectionHeading>{title}</SectionHeading>
      <div className="space-y-1.5">
        {rows.length === 0 && (
          <p className="text-[11px] text-white/56">No process data.</p>
        )}
        {rows.map((row, index) => {
          const value = Number(row[metric] ?? 0);
          return (
            <div
              key={`${row.name ?? "process"}-${index}`}
              className="flex items-center gap-2"
            >
              <span className="w-[38%] truncate text-[10.5px] text-white/70">
                {row.name ?? "unknown"}
              </span>
              <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/[0.05]">
                <div
                  className={`h-full rounded-full ${
                    metric === "memory_gb"
                      ? "bg-gradient-to-r from-indigo-400/70 to-violet-300/70"
                      : "bg-gradient-to-r from-amber-500/70 to-orange-300/70"
                  }`}
                  style={{ width: `${Math.max(3, (value / max) * 100)}%` }}
                />
              </div>
              <span className="w-14 text-right text-[10px] text-white/45 tabular-nums">
                {format(value)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Benchmarks({
  benchmarks,
  diskRate,
  networkRate,
}: {
  benchmarks: NonNullable<DiagnosisReportData["summary"]>["benchmarks"];
  diskRate?: { read_mbps?: number; write_mbps?: number };
  networkRate?: { sent_mbps?: number; received_mbps?: number };
}) {
  const entries = [
    {
      label: "CPU · 1 thread",
      value: benchmarks?.cpu_single_thread_mbps,
      unit: "MiB/s",
    },
    {
      label: "CPU · 4 threads",
      value: benchmarks?.cpu_multi_thread_mbps,
      unit: "MiB/s",
    },
    {
      label: "Memory copy",
      value: benchmarks?.memory_copy_gbps,
      unit: "GiB/s",
    },
    {
      label: "Disk read",
      value: diskRate?.read_mbps,
      unit: "MB/s",
    },
    {
      label: "Disk write",
      value: diskRate?.write_mbps,
      unit: "MB/s",
    },
    {
      label: "Network down",
      value: networkRate?.received_mbps,
      unit: "Mb/s",
    },
  ].filter(
    (entry) => typeof entry.value === "number" && Number.isFinite(entry.value),
  );

  if (entries.length === 0) {
    return null;
  }

  const max = Math.max(...entries.map((entry) => Number(entry.value)));

  return (
    <div className="diagnosis-panel">
      <SectionHeading hint="short tests">Performance</SectionHeading>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
        {entries.map((entry) => (
          <div key={entry.label}>
            <div className="text-[9.5px] text-white/56">{entry.label}</div>
            <div className="flex items-baseline gap-1">
              <span className="text-[12.5px] font-semibold text-white/85 tabular-nums">
                {Number(entry.value).toFixed(1)}
              </span>
              <span className="text-[9px] text-white/56">{entry.unit}</span>
            </div>
            <div className="mt-1 h-0.5 overflow-hidden rounded-full bg-white/[0.05]">
              <div
                className="h-full rounded-full bg-gradient-to-r from-sky-400/60 to-violet-300/70"
                style={{
                  width: `${Math.max(6, (Number(entry.value) / max) * 100)}%`,
                }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export const DiagnosisReport = memo(function DiagnosisReport({
  data,
}: {
  data: DiagnosisReportData;
}) {
  const [filesOpen, setFilesOpen] = useState(false);
  const summary = data.summary ?? {};
  const findings = data.findings ?? [];
  const charts = data.charts ?? [];
  const score = Math.max(0, Math.min(100, Math.round(data.health_score ?? 0)));
  const tone = scoreTone(score);
  const critical = findings.filter(
    (finding) => finding.severity === "critical",
  ).length;
  const warnings = findings.filter(
    (finding) => finding.severity === "warning",
  ).length;
  const info = findings.length - critical - warnings;
  const cpu = summary.cpu ?? {};
  const memory = summary.memory ?? {};
  const gpus = summary.gpus ?? [];
  const security = summary.security ?? {};
  const uptime = formatUptime(summary.uptime_hours);
  const computer = summary.computer ?? {};
  const os = summary.os ?? {};
  const processor = prettyName(cpu.name);
  const coreCount = cpu.logical_cores ? `${cpu.logical_cores} threads` : "";
  const gpuName = gpus[0]?.name ?? "No GPU reported";
  const gpuShort = (
    gpuName.match(/\b(?:RTX|GTX|RX|Arc)\s+[^,]+/)?.[0] ??
    gpuName
      .replace(/\((?:TM|R)\)/gi, "")
      .replace(/\s+/g, " ")
      .trim()
  ).slice(0, 22);
  const busyGpu = (summary.gpu_processes ?? []).find(
    (process) =>
      !["llama-server.exe", "ollama.exe"].includes(
        (process.name ?? "").toLowerCase(),
      ),
  );
  const busyGpuPercent = Math.min(
    100,
    Math.round(busyGpu?.utilization ?? 0),
  );
  const gpuDetail =
    busyGpuPercent >= 25 && busyGpu?.name
      ? `${busyGpu.name} · ${busyGpuPercent}% GPU`
      : (gpus[0]?.status ?? gpus[0]?.driver ?? "GPU");
  const ringLength = 2 * Math.PI * 42;
  const events = summary.recent_events ?? [];

  const findingsSorted = useMemo(() => {
    const rank: Record<string, number> = {
      critical: 0,
      warning: 1,
      info: 2,
    };
    return [...findings].sort(
      (left, right) =>
        (rank[left.severity ?? "info"] ?? 3) -
        (rank[right.severity ?? "info"] ?? 3),
    );
  }, [findings]);

  const memoryPercent = Math.max(
    0,
    Math.min(100, memory.percent ?? 0),
  );

  const firewallProfiles = security.firewall_profiles ?? 0;
  const firewallEnabled = security.firewall_enabled ?? 0;
  const firewallState =
    security.firewall_state ??
    (firewallProfiles === 0
      ? "unknown"
      : firewallEnabled === 0
        ? "disabled"
        : firewallEnabled < firewallProfiles
          ? "partial"
          : "enabled");
  const firewallLabel =
    firewallState === "unknown"
      ? "Firewall state unknown"
      : firewallState === "disabled"
        ? `Firewall disabled (0/${firewallProfiles})`
        : firewallState === "partial"
          ? `Firewall partial (${firewallEnabled}/${firewallProfiles})`
          : `Firewall on (${firewallProfiles}/${firewallProfiles})`;
  const firewallChip =
    firewallState === "enabled"
      ? SEVERITY.info.chip
      : firewallState === "disabled"
        ? SEVERITY.critical.chip
        : SEVERITY.warning.chip;

  return (
    <article className="diagnosis-report message-in">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <div className="relative flex size-[86px] shrink-0 items-center justify-center">
          <svg
            viewBox="0 0 100 100"
            className="absolute inset-0 size-full -rotate-90"
            aria-hidden="true"
          >
            <circle
              cx="50"
              cy="50"
              r="42"
              fill="none"
              stroke="rgba(255,255,255,0.07)"
              strokeWidth="7"
            />
            <circle
              cx="50"
              cy="50"
              r="42"
              fill="none"
              stroke={tone.stroke}
              strokeWidth="7"
              strokeLinecap="round"
              strokeDasharray={`${(score / 100) * ringLength} ${ringLength}`}
              className="[transition:stroke-dasharray_600ms_ease-out]"
            />
          </svg>
          <div className="relative flex flex-col items-center">
            <span
              className={`text-[22px] leading-none font-semibold tabular-nums ${tone.text}`}
            >
              {score}
            </span>
            <span className="mt-0.5 text-[9px] tracking-wide text-white/56">
              / 100
            </span>
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-[0.12em] text-white/56 uppercase">
            <span className={`size-1.5 rounded-full ${tone.dot}`} />
            System diagnosis · {tone.label}
          </div>
          <h3 className="mt-1 text-[15px] leading-snug font-semibold text-balance text-white/90">
            {prettyName(os.caption) || "Windows system"}
            {computer.model ? (
              <span className="font-normal text-white/58">
                {" "}
                · {computer.manufacturer} {computer.model}
              </span>
            ) : null}
          </h3>
          <p className="mt-0.5 text-[11px] leading-snug text-white/45">
            {processor}
            {coreCount ? ` · ${coreCount}` : ""}
            {memory.total_gb ? ` · ${formatGb(memory.total_gb)} GB RAM` : ""}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {critical > 0 && (
              <span className={`diagnosis-chip ${SEVERITY.critical.chip}`}>
                <span className={`size-1 rounded-full ${SEVERITY.critical.dot}`} />
                {critical} critical
              </span>
            )}
            {warnings > 0 && (
              <span className={`diagnosis-chip ${SEVERITY.warning.chip}`}>
                <span className={`size-1 rounded-full ${SEVERITY.warning.dot}`} />
                {warnings} warning{warnings === 1 ? "" : "s"}
              </span>
            )}
            {info > 0 && (
              <span className={`diagnosis-chip ${SEVERITY.info.chip}`}>
                <span className={`size-1 rounded-full ${SEVERITY.info.dot}`} />
                {info} info
              </span>
            )}
            {findings.length === 0 && (
              <span className={`diagnosis-chip ${SEVERITY.info.chip}`}>
                <Check className="size-2.5" /> No findings
              </span>
            )}
            {summary.pending_reboot && (
              <span className={`diagnosis-chip ${SEVERITY.warning.chip}`}>
                <AlertTriangle className="size-2.5" /> Reboot pending
              </span>
            )}
          </div>
        </div>
      </header>

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <MetricTile
          label="CPU load"
          value={String(Math.round(cpu.average_percent ?? 0))}
          unit="%"
          percent={cpu.average_percent ?? 0}
          accent="bg-emerald-400/80"
          detail={
            cpu.per_core
              ? `${Math.round(cpu.per_core.min ?? 0)}–${Math.round(cpu.per_core.max ?? 0)}% per core`
              : undefined
          }
        />
        <MetricTile
          label="Memory"
          value={String(Math.round(memoryPercent))}
          unit="%"
          percent={memoryPercent}
          accent="bg-sky-400/80"
          detail={
            memory.total_gb
              ? `${formatGb(memory.used_gb)} of ${formatGb(memory.total_gb)} GB`
              : undefined
          }
        />
        <MetricTile
          label="Graphics"
          value={gpuShort}
          detail={gpuDetail}
          percent={busyGpuPercent >= 25 ? busyGpuPercent : undefined}
          bar={
            busyGpuPercent >= 60
              ? "from-amber-400/80 to-rose-400/70"
              : "from-violet-400/70 to-sky-300/70"
          }
          accent={
            busyGpuPercent >= 60 ? "bg-amber-300/80" : "bg-violet-400/80"
          }
        />
        <MetricTile
          label="Uptime"
          value={uptime.value}
          unit={uptime.unit}
          accent="bg-amber-300/80"
          detail={os.last_boot ? `boot ${os.last_boot}` : undefined}
        />
      </div>

      {findingsSorted.length > 0 && (
        <section className="mt-4">
          <SectionHeading hint={`${findingsSorted.length} total`}>
            Findings
          </SectionHeading>
          <ul className="space-y-1.5 p-0">
            {findingsSorted.map((finding, index) => {
              const severity = severityOf(finding);
              return (
                <li
                  key={`${finding.title ?? "finding"}-${index}`}
                  className="diagnosis-finding"
                >
                  <span
                    className={`mt-[5px] size-1.5 shrink-0 rounded-full ${severity.dot}`}
                  />
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className={`text-[11.5px] font-semibold ${severity.text}`}>
                        {finding.title ?? severity.label}
                      </span>
                      <span className="text-[9px] tracking-wide text-white/48 uppercase">
                        {severity.label}
                      </span>
                    </div>
                    {finding.detail && (
                      <p className="mt-0.5 text-[11px] leading-relaxed text-white/50">
                        {finding.detail}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {(summary.per_core_percent ?? []).length > 1 && (
          <CoreChart values={summary.per_core_percent ?? []} />
        )}
        {(summary.timeline ?? []).length > 1 && (
          <TimelineChart series={summary.timeline ?? []} />
        )}
        <StorageSection
          volumes={summary.volumes}
          disks={summary.physical_disks}
          smart={summary.smart}
        />
        <Benchmarks
          benchmarks={summary.benchmarks}
          diskRate={summary.disk_rate}
          networkRate={summary.network_rate}
        />
        <ProcessList
          title="Top memory"
          rows={summary.top_processes?.by_memory ?? []}
          metric="memory_gb"
          format={(value) => `${value.toFixed(2)} GB`}
        />
        <ProcessList
          title="Top CPU"
          rows={summary.top_processes?.by_cpu ?? []}
          metric="cpu_percent"
          format={(value) => `${value.toFixed(1)}%`}
        />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <span
          className={`diagnosis-chip ${
            security.defender?.antivirus
              ? SEVERITY.info.chip
              : SEVERITY.warning.chip
          }`}
        >
          Defender {security.defender?.antivirus ? "on" : "off"}
          {security.defender?.realtime ? " · realtime" : ""}
        </span>
        <span className={`diagnosis-chip ${firewallChip}`}>
          {firewallState === "enabled" ? (
            <Check className="size-2.5" />
          ) : (
            <AlertTriangle className="size-2.5" />
          )}
          {firewallLabel}
        </span>
        {summary.services?.automatic_stopped_count ? (
          <span className={`diagnosis-chip ${SEVERITY.warning.chip}`}>
            {summary.services.automatic_stopped_count} auto services stopped
          </span>
        ) : null}
        {summary.startup?.count ? (
          <span className={`diagnosis-chip ${SEVERITY.info.chip}`}>
            {summary.startup.count} startup item
            {summary.startup.count === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>

      {events.length > 0 && (
        <section className="mt-4">
          <SectionHeading hint="latest">Recent system errors</SectionHeading>
          <ul className="space-y-1 p-0">
            {events.slice(0, 3).map((event, index) => (
              <li
                key={`${event.time ?? "event"}-${event.id ?? index}-${index}`}
                className="flex gap-2 text-[10.5px] text-white/45"
              >
                <Info className="mt-0.5 size-3 shrink-0 text-white/48" />
                <span className="min-w-0">
                  <span className="text-white/60">
                    {event.provider ?? "System"} · {event.level ?? "Error"}
                  </span>
                  {event.message ? (
                    <span className="block truncate text-white/56">
                      {event.message}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="mt-4 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-2.5">
        {data.report_file && (
          <>
            <button
              type="button"
              onClick={() => void openLocalFile(data.report_file as string)}
              className="diagnosis-link"
            >
              <ExternalLink className="size-3" />
              Full report
            </button>
            <button
              type="button"
              onClick={() => void revealLocalFile(data.report_file as string)}
              className="diagnosis-link"
            >
              <FolderOpen className="size-3" />
              Show folder
            </button>
          </>
        )}
        {charts.length > 0 && (
          <button
            type="button"
            onClick={() => setFilesOpen((current) => !current)}
            className="diagnosis-link ml-auto"
            aria-expanded={filesOpen}
          >
            <ChevronDown
              className={`size-3 transition-transform duration-200 ${
                filesOpen ? "rotate-180" : ""
              }`}
            />
            {charts.length} exported chart{charts.length === 1 ? "" : "s"}
          </button>
        )}
      </footer>

      {filesOpen && charts.length > 0 && (
        <ul className="mt-2 grid gap-1 p-0 sm:grid-cols-2">
          {charts.map((chart: DiagnosisChart) => (
            <li key={chart.path} className="min-w-0">
              <button
                type="button"
                onClick={() => void openLocalFile(chart.path)}
                className="flex w-full items-center gap-2 rounded-lg border border-white/[0.05] bg-white/[0.02] px-2 py-1.5 text-left transition-colors hover:border-white/10 hover:bg-white/[0.04]"
                title={chart.description ?? chart.title ?? fileName(chart.path)}
              >
                <span className="size-1.5 shrink-0 rounded-full bg-violet-300/60" />
                <span className="min-w-0 flex-1 truncate text-[10.5px] text-white/55">
                  {chart.title ?? fileName(chart.path)}
                </span>
                <ExternalLink className="size-3 shrink-0 text-white/48" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
});
