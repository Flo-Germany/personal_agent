"""Minimal GGUF metadata reader used for sizing and layer planning.

Only the metadata block is read (it sits at the start of the file), so this
works on multi-gigabyte models without loading them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GGUF_MAGIC = b"GGUF"

_UINT8 = 0
_INT8 = 1
_UINT16 = 2
_INT16 = 3
_UINT32 = 4
_INT32 = 5
_FLOAT32 = 6
_BOOL = 7
_STRING = 8
_ARRAY = 9
_UINT64 = 10
_INT64 = 11
_FLOAT64 = 12

_SCALAR_FORMATS: dict[int, tuple[str, int]] = {
    _UINT8: ("<B", 1),
    _INT8: ("<b", 1),
    _UINT16: ("<H", 2),
    _INT16: ("<h", 2),
    _UINT32: ("<I", 4),
    _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4),
    _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8),
    _INT64: ("<q", 8),
    _FLOAT64: ("<d", 8),
}

# Metadata keys this module cares about, per architecture.
WANTED_KEYS = (
    "general.architecture",
    "general.name",
    "general.size_label",
    "general.parameter_count",
    "block_count",
    "embedding_length",
    "attention.head_count",
    "attention.head_count_kv",
    "attention.key_length",
    "attention.value_length",
    "context_length",
    "rope.freq_base",
)


@dataclass
class GgufInfo:
    """Everything the planner needs to size a model."""

    path: Path
    file_size: int
    architecture: str = ""
    name: str = ""
    block_count: int = 0
    embedding_length: int = 0
    head_count: int = 0
    head_count_kv: list[int] = field(default_factory=list)
    key_length: int = 0
    value_length: int = 0
    context_length: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def layer_count(self) -> int:
        return max(1, self.block_count)

    def bytes_per_layer(self) -> int:
        """Approximate weight bytes per transformer block.

        0.94 keeps a little room for the token embedding (which is offloaded
        separately) so the estimate errs toward fitting, not OOM.
        """
        total = self.file_size * 0.94
        return max(1, int(total // self.layer_count))

    def kv_bytes_per_token(self) -> int:
        """KV-cache bytes for one token with f16 K/V, all layers."""
        heads = self.head_count_kv or [self.head_count or 1]
        if not self.head_count:
            return 0
        if self.key_length and self.value_length:
            per_head_dim = (self.key_length + self.value_length) / 2
        else:
            per_head_dim = self.embedding_length / max(1, self.head_count)
        total_heads = sum(heads) if len(heads) > 1 else heads[0] * self.layer_count
        # 2 bytes per value (f16), K plus V already folded into per_head_dim.
        return int(total_heads * per_head_dim * 2 * 2)

    def kv_bytes(self, context_tokens: int) -> int:
        return self.kv_bytes_per_token() * max(1, context_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_size": self.file_size,
            "architecture": self.architecture,
            "name": self.name,
            "block_count": self.block_count,
            "embedding_length": self.embedding_length,
            "head_count": self.head_count,
            "head_count_kv": self.head_count_kv,
            "context_length": self.context_length,
            "kv_bytes_per_token": self.kv_bytes_per_token(),
            "error": self.error,
        }


class _Reader:
    def __init__(self, handle: Any, limit: int = 64 * 1024 * 1024) -> None:
        self.handle = handle
        self.consumed = 0
        self.limit = limit

    def read(self, size: int) -> bytes:
        self.consumed += size
        if self.consumed > self.limit:
            raise ValueError("GGUF metadata is larger than expected")
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError("Unexpected end of GGUF file")
        return data

    def string(self) -> str:
        (length,) = struct.unpack("<Q", self.read(8))
        if length > 1_000_000:
            raise ValueError("GGUF string is implausibly long")
        return self.read(length).decode("utf-8", errors="replace")

    def value(self, kind: int) -> Any:
        if kind == _STRING:
            return self.string()
        if kind == _ARRAY:
            (inner,) = struct.unpack("<I", self.read(4))
            (count,) = struct.unpack("<Q", self.read(8))
            if count > 1_000_000:
                raise ValueError("GGUF array is implausibly long")
            return [self.value(inner) for _ in range(count)]
        fmt = _SCALAR_FORMATS.get(kind)
        if fmt is None:
            raise ValueError(f"Unknown GGUF value type {kind}")
        code, size = fmt
        return struct.unpack(code, self.read(size))[0]


def read_gguf(path: str | Path, *, max_kv: int = 200_000) -> GgufInfo:
    """Read model metadata without loading tensors."""
    target = Path(path)
    info = GgufInfo(path=target, file_size=target.stat().st_size)
    try:
        with target.open("rb") as handle:
            reader = _Reader(handle)
            magic = reader.read(4)
            if magic != GGUF_MAGIC:
                raise ValueError("Not a GGUF file")
            (version,) = struct.unpack("<I", reader.read(4))
            if version not in (2, 3):
                raise ValueError(f"Unsupported GGUF version {version}")
            reader.read(8)  # tensor count
            (kv_count,) = struct.unpack("<Q", reader.read(8))
            if kv_count > max_kv:
                raise ValueError("GGUF metadata count is implausibly large")

            raw: dict[str, Any] = {}
            for _ in range(kv_count):
                key = reader.string()
                (kind,) = struct.unpack("<I", reader.read(4))
                value = reader.value(kind)
                if key.startswith("general.") or any(
                    key.endswith(f".{wanted}") for wanted in WANTED_KEYS
                ):
                    raw[key] = value
    except (OSError, ValueError, struct.error) as exc:
        info.error = str(exc)
        return info

    info.metadata = raw
    info.architecture = str(raw.get("general.architecture") or "")
    info.name = str(raw.get("general.name") or "")
    prefix = f"{info.architecture}."

    def get(key: str, default: Any = None) -> Any:
        return raw.get(f"{prefix}{key}", raw.get(key, default))

    info.block_count = int(get("block_count") or 0)
    info.embedding_length = int(get("embedding_length") or 0)
    info.head_count = int(get("attention.head_count") or 0)
    info.key_length = int(get("attention.key_length") or 0)
    info.value_length = int(get("attention.value_length") or 0)
    info.context_length = int(get("context_length") or 0)

    kv_heads = get("attention.head_count_kv")
    if isinstance(kv_heads, list):
        info.head_count_kv = [int(item) for item in kv_heads if isinstance(item, (int, float))]
    elif isinstance(kv_heads, (int, float)):
        info.head_count_kv = [int(kv_heads)]

    if not info.block_count or not info.head_count:
        info.error = info.error or "GGUF metadata is missing layer/head counts"
    return info
