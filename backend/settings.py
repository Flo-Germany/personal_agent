"""Application settings loaded from config/settings.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"

_DEFAULT_ENV = {
    "OLLAMA_FLASH_ATTENTION": "1",
}


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs without adding a python-dotenv dependency.

    Existing process environment variables win, so explicit shell/OS settings
    are never overridden by the local file.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")
for _env_key, _env_value in _DEFAULT_ENV.items():
    os.environ.setdefault(_env_key, _env_value)


class BackendSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class OllamaSettings(BaseModel):
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3-vl:8b-instruct"
    timeout_seconds: float = Field(default=120.0, gt=0)
    temperature: float = Field(default=0.5, ge=0, le=2)
    max_retries: int = Field(default=1, ge=0, le=5)
    # 0 unloads the model immediately after every request, keeping VRAM free
    # for the vision/text model switch. Set a positive value to keep it warm.
    keep_alive_seconds: int = Field(default=0, ge=0, le=86_400)
    # Recommended model quantization; avoid Q8/FP16 variants on limited VRAM.
    model_quantization: str = Field(default="q4_K_M", min_length=1, max_length=40)
    # Ollama defaults to a small context window unless told otherwise. The
    # identity, personality, knowledge base, and tool schemas together need
    # considerably more than the default, so set it explicitly.
    context_window: int = Field(default=16384, ge=2048, le=131072)
    # Some model templates (for example hermes3) silently drop the system
    # message when tool schemas are supplied. "auto" folds the system prompt
    # into the latest user message for those models; "system" always uses the
    # system role, and "user" always folds it into the latest user message.
    system_prompt_mode: Literal["auto", "system", "user"] = "auto"

    def quantization_warning(self) -> str | None:
        """Warn when the model tag advertises a large quantization."""
        model = self.model.casefold()
        markers = ("fp16", "f16", "q8", "q6", "q5")
        if not any(marker in model for marker in markers):
            return None
        return (
            f"Model '{self.model}' appears to use a large quantization. "
            f"Prefer a '{self.model_quantization}' variant to reduce VRAM use."
        )


class AgentSettings(BaseModel):
    max_iterations: int = Field(default=5, ge=1)
    approval_ttl_seconds: float = Field(default=600.0, gt=0)


class MemorySettings(BaseModel):
    # Chat history stays in memory; only the knowledge base touches disk.
    knowledge_base_path: Path = Path("memory/knowledge_base.md")
    max_conversations: int = Field(default=100, ge=1)
    learn_from_chats: bool = True

    def resolved_knowledge_path(self) -> Path:
        if self.knowledge_base_path.is_absolute():
            return self.knowledge_base_path
        return PROJECT_ROOT / self.knowledge_base_path


class ToolSettings(BaseModel):
    max_read_bytes: int = Field(default=1_000_000, ge=1)
    max_search_results: int = Field(default=50, ge=1, le=200)
    max_search_files: int = Field(default=20_000, ge=1, le=200_000)
    max_search_seconds: float = Field(default=30.0, gt=0, le=300)
    max_unzip_bytes: int = Field(default=200_000_000, ge=1)
    redact_secrets: bool = True
    powershell_timeout_seconds: float = Field(default=15.0, ge=0.5, le=300.0)
    powershell_max_output_chars: int = Field(
        default=12_000,
        ge=1_000,
        le=100_000,
    )


class VisionSettings(BaseModel):
    """Global CLIP image index. Disabled by default so tests stay offline."""

    enabled: bool = False
    embed_model: str = "clip-ViT-B-32"
    index_path: Path = Path("memory/vision_index.sqlite3")
    scan_roots: list[str] = Field(default_factory=lambda: [str(Path.home())])
    skip_directory_names: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    batch_size: int = Field(default=8, ge=1, le=32)
    scan_interval_seconds: float = Field(default=900.0, ge=30, le=86_400)
    max_images: int = Field(default=100_000, ge=100, le=1_000_000)
    max_file_mb: int = Field(default=50, ge=1, le=500)
    min_confidence_z: float = Field(default=3.9, ge=0.0, le=10.0)
    min_result_z: float = Field(default=3.5, ge=0.0, le=10.0)
    dedupe_threshold: float = Field(default=0.995, ge=0.0, le=1.0)
    screen_model: str = "qwen3-vl:8b-instruct"
    screen_timeout_seconds: float = Field(default=180.0, gt=0.0, le=600.0)
    screen_max_width: int = Field(default=1600, ge=320, le=3840)
    screen_max_tokens: int = Field(default=700, ge=64, le=8192)

    def resolved_index_path(self) -> Path:
        if self.index_path.is_absolute():
            return self.index_path
        return PROJECT_ROOT / self.index_path

    def resolved_scan_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()

        for raw in self.scan_roots:
            try:
                path = Path(os.path.expandvars(raw)).expanduser()
            except (OSError, ValueError):
                continue

            key = str(path).casefold()
            if key in seen or not path.is_dir():
                continue

            seen.add(key)
            roots.append(path)

        return roots


class Settings(BaseModel):
    agent_name: str = Field(default="Nova", min_length=1, max_length=80)
    # Personality lives in a dedicated, user-editable Markdown file. The inline
    # field remains as a fallback for older configs or when the file is missing.
    personality: str = ""
    personality_file: Path = Path("config/personality.md")
    system_prompt: str = ""

    backend: BackendSettings = Field(default_factory=BackendSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)

    def resolved_personality_path(self) -> Path:
        if self.personality_file.is_absolute():
            return self.personality_file
        return PROJECT_ROOT / self.personality_file


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults for missing fields."""
    config_path = path or DEFAULT_CONFIG_PATH

    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            return Settings.model_validate(json.load(handle))

    return Settings()
