"""Curated GGUF catalog for the integrated model manager.

Every entry pins the exact file, byte size, and SHA256 published by
HuggingFace, so a download can be verified and a partially written file can be
detected and removed instead of being loaded as a corrupt model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

TEXT = "text"
VISION = "vision"

HIGH = "High-End PC (16GB+ VRAM) - Maximum reasoning"
MID = "Mid-Range (8GB VRAM) - Balanced"
LOW = "Low-End PC (4GB VRAM or CPU) - Fast & lightweight"


@dataclass(frozen=True)
class ModelFile:
    role: str  # "model" or "mmproj"
    repo: str
    filename: str
    size: int
    sha256: str
    url_override: str = ""

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        return (
            f"https://huggingface.co/{self.repo}/resolve/main/"
            f"{quote(self.filename)}?download=true"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "url": self.url,
        }


@dataclass(frozen=True)
class ModelEntry:
    id: str
    name: str
    task: str
    family: str
    parameters: str
    quantization: str
    badge: str
    min_vram_gb: float
    description: str
    context: int
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def file(self, role: str) -> ModelFile | None:
        return next((item for item in self.files if item.role == role), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task": self.task,
            "family": self.family,
            "parameters": self.parameters,
            "quantization": self.quantization,
            "badge": self.badge,
            "min_vram_gb": self.min_vram_gb,
            "description": self.description,
            "context": self.context,
            "total_bytes": self.total_bytes,
            "files": [item.as_dict() for item in self.files],
        }


def _entry(
    ident: str,
    name: str,
    task: str,
    family: str,
    parameters: str,
    quant: str,
    badge: str,
    min_vram_gb: float,
    description: str,
    context: int,
    model: tuple[str, str, int, str],
    mmproj: tuple[str, str, int, str] | None = None,
) -> ModelEntry:
    files = [ModelFile("model", *model)]
    if mmproj is not None:
        files.append(ModelFile("mmproj", *mmproj))
    return ModelEntry(
        id=ident,
        name=name,
        task=task,
        family=family,
        parameters=parameters,
        quantization=quant,
        badge=badge,
        min_vram_gb=min_vram_gb,
        description=description,
        context=context,
        files=tuple(files),
    )


CATALOG: tuple[ModelEntry, ...] = (
    _entry(
        "qwen3-0.6b-q4",
        "Qwen3 0.6B",
        TEXT,
        "qwen",
        "0.6B",
        "Q4_K_M",
        LOW,
        1.0,
        "Tiny test and utility model. Runs on CPU, ideal for smoke tests and "
        "low-end office PCs.",
        32768,
        (
            "unsloth/Qwen3-0.6B-GGUF",
            "Qwen3-0.6B-Q4_K_M.gguf",
            396_705_472,
            "ac2d97712095a558e31573f62f466a3f9d93990898b0ec79d7c974c1780d524a",
        ),
    ),
    _entry(
        "qwen3-4b-q4",
        "Qwen3 4B Instruct 2507",
        TEXT,
        "qwen",
        "4B",
        "Q4_K_M",
        MID,
        6.0,
        "Balanced daily driver: strong instruction following and tool use at "
        "2.4 GB.",
        32768,
        (
            "unsloth/Qwen3-4B-Instruct-2507-GGUF",
            "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            2_497_281_120,
            "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
        ),
    ),
    _entry(
        "qwen3-8b-q4",
        "Qwen3 8B",
        TEXT,
        "qwen",
        "8B",
        "Q4_K_M",
        HIGH,
        10.0,
        "Maximum local reasoning for chat and tool planning. Uses most of a "
        "16 GB GPU.",
        32768,
        (
            "unsloth/Qwen3-8B-GGUF",
            "Qwen3-8B-Q4_K_M.gguf",
            5_027_784_512,
            "120307ba529eb2439d6c430d94104dabd578497bc7bfe7e322b5d9933b449bd4",
        ),
    ),
    _entry(
        "hermes3-8b-q4",
        "Hermes 3 Llama 3.1 8B",
        TEXT,
        "llama",
        "8B",
        "Q4_K_M",
        HIGH,
        10.0,
        "Alternative 8B with a different personality; good tool-call support.",
        32768,
        (
            "NousResearch/Hermes-3-Llama-3.1-8B-GGUF",
            "Hermes-3-Llama-3.1-8B.Q4_K_M.gguf",
            4_920_733_824,
            "d4403ce5a6e930f4c2509456388c20d633a15ff08dd52ef3b142ff1810ec3553",
        ),
    ),
    _entry(
        "qwen3-vl-2b-q4",
        "Qwen3-VL 2B",
        VISION,
        "qwen",
        "2B",
        "Q4_K_M + F16 mmproj",
        LOW,
        4.0,
        "Screen understanding and Set-of-Mark grounding on modest hardware.",
        16384,
        (
            "unsloth/Qwen3-VL-2B-Instruct-GGUF",
            "Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
            1_107_410_624,
            "858fcf2a39dc73b26dd86592cb0a5f949b59d1edb365d1dea98e46b02e955e56",
        ),
        (
            "unsloth/Qwen3-VL-2B-Instruct-GGUF",
            "mmproj-F16.gguf",
            819_395_232,
            "cd5a851d3928697fa1bd76d459d2cc409b6cf40c9d9682b2f5c8e7c6a9f9630f",
        ),
    ),
    _entry(
        "qwen3-vl-4b-q4",
        "Qwen3-VL 4B",
        VISION,
        "qwen",
        "4B",
        "Q4_K_M + F16 mmproj",
        MID,
        8.0,
        "Balanced vision: better UI text and layout understanding than 2B.",
        16384,
        (
            "unsloth/Qwen3-VL-4B-Instruct-GGUF",
            "Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
            2_497_282_336,
            "d4dcd426bfba75752a312b266b80fec8136fbaca13c62d93b7ac41fa67f0492b",
        ),
        (
            "unsloth/Qwen3-VL-4B-Instruct-GGUF",
            "mmproj-F16.gguf",
            836_180_640,
            "1b9f4e92f0fbda14d7d7b58baed86039b8a980fe503d9d6a9393f25c0028f1fc",
        ),
    ),
    _entry(
        "qwen3-vl-8b-q4",
        "Qwen3-VL 8B",
        VISION,
        "qwen",
        "8B",
        "Q4_K_M + F16 mmproj",
        HIGH,
        12.0,
        "Best local vision quality; uses ~6.2 GB for weights plus the projector.",
        16384,
        (
            "unsloth/Qwen3-VL-8B-Instruct-GGUF",
            "Qwen3-VL-8B-Instruct-Q4_K_M.gguf",
            5_027_785_568,
            "108e7ff92b78eefd3db4741885104acba514255c11b617d3c7b197a5f46efe89",
        ),
        (
            "unsloth/Qwen3-VL-8B-Instruct-GGUF",
            "mmproj-F16.gguf",
            1_159_030_336,
            "d406d03ebabefdef86a2c86bf0c1b65f9e046f7a81c218f25de4931b46a07fc4",
        ),
    ),
)

BY_ID = {entry.id: entry for entry in CATALOG}


def get_entry(model_id: str) -> ModelEntry:
    entry = BY_ID.get(model_id)
    if entry is None:
        raise KeyError(f"Unknown model id: {model_id}")
    return entry


def models_for(task: str) -> list[ModelEntry]:
    return [entry for entry in CATALOG if entry.task == task]
