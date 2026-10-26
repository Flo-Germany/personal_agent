"""Hardware profiling and GPU-layer planning for the llama.cpp engine.

The goal is to use as much VRAM as safely fits a model, and to fall back to a
pure CPU (AVX2/AVX-512) run instead of crashing with an out-of-memory error on
small machines.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import psutil

from engine.gguf import GgufInfo

logger = logging.getLogger(__name__)

_GIB = 1024**3
_DEFAULT_RESERVE_GIB = 1.5
_DEFAULT_HEADROOM = 0.9

_HEAP_SIZE = re.compile(r"memoryHeaps\[(\d+)\]:")
_HEAP_VALUE = re.compile(r"^\s*(size|flags)\s*=\s*(\S.*)$", re.IGNORECASE)
_DEVICE_LOCAL = re.compile(r"DEVICE_LOCAL", re.IGNORECASE)


@dataclass
class HardwareProfile:
    """What the engine may use right now."""

    gpu_name: str = ""
    vram_bytes: int = 0
    ram_bytes: int = 0
    cpu: str = ""
    platform: str = ""
    vulkan: bool = False
    source: str = "unknown"

    @property
    def vram_gb(self) -> float:
        return round(self.vram_bytes / _GIB, 2) if self.vram_bytes else 0.0

    @property
    def ram_gb(self) -> float:
        return round(self.ram_bytes / _GIB, 2) if self.ram_bytes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "gpu_name": self.gpu_name,
            "vram_gb": self.vram_gb,
            "ram_gb": self.ram_gb,
            "cpu": self.cpu,
            "platform": self.platform,
            "vulkan": self.vulkan,
            "source": self.source,
            "cpu_fallback": True,
            "cpu_note": (
                "llama.cpp selects the best AVX2/AVX-512 kernel for this CPU "
                "automatically."
            ),
        }


@dataclass
class LayerPlan:
    n_gpu_layers: int
    total_layers: int
    weights_gb: float
    kv_gb: float
    budget_gb: float
    vram_gb: float
    fully_on_gpu: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_gpu_layers": self.n_gpu_layers,
            "total_layers": self.total_layers,
            "weights_gb": self.weights_gb,
            "kv_gb": self.kv_gb,
            "budget_gb": self.budget_gb,
            "vram_gb": self.vram_gb,
            "fully_on_gpu": self.fully_on_gpu,
            "reason": self.reason,
        }


def _parse_vulkan_memory(output: str) -> tuple[str, int]:
    """Return (gpu name, largest DEVICE_LOCAL heap bytes) from vulkaninfo."""
    name = ""
    match = re.search(r"deviceName\s*=\s*(.+)", output)
    if match:
        name = match.group(1).strip()

    heaps: dict[int, dict[str, Any]] = {}
    current: int | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("memoryTypes:"):
            # Types reference the same heaps again; stop before re-parsing.
            break
        head = re.match(r"memoryHeaps\[(\d+)\]:", stripped)
        if head:
            current = int(head.group(1))
            heaps.setdefault(current, {})
            continue
        if current is None:
            continue
        if "MEMORY_HEAP_DEVICE_LOCAL_BIT" in line:
            heaps[current]["device_local"] = True
        size = re.match(r"size\s*=\s*(\d+)", stripped)
        if size:
            heaps[current]["size"] = int(size.group(1))

    device_local = [
        heap.get("size", 0)
        for heap in heaps.values()
        if heap.get("device_local")
    ]
    return name, max(device_local, default=0)


@lru_cache(maxsize=1)
def _registry_gpu() -> tuple[str, int]:
    """Read the dedicated VRAM size from the Windows display class registry.

    ``vulkaninfo`` prints accurate heaps but takes 30-60 s on machines with
    many Vulkan layers; startup must not block on that. AMD and NVIDIA both
    publish the real VRAM size as a QWORD here.
    """
    if platform.system() != "Windows":
        return "", 0
    try:
        import winreg
    except ImportError:
        return "", 0

    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best_name, best_size = "", 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not (len(subkey_name) == 4 and subkey_name.isdigit()):
                    continue
                try:
                    with winreg.OpenKey(root, subkey_name) as gpu:
                        name = str(winreg.QueryValueEx(gpu, "DriverDesc")[0])
                        size = int(
                            winreg.QueryValueEx(
                                gpu, "HardwareInformation.qwMemorySize"
                            )[0]
                        )
                except (OSError, ValueError, TypeError):
                    continue
                if size > best_size:
                    best_name, best_size = name, size
    except OSError:
        return "", 0
    return best_name, best_size


@lru_cache(maxsize=1)
def _probe_vulkan() -> tuple[str, int, bool, str]:
    name, vram = _registry_gpu()
    if vram:
        return name, vram, True, "registry qwMemorySize"

    executable = shutil.which("vulkaninfo") or shutil.which("vulkaninfo.exe")
    if not executable:
        return "", 0, False, "vulkaninfo not found"
    try:
        completed = subprocess.run(
            [executable, "--summary"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        summary = completed.stdout or ""
        if "Devices:" not in summary:
            return "", 0, False, "no Vulkan device reported"
        full = subprocess.run(
            [executable],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (full.stdout or "") + summary
    except (OSError, subprocess.SubprocessError) as exc:
        return "", 0, False, f"vulkaninfo failed: {exc}"

    name, vram = _parse_vulkan_memory(output)
    return name, vram, bool(vram), ("vulkan heap" if vram else "shared memory")


def detect_hardware(vram_override_gb: float | None = None) -> HardwareProfile:
    """Profile the machine, with an optional operator override for VRAM."""
    cpu = platform.processor() or ""
    profile = HardwareProfile(
        cpu=cpu.strip() or "unknown CPU",
        ram_bytes=int(psutil.virtual_memory().total),
        platform=f"{platform.system()} {platform.release()}",
    )

    name, vram, vulkan, source = _probe_vulkan()
    profile.gpu_name = name
    profile.vulkan = vulkan
    profile.source = source
    profile.vram_bytes = vram

    if vram_override_gb is not None and vram_override_gb > 0:
        profile.vram_bytes = int(vram_override_gb * _GIB)
        profile.source = f"configured override {vram_override_gb:g} GB"

    if not profile.vram_bytes:
        logger.info("No dedicated Vulkan VRAM detected; the engine will use CPU")
    return profile


def plan_gpu_layers(
    profile: HardwareProfile,
    info: GgufInfo,
    context_tokens: int,
    *,
    reserve_gb: float = _DEFAULT_RESERVE_GIB,
    headroom: float = _DEFAULT_HEADROOM,
    override: int | None = None,
) -> LayerPlan:
    """Decide how many layers fit in VRAM without an OOM crash.

    ``reserve_gb`` leaves room for the desktop and driver; ``headroom`` keeps
    a safety margin for CUDA/HIP/Vulkan allocator fragmentation. The result is
    clamped to the model's layer count and never negative.
    """
    total_layers = info.layer_count
    vram_gb = profile.vram_gb
    weights_gb = info.file_size / _GIB
    kv_gb = info.kv_bytes(context_tokens) / _GIB

    if override is not None:
        layers = max(0, min(int(override), total_layers))
        return LayerPlan(
            n_gpu_layers=layers,
            total_layers=total_layers,
            weights_gb=round(weights_gb, 2),
            kv_gb=round(kv_gb, 2),
            budget_gb=round(vram_gb, 2),
            vram_gb=vram_gb,
            fully_on_gpu=layers >= total_layers,
            reason="configured gpu_layers override",
        )

    if not profile.vram_bytes:
        return LayerPlan(
            n_gpu_layers=0,
            total_layers=total_layers,
            weights_gb=round(weights_gb, 2),
            kv_gb=round(kv_gb, 2),
            budget_gb=0.0,
            vram_gb=0.0,
            fully_on_gpu=False,
            reason="no dedicated VRAM detected; running on CPU",
        )

    budget_gb = max(0.0, (vram_gb - reserve_gb) * headroom)
    if budget_gb <= kv_gb:
        return LayerPlan(
            n_gpu_layers=0,
            total_layers=total_layers,
            weights_gb=round(weights_gb, 2),
            kv_gb=round(kv_gb, 2),
            budget_gb=round(budget_gb, 2),
            vram_gb=vram_gb,
            fully_on_gpu=False,
            reason=(
                f"{vram_gb:g} GB VRAM is not enough for the {kv_gb:.2f} GB "
                "KV cache after reserving desktop memory; using CPU"
            ),
        )

    per_layer_gb = info.bytes_per_layer() / _GIB
    layers = int((budget_gb - kv_gb) // per_layer_gb) if per_layer_gb else 0
    layers = max(0, min(total_layers, layers))
    if layers >= total_layers:
        reason = f"all {total_layers} layers fit in {budget_gb:.2f} GB of a {vram_gb:g} GB budget"
    elif layers <= 0:
        reason = "weights exceed the VRAM budget; using CPU"
    else:
        reason = (
            f"{layers}/{total_layers} layers fit in {budget_gb:.2f} GB "
            f"(~{weights_gb:.2f} GB weights, {kv_gb:.2f} GB KV cache)"
        )

    return LayerPlan(
        n_gpu_layers=layers,
        total_layers=total_layers,
        weights_gb=round(weights_gb, 2),
        kv_gb=round(kv_gb, 2),
        budget_gb=round(budget_gb, 2),
        vram_gb=vram_gb,
        fully_on_gpu=layers >= total_layers,
        reason=reason,
    )
