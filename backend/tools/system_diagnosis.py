"""Read-only Windows system diagnosis with matplotlib charts.

The tool collects hardware/OS facts, live resource usage, top processes,
storage health, security/update status, recent system errors, and small
benchmarks. It writes PNG charts under ``memory/diagnostics`` so the chat can
display them. No system settings are changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_SECONDS = 4.0
_MIN_SAMPLE_SECONDS = 2.0
_MAX_SAMPLE_SECONDS = 10.0
_CREATE_NO_WINDOW = 0x0800_0000
_JSON_MARKER = "@@DIAGNOSIS_JSON@@"
_JSON_DATE = re.compile(r"/Date\((-?\d+)\)/")

_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$out = [ordered]@{}

$os = Get-CimInstance Win32_OperatingSystem
if ($os) {
  $out.os = [ordered]@{
    caption = $os.Caption
    version = $os.Version
    build = $os.BuildNumber
    last_boot = $os.LastBootUpTime
    install_date = $os.InstallDate
    total_memory_kb = $os.TotalVisibleMemorySize
    free_memory_kb = $os.FreePhysicalMemory
    system_drive = $os.SystemDrive
  }
}

$cs = Get-CimInstance Win32_ComputerSystem
if ($cs) {
  $out.computer = [ordered]@{
    manufacturer = $cs.Manufacturer
    model = $cs.Model
    domain = $cs.Domain
    total_memory = $cs.TotalPhysicalMemory
  }
}

$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu) {
  $out.cpu = [ordered]@{
    name = $cpu.Name
    cores = $cpu.NumberOfCores
    logical = $cpu.NumberOfLogicalProcessors
    max_clock_mhz = $cpu.MaxClockSpeed
    load_percent = $cpu.LoadPercentage
    socket = $cpu.SocketDesignation
  }
}

$out.memory_modules = @(
  Get-CimInstance Win32_PhysicalMemory |
    Select-Object BankLabel,DeviceLocator,Capacity,Speed,Manufacturer,PartNumber
)
$out.gpus = @(
  Get-CimInstance Win32_VideoController |
    Select-Object Name,DriverVersion,VideoProcessor,Status
)
$out.physical_disks = @(
  Get-PhysicalDisk |
    Select-Object FriendlyName,MediaType,HealthStatus,OperationalStatus,Size,BusType
)
$out.volumes = @(
  Get-Volume |
    Where-Object { $_.DriveLetter } |
    Select-Object DriveLetter,FileSystemLabel,FileSystem,HealthStatus,Size,SizeRemaining
)
$out.reliability = @(
  Get-PhysicalDisk | ForEach-Object {
    $disk = $_
    $counter = Get-StorageReliabilityCounter -PhysicalDisk $disk
    if ($counter) {
      [pscustomobject]@{
        disk = $disk.FriendlyName
        temperature = $counter.Temperature
        wear = $counter.Wear
        power_on_hours = $counter.PowerOnHours
        read_errors = $counter.ReadErrorsTotal
        write_errors = $counter.WriteErrorsTotal
      }
    }
  }
)

$out.startup = @(
  Get-CimInstance Win32_StartupCommand |
    Select-Object Name,Command,Location,User
)

$services = Get-Service
$out.services = [ordered]@{
  total = @($services).Count
  running = @($services | Where-Object { $_.Status -eq 'Running' }).Count
  stopped = @($services | Where-Object { $_.Status -ne 'Running' }).Count
  automatic_stopped = @(
    $services |
      Where-Object { $_.StartType -eq 'Automatic' -and $_.Status -ne 'Running' } |
      Select-Object Name,DisplayName,Status
  )
}

$defender = Get-MpComputerStatus
if ($defender) {
  $out.defender = [ordered]@{
    am_service_enabled = $defender.AMServiceEnabled
    antivirus_enabled = $defender.AntivirusEnabled
    realtime_enabled = $defender.RealTimeProtectionEnabled
    signature_last_updated = $defender.AntivirusSignatureLastUpdated
    quick_scan_end = $defender.QuickScanEndTime
  }
}

$out.firewall = @(Get-NetFirewallProfile | Select-Object Name,Enabled)
$out.netadapters = @(
  Get-NetAdapter |
    Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress
)
$out.hotfixes = @(
  Get-HotFix |
    Sort-Object InstalledOn -Descending |
    Select-Object -First 10 HotFixID,Description,InstalledOn
)

$pendingReboot = $false
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
  $pendingReboot = $true
}
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
  $pendingReboot = $true
}
$pendingRenames = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations).PendingFileRenameOperations
if ($pendingRenames) { $pendingReboot = $true }
$out.pending_reboot = $pendingReboot

$out.events = @(
  Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1,2; StartTime = (Get-Date).AddDays(-7) } -MaxEvents 20 |
    Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,Message
)

$gpuUtil = Get-Counter '\GPU Engine(*)\Utilization Percentage'
$out.gpu_utilization = @(
  $gpuUtil.CounterSamples |
    Where-Object { $_.CookedValue -gt 0 } |
    Sort-Object CookedValue -Descending |
    Select-Object -First 12 InstanceName,CookedValue
)
$gpuMem = Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage'
$out.gpu_memory = @(
  $gpuMem.CounterSamples |
    Where-Object { $_.CookedValue -gt 0 } |
    Sort-Object CookedValue -Descending |
    Select-Object -First 8 InstanceName,CookedValue
)

Write-Output '@@DIAGNOSIS_JSON@@'
$out | ConvertTo-Json -Depth 8 -Compress
"""


def _clamp_sample_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _DEFAULT_SAMPLE_SECONDS
    return max(_MIN_SAMPLE_SECONDS, min(seconds, _MAX_SAMPLE_SECONDS))


def _run_powershell_json(script: str, timeout: float = 45.0) -> dict[str, Any]:
    """Run a fixed, read-only PowerShell script and parse its JSON output."""
    if os.name != "nt":
        return {}

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        logger.info("PowerShell is not available; skipping Windows sections")
        return {}
    except subprocess.TimeoutExpired:
        logger.warning("System diagnosis PowerShell query timed out")
        return {}
    except OSError:
        logger.warning("Could not run the system diagnosis PowerShell query")
        return {}

    output = completed.stdout or ""
    if _JSON_MARKER in output:
        output = output.split(_JSON_MARKER, 1)[1]
    output = output.strip()
    if not output:
        return {}

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        logger.debug("Could not parse diagnosis JSON: %s", output[:300])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalise_dates(value: Any) -> Any:
    """Convert PowerShell 5.1 ``/Date(ms)/`` values into ISO timestamps."""
    if isinstance(value, str):
        match = _JSON_DATE.fullmatch(value.strip())
        if match is None:
            return value
        try:
            timestamp = int(match.group(1)) / 1000
            return datetime.fromtimestamp(timestamp).astimezone().isoformat(
                timespec="seconds"
            )
        except (OverflowError, OSError, ValueError):
            return value
    if isinstance(value, list):
        return [_normalise_dates(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise_dates(item) for key, item in value.items()}
    return value


def _strip_strings(value: Any) -> Any:
    """Trim accidental whitespace in hardware names and CIM strings."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_strip_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_strings(item) for key, item in value.items()}
    return value


def _gb(value: Any) -> float:
    try:
        return round(float(value) / 1024**3, 2)
    except (TypeError, ValueError):
        return 0.0


def _percent(part: float, total: float) -> float:
    if not total:
        return 0.0
    return round(max(0.0, min(100.0, part / total * 100.0)), 1)


def _enabled_flag(value: Any) -> bool:
    """Interpret PowerShell flags that may be bool, 0/1, or true/false text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().casefold() in {"true", "1", "yes", "enabled", "on"}


def _collect_live(sample_seconds: float) -> dict[str, Any]:
    per_core = psutil.cpu_percent(interval=0.5, percpu=True)
    average = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    first_cpu = psutil.cpu_percent(interval=None)
    timeline: list[dict[str, float]] = []
    started = time.monotonic()
    start_net = psutil.net_io_counters()
    start_disk = psutil.disk_io_counters()

    while time.monotonic() - started < sample_seconds:
        time.sleep(0.5)
        timeline.append(
            {
                "seconds": round(time.monotonic() - started, 1),
                "cpu": round(psutil.cpu_percent(interval=None), 1),
                "memory": round(psutil.virtual_memory().percent, 1),
            }
        )

    elapsed = max(0.001, time.monotonic() - started)
    end_net = psutil.net_io_counters()
    end_disk = psutil.disk_io_counters()
    network_rate = {
        "sent_mbps": round(
            max(0, end_net.bytes_sent - start_net.bytes_sent) / elapsed / 1e6,
            2,
        ),
        "received_mbps": round(
            max(0, end_net.bytes_recv - start_net.bytes_recv) / elapsed / 1e6,
            2,
        ),
    }
    disk_rate = {
        "read_mbps": round(
            max(0, end_disk.read_bytes - start_disk.read_bytes) / elapsed / 1e6,
            2,
        )
        if end_disk and start_disk
        else 0.0,
        "write_mbps": round(
            max(0, end_disk.write_bytes - start_disk.write_bytes) / elapsed / 1e6,
            2,
        )
        if end_disk and start_disk
        else 0.0,
    }

    mounts: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        mounts.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "fstype": partition.fstype,
                "total_gb": _gb(usage.total),
                "used_gb": _gb(usage.used),
                "free_gb": _gb(usage.free),
                "percent": round(usage.percent, 1),
            }
        )

    processes = list(
        psutil.process_iter(
            ["pid", "name", "username", "memory_info", "status"]
        )
    )
    for process in processes:
        try:
            process.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.7)
    logical_cores = psutil.cpu_count(logical=True) or 1
    rows: list[dict[str, Any]] = []
    for process in processes:
        try:
            info = process.info
            memory_info = info.get("memory_info")
            raw_cpu = process.cpu_percent(None)
            rows.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "unknown",
                    "username": info.get("username") or "",
                    "status": info.get("status") or "",
                    "cpu_percent": round(raw_cpu, 1),
                    "cpu_system_percent": round(
                        raw_cpu / logical_cores,
                        1,
                    ),
                    "memory_mb": round(
                        (memory_info.rss if memory_info else 0) / 1024**2,
                        1,
                    ),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    cpu_rows = [
        row
        for row in rows
        if row["name"].casefold() not in {"system idle process", "idle"}
        and row["pid"] not in (0,)
    ]

    boot_time = psutil.boot_time()
    uptime_hours = round((time.time() - boot_time) / 3600, 1)
    frequency = psutil.cpu_freq()
    temperatures = getattr(psutil, "sensors_temperatures", lambda: {})() or {}

    return {
        "cpu": {
            "per_core_percent": [round(value, 1) for value in per_core],
            "average_percent": average,
            "first_sample_percent": round(first_cpu, 1),
            "physical_cores": psutil.cpu_count(logical=False) or 0,
            "logical_cores": psutil.cpu_count(logical=True) or 0,
            "frequency_mhz": round(frequency.current, 0) if frequency else 0,
            "temperatures": temperatures,
        },
        "memory": {
            "total_gb": _gb(memory.total),
            "used_gb": _gb(memory.used),
            "available_gb": _gb(memory.available),
            "percent": round(memory.percent, 1),
            "cached_gb": _gb(getattr(memory, "cached", 0)),
            "swap_total_gb": _gb(swap.total),
            "swap_used_gb": _gb(swap.used),
            "swap_percent": round(swap.percent, 1),
        },
        "timeline": timeline,
        "network_rate": network_rate,
        "disk_rate": disk_rate,
        "volumes": mounts,
        "top_processes": {
            "by_memory": sorted(
                rows,
                key=lambda item: item["memory_mb"],
                reverse=True,
            )[:10],
            "by_cpu": sorted(
                cpu_rows,
                key=lambda item: item["cpu_system_percent"],
                reverse=True,
            )[:10],
        },
        "boot_time": datetime.fromtimestamp(boot_time).isoformat(
            timespec="seconds"
        ),
        "uptime_hours": uptime_hours,
        "users": [
            {
                "name": user.name,
                "terminal": user.terminal,
                "host": user.host,
            }
            for user in psutil.users()
        ],
    }


def _run_benchmarks() -> dict[str, Any]:
    """Small, in-memory CPU and memory throughput tests."""
    single_block = b"x" * (16 * 1024 * 1024)
    started = time.perf_counter()
    hashlib.sha256(single_block).digest()
    single_elapsed = max(0.001, time.perf_counter() - started)
    single_mbps = (len(single_block) / 1024**2) / single_elapsed

    multi_blocks = [b"y" * (8 * 1024 * 1024) for _ in range(4)]
    started = time.perf_counter()

    def digest(block: bytes) -> bytes:
        return hashlib.sha256(block).digest()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(digest, multi_blocks))
    multi_elapsed = max(0.001, time.perf_counter() - started)
    multi_mbps = (sum(len(block) for block in multi_blocks) / 1024**2) / multi_elapsed

    source = bytearray(128 * 1024 * 1024)
    destination = bytearray(len(source))
    started = time.perf_counter()
    destination[:] = source
    copy_elapsed = max(0.001, time.perf_counter() - started)
    copy_gbps = (len(source) / 1024**3) / copy_elapsed

    return {
        "cpu_single_thread_mbps": round(single_mbps, 1),
        "cpu_multi_thread_mbps": round(multi_mbps, 1),
        "memory_copy_gbps": round(copy_gbps, 1),
        "cpu_single_thread_seconds": round(single_elapsed, 3),
        "cpu_multi_thread_seconds": round(multi_elapsed, 3),
        "memory_copy_seconds": round(copy_elapsed, 3),
    }


def _chart_style() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.facecolor": "#141414",
            "axes.facecolor": "#1b1b1b",
            "axes.edgecolor": "#333333",
            "axes.labelcolor": "#d4d4d4",
            "text.color": "#e5e5e5",
            "xtick.color": "#a3a3a3",
            "ytick.color": "#a3a3a3",
            "grid.color": "#2a2a2a",
            "font.size": 9,
        }
    )
    return plt


def _save_chart(plt: Any, fig: Any, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor="#141414", bbox_inches="tight")
    plt.close(fig)


def _make_charts(data: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    plt = _chart_style()
    charts: list[dict[str, Any]] = []

    def add(path: Path, title: str, description: str) -> None:
        charts.append(
            {
                "path": str(path),
                "title": title,
                "description": description,
            }
        )

    cpu = data["cpu"]
    per_core = cpu["per_core_percent"]
    if per_core:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        axes = list(range(1, len(per_core) + 1))
        colors = ["#f97316" if value >= 85 else "#4ade80" for value in per_core]
        ax.bar(axes, per_core, color=colors, width=0.75)
        ax.axhline(cpu["average_percent"], color="#60a5fa", linestyle="--", linewidth=1, label=f"Average {cpu['average_percent']}%")
        ax.set_ylim(0, 100)
        ax.set_xlabel("Logical core")
        ax.set_ylabel("Utilization (%)")
        ax.set_title("CPU utilization per logical core")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(loc="upper right", frameon=False)
        path = output_dir / "01_cpu_cores.png"
        _save_chart(plt, fig, path)
        add(path, "CPU per core", "Instantaneous utilization of every logical CPU core.")

    timeline = data.get("timeline") or []
    if len(timeline) >= 2:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        seconds = [item["seconds"] for item in timeline]
        ax.plot(seconds, [item["cpu"] for item in timeline], color="#4ade80", marker="o", markersize=3, label="CPU")
        ax.plot(seconds, [item["memory"] for item in timeline], color="#60a5fa", marker="o", markersize=3, label="Memory")
        ax.set_ylim(0, 100)
        ax.set_xlabel("Seconds")
        ax.set_ylabel("Utilization (%)")
        ax.set_title("CPU and memory over the sampling window")
        ax.grid(alpha=0.25)
        ax.legend(frameon=False)
        path = output_dir / "02_cpu_memory_timeline.png"
        _save_chart(plt, fig, path)
        add(path, "CPU and memory timeline", "Live utilization sampled over several seconds.")

    by_memory = data["top_processes"]["by_memory"][:8]
    by_cpu = data["top_processes"]["by_cpu"][:8]
    if by_memory or by_cpu:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
        if by_memory:
            names = [item["name"][:26] for item in reversed(by_memory)]
            values = [item["memory_mb"] / 1024 for item in reversed(by_memory)]
            axes[0].barh(names, values, color="#818cf8")
            axes[0].set_xlabel("Memory (GiB)")
            axes[0].set_title("Top processes by memory")
            axes[0].grid(axis="x", alpha=0.25)
        else:
            axes[0].set_visible(False)
        if by_cpu:
            names = [item["name"][:26] for item in reversed(by_cpu)]
            values = [
                item.get("cpu_system_percent", item.get("cpu_percent", 0))
                for item in reversed(by_cpu)
            ]
            axes[1].barh(names, values, color="#fb923c")
            axes[1].set_xlabel("CPU (% of total system)")
            axes[1].set_title("Top processes by CPU")
            axes[1].grid(axis="x", alpha=0.25)
        else:
            axes[1].set_visible(False)
        path = output_dir / "03_top_processes.png"
        _save_chart(plt, fig, path)
        add(path, "Top processes", "Largest processes by memory and CPU usage.")

    volumes = data.get("volumes") or []
    if volumes:
        fig, ax = plt.subplots(figsize=(8, 3.8))
        labels = [volume["device"].rstrip("\\") or volume["mountpoint"] for volume in volumes]
        used = [volume["used_gb"] for volume in volumes]
        free = [volume["free_gb"] for volume in volumes]
        positions = list(range(len(volumes)))
        ax.bar(positions, used, color="#f87171", label="Used")
        ax.bar(positions, free, bottom=used, color="#4ade80", label="Free")
        ax.set_xticks(positions)
        ax.set_xticklabels(labels)
        ax.set_ylabel("Capacity (GiB)")
        ax.set_title("Disk usage by volume")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(frameon=False)
        for index, volume in enumerate(volumes):
            total = volume["total_gb"]
            if total:
                ax.text(
                    index,
                    total * 1.02,
                    f"{volume['percent']}%",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                )
        path = output_dir / "04_disks.png"
        _save_chart(plt, fig, path)
        add(path, "Disk usage", "Used and free capacity for each mounted volume.")

    benchmarks = data.get("benchmarks") or {}
    if benchmarks:
        fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
        labels = ["1 thread", "4 threads"]
        values = [
            benchmarks.get("cpu_single_thread_mbps", 0),
            benchmarks.get("cpu_multi_thread_mbps", 0),
        ]
        axes[0].bar(labels, values, color=["#60a5fa", "#4ade80"])
        axes[0].set_ylabel("SHA-256 throughput (MiB/s)")
        axes[0].set_title("CPU hashing benchmark")
        axes[0].grid(axis="y", alpha=0.25)
        memory_value = benchmarks.get("memory_copy_gbps", 0)
        axes[1].bar(["Memory copy"], [memory_value], color="#fbbf24")
        axes[1].set_ylabel("Throughput (GiB/s)")
        axes[1].set_title("Memory copy benchmark")
        axes[1].grid(axis="y", alpha=0.25)
        path = output_dir / "05_benchmarks.png"
        _save_chart(plt, fig, path)
        add(path, "Performance benchmarks", "Short CPU and memory throughput tests.")

    gpu_rows = data.get("gpu_processes") or []
    if gpu_rows:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        names = [row["name"][:26] for row in reversed(gpu_rows[:8])]
        values = [row["utilization"] for row in reversed(gpu_rows[:8])]
        ax.barh(names, values, color="#c084fc")
        ax.set_xlabel("GPU utilization sum (%)")
        ax.set_title("Top processes by GPU utilization")
        ax.grid(axis="x", alpha=0.25)
        path = output_dir / "06_gpu.png"
        _save_chart(plt, fig, path)
        add(path, "GPU utilization", "GPU engine utilization grouped by process.")

    return charts


def _gpu_processes(windows: dict[str, Any]) -> list[dict[str, Any]]:
    rows = windows.get("gpu_utilization") or []
    if not isinstance(rows, list):
        return []
    names = {
        process.info["pid"]: process.info.get("name") or f"PID {process.info['pid']}"
        for process in psutil.process_iter(["pid", "name"])
        if process.info.get("pid") is not None
    }
    grouped: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        match = re.search(r"pid_(\d+)", str(row.get("InstanceName") or ""))
        if match is None:
            continue
        try:
            pid = int(match.group(1))
            grouped[pid] = grouped.get(pid, 0.0) + float(row.get("CookedValue") or 0)
        except (TypeError, ValueError):
            continue
    return sorted(
        [
            {
                "pid": pid,
                "name": names.get(pid, f"PID {pid}"),
                "utilization": round(value, 1),
            }
            for pid, value in grouped.items()
            if value >= 0.05
        ],
        key=lambda item: item["utilization"],
        reverse=True,
    )


def _build_findings(data: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    memory = data["memory"]
    if memory["percent"] >= 90:
        findings.append(
            {
                "severity": "critical",
                "title": "Memory pressure is very high",
                "detail": f"{memory['percent']}% of {memory['total_gb']} GiB is in use; only {memory['available_gb']} GiB is available.",
            }
        )
    elif memory["percent"] >= 80:
        findings.append(
            {
                "severity": "warning",
                "title": "Memory usage is elevated",
                "detail": f"{memory['percent']}% of {memory['total_gb']} GiB is in use.",
            }
        )

    for volume in data.get("volumes") or []:
        if volume["percent"] >= 95:
            findings.append(
                {
                    "severity": "critical",
                    "title": f"Volume {volume['device']} is nearly full",
                    "detail": f"{volume['percent']}% used; {volume['free_gb']} GiB free.",
                }
            )
        elif volume["percent"] >= 90:
            findings.append(
                {
                    "severity": "warning",
                    "title": f"Volume {volume['device']} is filling up",
                    "detail": f"{volume['percent']}% used; {volume['free_gb']} GiB free.",
                }
            )

    for disk in data.get("physical_disks") or []:
        health = str(disk.get("HealthStatus") or "").strip()
        operational = str(disk.get("OperationalStatus") or "").strip()
        if health and health.casefold() != "healthy":
            findings.append(
                {
                    "severity": "critical",
                    "title": f"Physical disk reports {health}",
                    "detail": f"{disk.get('FriendlyName', 'Unknown disk')} health is {health}; operational status: {operational or 'unknown'}.",
                }
            )
        elif operational and operational.casefold() not in {"ok", "online"}:
            findings.append(
                {
                    "severity": "warning",
                    "title": "Physical disk is not in a normal operational state",
                    "detail": f"{disk.get('FriendlyName', 'Unknown disk')}: {operational}.",
                }
            )

    defender = data.get("defender") or {}
    if defender:
        if not defender.get("antivirus_enabled", True):
            findings.append(
                {
                    "severity": "warning",
                    "title": "Microsoft Defender antivirus is disabled",
                    "detail": "Antivirus protection is reported as disabled.",
                }
            )
        if not defender.get("realtime_enabled", True):
            findings.append(
                {
                    "severity": "warning",
                    "title": "Defender real-time protection is off",
                    "detail": "Real-time protection is reported as disabled.",
                }
            )

    firewall = data.get("firewall") or []
    firewall_total = sum(1 for item in firewall if isinstance(item, dict))
    firewall_enabled = sum(
        1
        for item in firewall
        if isinstance(item, dict) and _enabled_flag(item.get("Enabled"))
    )
    if firewall_total and firewall_enabled == 0:
        findings.append(
            {
                "severity": "critical",
                "title": "Windows Firewall is disabled for every profile",
                "detail": (
                    f"0 of {firewall_total} network profiles have the firewall "
                    "enabled. Enable Domain, Private, and Public protection in "
                    "Windows Security unless a third-party firewall is active."
                ),
            }
        )
    elif firewall_total and firewall_enabled < firewall_total:
        findings.append(
            {
                "severity": "warning",
                "title": "Windows Firewall is disabled for some profiles",
                "detail": (
                    f"Only {firewall_enabled} of {firewall_total} network "
                    "profiles have the firewall enabled."
                ),
            }
        )

    auto_stopped = data.get("services", {}).get("automatic_stopped") or []
    if len(auto_stopped) >= 5:
        findings.append(
            {
                "severity": "warning",
                "title": "Several automatic services are stopped",
                "detail": f"{len(auto_stopped)} automatic services are not running.",
            }
        )

    events = data.get("recent_events") or []
    if len(events) >= 10:
        findings.append(
            {
                "severity": "warning",
                "title": "Repeated system errors in the event log",
                "detail": f"{len(events)} error/critical System events were recorded in the last 7 days.",
            }
        )

    if data.get("pending_reboot"):
        findings.append(
            {
                "severity": "info",
                "title": "A reboot is pending",
                "detail": "Windows has pending servicing or update work that requires a restart.",
            }
        )

    if data.get("uptime_hours", 0) >= 24 * 30:
        findings.append(
            {
                "severity": "info",
                "title": "Long uptime",
                "detail": f"The system has been running for {data['uptime_hours'] / 24:.0f} days; a restart may clear leaked resources.",
            }
        )

    top_memory = (data.get("top_processes", {}).get("by_memory") or [None])[0]
    if top_memory and top_memory["memory_mb"] >= 4096:
        findings.append(
            {
                "severity": "info",
                "title": "A process is using a large amount of memory",
                "detail": f"{top_memory['name']} uses {top_memory['memory_mb'] / 1024:.1f} GiB.",
            }
        )

    gpu_rows = [
        row
        for row in data.get("gpu_processes") or []
        if isinstance(row, dict)
        and row.get("name") not in {"llama-server.exe", "ollama.exe"}
    ]
    top_gpu = max(
        gpu_rows,
        key=lambda row: float(row.get("utilization") or 0),
        default=None,
    )
    if top_gpu and float(top_gpu.get("utilization") or 0) >= 60:
        findings.append(
            {
                "severity": "info",
                "title": "The GPU is busy with another application",
                "detail": (
                    f"{top_gpu.get('name')} is using "
                    f"{min(100, round(float(top_gpu.get('utilization') or 0)))}% "
                    "of the GPU. The local model shares the GPU, so replies "
                    "will be slower until that application is closed or its "
                    "frame rate is capped."
                ),
            }
        )

    if not findings:
        findings.append(
            {
                "severity": "info",
                "title": "No critical issues detected",
                "detail": "Resource usage, storage health, and security indicators look normal.",
            }
        )
    return findings


def _health_score(findings: list[dict[str, str]]) -> int:
    score = 100
    for finding in findings:
        if finding["severity"] == "critical":
            score -= 20
        elif finding["severity"] == "warning":
            score -= 8
        elif finding["severity"] == "info":
            score -= 2
    return max(0, score)


def _compact_report(data: dict[str, Any]) -> dict[str, Any]:
    """A model-friendly report that fits comfortably in the context window."""
    cpu = data["cpu"]
    per_core = cpu.get("per_core_percent") or []
    timeline = data.get("timeline") or []
    timeline_cpu = [item["cpu"] for item in timeline]
    timeline_memory = [item["memory"] for item in timeline]
    services = data.get("services") or {}
    defender = data.get("defender") or {}
    firewall = data.get("firewall") or []
    firewall_total = sum(1 for item in firewall if isinstance(item, dict))
    firewall_enabled = sum(
        1
        for item in firewall
        if isinstance(item, dict) and _enabled_flag(item.get("Enabled"))
    )
    reliability = [
        {
            "disk": item.get("disk"),
            "temperature_c": item.get("temperature"),
            "wear_percent": item.get("wear"),
            "power_on_hours": item.get("power_on_hours"),
            "read_errors": item.get("read_errors"),
            "write_errors": item.get("write_errors"),
        }
        for item in data.get("reliability") or []
        if isinstance(item, dict)
        and (
            item.get("temperature") is not None
            or item.get("wear") is not None
            or item.get("power_on_hours") is not None
        )
    ]
    startup = [
        item.get("Name")
        for item in data.get("startup", [])
        if isinstance(item, dict) and item.get("Name")
    ]
    volumes = [
        {
            "device": item.get("device"),
            "total_gb": item.get("total_gb"),
            "used_gb": item.get("used_gb"),
            "free_gb": item.get("free_gb"),
            "percent": item.get("percent"),
        }
        for item in data.get("volumes", [])
        if isinstance(item, dict)
    ]
    disks = [
        {
            "name": item.get("FriendlyName"),
            "media": item.get("MediaType"),
            "health": item.get("HealthStatus"),
            "size_gb": _gb(item.get("Size")),
        }
        for item in data.get("physical_disks", [])
        if isinstance(item, dict)
    ]
    gpus = [
        {
            "name": item.get("Name"),
            "driver": item.get("DriverVersion"),
            "status": item.get("Status"),
        }
        for item in data.get("gpus", [])
        if isinstance(item, dict)
    ]
    top_processes = data.get("top_processes") or {}
    return {
        "os": {
            key: (data.get("os") or {}).get(key)
            for key in ("caption", "version", "build", "last_boot", "system_drive")
        },
        "computer": {
            key: (data.get("computer") or {}).get(key)
            for key in ("manufacturer", "model")
        },
        "cpu": {
            "name": (data.get("cpu_hardware") or {}).get("name"),
            "physical_cores": cpu.get("physical_cores"),
            "logical_cores": cpu.get("logical_cores"),
            "average_percent": cpu.get("average_percent"),
            "per_core": {
                "min": round(min(per_core), 1) if per_core else 0,
                "max": round(max(per_core), 1) if per_core else 0,
                "average": cpu.get("average_percent"),
            },
            "frequency_mhz": cpu.get("frequency_mhz"),
        },
        "memory": data.get("memory"),
        # Small numeric series so the chat can draw native, crisp charts
        # instead of embedding the rendered PNG files.
        "per_core_percent": [round(value, 1) for value in per_core],
        "timeline": [
            {
                "seconds": item.get("seconds"),
                "cpu": round(item.get("cpu", 0), 1),
                "memory": round(item.get("memory", 0), 1),
            }
            for item in timeline
        ],
        "gpus": gpus,
        "volumes": volumes,
        "physical_disks": disks,
        "smart": reliability,
        "smart_available": bool(reliability),
        "top_processes": {
            "by_memory": [
                {
                    "name": item["name"],
                    "memory_gb": round(item["memory_mb"] / 1024, 2),
                    "cpu_percent": item["cpu_system_percent"],
                }
                for item in (top_processes.get("by_memory") or [])[:5]
            ],
            "by_cpu": [
                {
                    "name": item["name"],
                    "cpu_percent": item["cpu_system_percent"],
                    "memory_gb": round(item["memory_mb"] / 1024, 2),
                }
                for item in (top_processes.get("by_cpu") or [])[:5]
            ],
        },
        "services": {
            "total": services.get("total"),
            "running": services.get("running"),
            "stopped": services.get("stopped"),
            "automatic_stopped_count": len(
                services.get("automatic_stopped") or []
            ),
            "automatic_stopped": [
                item.get("Name")
                for item in (services.get("automatic_stopped") or [])[:5]
                if isinstance(item, dict)
            ],
        },
        "startup": {
            "count": len(startup),
            "names": startup[:5],
        },
        "security": {
            "defender": {
                "antivirus": defender.get("antivirus_enabled"),
                "realtime": defender.get("realtime_enabled"),
                "signature_last_updated": defender.get(
                    "signature_last_updated"
                ),
            },
            "firewall_enabled": firewall_enabled,
            "firewall_profiles": firewall_total,
            "firewall_state": (
                "disabled"
                if firewall_total and firewall_enabled == 0
                else "partial"
                if firewall_total and firewall_enabled < firewall_total
                else "enabled"
                if firewall_total
                else "unknown"
            ),
        },
        "recent_events": [
            {
                "time": str(event.get("TimeCreated") or ""),
                "id": event.get("Id"),
                "level": event.get("LevelDisplayName"),
                "provider": event.get("ProviderName"),
                "message": str(event.get("Message") or "")[:160],
            }
            for event in (data.get("recent_events") or [])[:5]
            if isinstance(event, dict)
        ],
        "pending_reboot": data.get("pending_reboot"),
        "uptime_hours": data.get("uptime_hours"),
        "benchmarks": data.get("benchmarks", {}),
        "gpu_processes": (data.get("gpu_processes") or [])[:3],
        "network_rate": data.get("network_rate"),
        "disk_rate": data.get("disk_rate"),
        "sampling": {
            "seconds": len(timeline) * 0.5,
            "cpu_min": round(min(timeline_cpu), 1) if timeline_cpu else 0,
            "cpu_max": round(max(timeline_cpu), 1) if timeline_cpu else 0,
            "memory_max": round(max(timeline_memory), 1)
            if timeline_memory
            else 0,
        },
    }


def run_diagnosis(
    sample_seconds: Any = _DEFAULT_SAMPLE_SECONDS,
    include_benchmark: bool = True,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Collect a system diagnosis, write charts, and return structured results."""
    seconds = _clamp_sample_seconds(sample_seconds)
    include_benchmark = bool(include_benchmark)

    try:
        # The Windows checks and the live sampling are independent; run them
        # together so a full diagnosis does not take the sum of both.
        with ThreadPoolExecutor(max_workers=2) as pool:
            windows_future = pool.submit(_run_powershell_json, _PS_SCRIPT)
            live_future = pool.submit(_collect_live, seconds)
            windows = _strip_strings(
                _normalise_dates(windows_future.result())
            )
            live = live_future.result()
    except Exception as exc:  # noqa: BLE001 - report a readable tool error
        logger.exception("System diagnosis collection failed")
        return {
            "ok": False,
            "error": f"System diagnosis failed: {exc}",
        }

    data: dict[str, Any] = {
        **live,
        "os": windows.get("os") or {},
        "computer": windows.get("computer") or {},
        "cpu_hardware": windows.get("cpu") or {},
        "memory_modules": windows.get("memory_modules") or [],
        "gpus": windows.get("gpus") or [],
        "physical_disks": windows.get("physical_disks") or [],
        "windows_volumes": windows.get("volumes") or [],
        "reliability": windows.get("reliability") or [],
        "startup": windows.get("startup") or [],
        "services": windows.get("services") or {},
        "defender": windows.get("defender") or {},
        "firewall": windows.get("firewall") or [],
        "netadapters": windows.get("netadapters") or [],
        "hotfixes": windows.get("hotfixes") or [],
        "recent_events": windows.get("events") or [],
        "pending_reboot": bool(windows.get("pending_reboot")),
        "gpu_memory": windows.get("gpu_memory") or [],
    }
    data["gpu_processes"] = _gpu_processes(windows)

    if include_benchmark:
        try:
            data["benchmarks"] = _run_benchmarks()
        except Exception:  # noqa: BLE001 - benchmark is optional
            logger.warning("System diagnosis benchmark failed", exc_info=True)
            data["benchmarks"] = {}

    findings = _build_findings(data)
    data["findings"] = findings
    data["health_score"] = _health_score(findings)

    if output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = PROJECT_ROOT / "memory" / "diagnostics" / f"{stamp}-{os.getpid()}"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        charts = _make_charts(data, output_dir)
    except Exception:  # noqa: BLE001 - charts are best effort
        logger.exception("System diagnosis chart generation failed")
        charts = []

    files = [chart["path"] for chart in charts]
    critical = sum(1 for item in findings if item["severity"] == "critical")
    warnings = sum(1 for item in findings if item["severity"] == "warning")
    message = (
        f"System diagnosis completed. Health score: {data['health_score']}/100. "
        f"{critical} critical and {warnings} warning finding(s). "
        f"{len(charts)} chart(s) saved under {output_dir}."
    )

    report_path: Path | None = output_dir / "report.json"
    try:
        report_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "health_score": data["health_score"],
                    "findings": findings,
                    "charts": charts,
                    "summary": data,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not write the system diagnosis report")
        report_path = None

    return {
        "ok": True,
        "message": message,
        "health_score": data["health_score"],
        "findings": findings,
        "charts": [
            {"path": chart["path"], "title": chart["title"]}
            for chart in charts
        ],
        "files": files,
        "report_dir": str(output_dir),
        "report_file": str(report_path) if report_path else None,
        "summary": _compact_report(data),
        "next_step": (
            "Write a concise professional summary in 250-350 words using Markdown "
            "'##' headings and short bullets. Use exactly this structure: "
            "'## Assessment' (2 sentences), '## Priority findings' (the 3 most "
            "important items, one bullet each), and '## Recommended actions' "
            "(3 numbered actions). The dashboard already shows hardware, storage, "
            "SMART, security, processes, and benchmarks, so do not repeat those "
            "details. Match severity words to the findings: do not call a warning "
            "critical. Do not use emoji, '---' rules, bold pseudo-headings, file "
            "names, image links, or chart paths. If you mention the firewall, "
            "state it literally: 'firewall_state' is disabled, partial, or "
            "enabled, and 'firewall_enabled' is the number of enabled profiles out "
            "of 'firewall_profiles'. Mention SSD temperature/wear only when "
            "'smart_available' is true, and never claim SMART data that is absent. "
            "Do not call any more tools; this report covers the diagnosis."
        ),
        "notes": [
            "Deep file-integrity scans (SFC/DISM) are not run automatically; "
            "they can take several minutes and may require administrator rights.",
            "SMART reliability counters may be unavailable without administrator "
            "rights.",
            "Benchmarks are short in-memory tests and can be affected by other "
            "workloads running on the machine.",
        ],
    }
