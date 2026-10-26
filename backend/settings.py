"""Application settings loaded from config/settings.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_root() -> Path:
    """Return the writable folder for settings, memory, and generated files.

    In a source checkout this is the repository root. The packaged desktop app
    lives in a read-only install folder, so its launcher sets
    ``PERSONAL_AGENT_DATA_DIR`` to a per-user location such as
    ``%LOCALAPPDATA%\\com.personalagent.desktop\\data``.
    """
    raw = os.environ.get("PERSONAL_AGENT_DATA_DIR", "").strip()
    if not raw:
        return PROJECT_ROOT

    try:
        path = Path(os.path.expandvars(raw)).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()
    except OSError:
        # Never fail the whole backend because the data folder is unavailable;
        # the source checkout remains a usable fallback for development.
        return PROJECT_ROOT


DATA_ROOT = _resolve_data_root()


def _default_config_path() -> Path:
    """Prefer the user-editable config in the data folder when it exists."""
    candidates = (
        DATA_ROOT / "config" / "settings.json",
        PROJECT_ROOT / "config" / "settings.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


DEFAULT_CONFIG_PATH = _default_config_path()

_DEFAULT_ENV = {
    "OLLAMA_FLASH_ATTENTION": "1",
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "TRANSFORMERS_NO_ADVISORY_WARNINGS": "1",
}


def resolve_data_path(path: Path) -> Path:
    """Resolve a possibly relative data path against the writable data root."""
    if path.is_absolute():
        return path
    return DATA_ROOT / path


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


_load_dotenv(DATA_ROOT / ".env")
if DATA_ROOT != PROJECT_ROOT:
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
    # Ollama unloads an idle model after this many seconds. Keeping it warm
    # avoids an 8+ second cold load and preserves the prompt cache, which is
    # by far the largest latency win. Chat and screen analysis use the same
    # model, so there is no second model competing for VRAM. Set 0 to restore
    # immediate unloading after every request.
    keep_alive_seconds: int = Field(default=1800, ge=0, le=86_400)
    # Preload the model and the stable prompt prefix when the backend starts.
    # The first real message then only evaluates its new user text.
    warmup_on_startup: bool = False
    # Reasoning models stream a hidden "thinking" phase before the answer.
    # "off" never asks for it, "auto" enables it when the model advertises the
    # capability, and "low"/"medium"/"high" request that thinking budget.
    # Non-thinking models ignore this (the field is not sent at all).
    thinking: Literal["off", "auto", "low", "medium", "high"] = "auto"
    # Thinking is only worth its latency on turns that need a plan. Turns that
    # are short, purely conversational, or a direct tool call skip it even when
    # the model supports it. Set to 0 to think on every turn.
    thinking_min_chars: int = Field(default=160, ge=0, le=10_000)
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
    # 0 means no hard limit: the agent may take as many steps as the task
    # requires. A positive value is an optional safety cap.
    max_iterations: int = Field(default=0, ge=0)
    # 0 means no hard limit on actions after one approval.
    max_auto_approved_actions: int = Field(default=0, ge=0)
    approval_ttl_seconds: float = Field(default=600.0, gt=0)
    # Successful recoveries and multi-step workflows become durable facts in
    # memory/knowledge_base.md.
    self_improvement_enabled: bool = True
    self_improvement_workflow_min_tools: int = Field(default=3, ge=1, le=20)
    # Send only the tool schemas the request plausibly needs. The full set is
    # ~6k tokens; a focused set keeps prompt evaluation and generation fast on
    # a local model. Set to false to always send every tool schema.
    dynamic_tools: bool = True


class MemorySettings(BaseModel):
    # Chat history stays in memory; only the knowledge base touches disk.
    knowledge_base_path: Path = Path("memory/knowledge_base.md")
    max_conversations: int = Field(default=100, ge=1)
    learn_from_chats: bool = True

    def resolved_knowledge_path(self) -> Path:
        return resolve_data_path(self.knowledge_base_path)


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
    min_confidence_z: float = Field(default=3.0, ge=0.0, le=10.0)
    min_result_z: float = Field(default=2.4, ge=0.0, le=10.0)
    dedupe_threshold: float = Field(default=0.995, ge=0.0, le=1.0)
    screen_model: str = "qwen3-vl:8b-instruct"
    # Grounding-only model for Set-of-Mark element selection. Empty means the
    # screen model is used. A small dedicated model shortens each GUI step
    # without touching the chat planner, and can run next to it in VRAM.
    ground_model: str = ""
    ground_context_window: int = Field(default=8192, ge=2048, le=65536)
    screen_timeout_seconds: float = Field(default=180.0, gt=0.0, le=600.0)
    screen_max_width: int = Field(default=1600, ge=320, le=3840)
    screen_max_tokens: int = Field(default=700, ge=64, le=8192)

    def resolved_index_path(self) -> Path:
        return resolve_data_path(self.index_path)

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


class AutomationSettings(BaseModel):
    """GUI control: Set-of-Mark detection, smooth pointer, physical killswitch."""

    enabled: bool = True
    move_duration_seconds: float = Field(default=0.4, ge=0.1, le=5.0)
    highlight_seconds: float = Field(default=0.3, ge=0.0, le=2.0)
    arm_delay_seconds: float = Field(default=0.4, ge=0.0, le=2.0)
    move_threshold_pixels: int = Field(default=4, ge=1, le=50)
    max_elements: int = Field(default=140, ge=5, le=400)
    # The annotated screenshot labels at most this many controls (priority:
    # input fields first, then lists/tabs, then buttons). The model still gets
    # the full element list, and clicks work by name/ID even when an element is
    # not drawn in the image.
    max_annotated_elements: int = Field(default=60, ge=5, le=200)
    min_element_size: int = Field(default=8, ge=2, le=100)
    max_element_scan: int = Field(default=3000, ge=100, le=20000)
    max_element_depth: int = Field(default=20, ge=2, le=60)
    type_interval_seconds: float = Field(default=0.02, ge=0.0, le=0.2)
    # How long to watch the desktop for a window change after an action. A
    # short value keeps actions snappy; apps that open windows slowly can use a
    # larger one.
    action_settle_seconds: float = Field(default=0.9, ge=0.0, le=5.0)
    # Require keyboard input to come from the analyzed window that is actually
    # foreground. Disable only in unit tests.
    verify_foreground: bool = True
    screenshot_dir: Path = Path("memory/screenshots")
    screenshot_max_width: int = Field(default=1600, ge=640, le=3840)
    # Keep only the newest annotated snapshots so a long GUI task does not
    # accumulate hundreds of megabytes in memory/screenshots.
    screenshot_keep: int = Field(default=200, ge=10, le=5000)
    visual_fallback: bool = True
    # If UIA exposes fewer than this many verified controls, use the strict OCR
    # fallback. The default of 1 means "only when UIA exposes nothing usable";
    # raise it for custom apps that expose a partial tree.
    uia_min_elements: int = Field(default=1, ge=1, le=40)
    ocr_min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    ocr_max_words: int = Field(default=4, ge=1, le=12)
    ocr_max_height: int = Field(default=80, ge=20, le=300)
    # Skip the grounding model when an element name already matches the intent
    # unambiguously. This saves a VLM round per GUI step and avoids wrong picks
    # on well-labeled native controls.
    local_grounding: bool = True
    # Reuse the previous snapshot when the window pixels did not change (for
    # example after typing into a field). Recomputed from a fingerprint of the
    # live screen, so stale IDs cannot silently be used.
    snapshot_reuse_seconds: float = Field(default=20.0, ge=0.0, le=300.0)
    snapshot_reuse_threshold: float = Field(default=0.02, ge=0.0, le=0.5)
    # Re-check the live window/process/rect/focus immediately before an action.
    pre_action_validation: bool = True
    validation_tolerance_px: int = Field(default=8, ge=1, le=100)

    def resolved_screenshot_dir(self) -> Path:
        return resolve_data_path(self.screenshot_dir)


class EngineSettings(BaseModel):
    """Phase 22 native engine. Ollama stays selectable during migration."""

    # llama.cpp speaks the same OpenAI subset as the Ollama adapter, so the
    # agent loop is unchanged; this switch only selects the transport.
    type: Literal["ollama", "llamacpp"] = "ollama"
    # Leave auto planning on unless you want to pin a layer count.
    gpu_layers: int | None = Field(default=None, ge=0, le=512)
    vram_override_gb: float | None = Field(default=None, gt=0, le=512)
    # Keep room for the desktop and driver before filling VRAM.
    vram_reserve_gb: float = Field(default=1.5, ge=0, le=32)
    text_model: str = "qwen3-0.6b-q4"
    vision_model: str = "qwen3-vl-2b-q4"
    # Empty means the model catalog context (16k for vision, 32k for text).
    context_window: int | None = Field(default=None, ge=2048, le=131072)
    start_timeout_seconds: float = Field(default=180.0, gt=0, le=900)


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
    automation: AutomationSettings = Field(default_factory=AutomationSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)

    def resolved_personality_path(self) -> Path:
        if self.personality_file.is_absolute():
            return self.personality_file
        # The launcher copies the bundled personality into the data folder so
        # users can edit it without touching the installed files.
        data_candidate = DATA_ROOT / self.personality_file
        if data_candidate.exists():
            return data_candidate
        return PROJECT_ROOT / self.personality_file


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults for missing fields."""
    config_path = path or DEFAULT_CONFIG_PATH

    if config_path.exists():
        # utf-8-sig tolerates a BOM added by Windows editors.
        with config_path.open(encoding="utf-8-sig") as handle:
            return Settings.model_validate(json.load(handle))

    return Settings()
