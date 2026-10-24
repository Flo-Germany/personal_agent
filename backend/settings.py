"""Application settings loaded from config/settings.json."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"


class BackendSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class OllamaSettings(BaseModel):
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "hermes3"
    timeout_seconds: float = Field(default=120.0, gt=0)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_retries: int = Field(default=1, ge=0, le=5)


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


class Settings(BaseModel):
    agent_name: str = Field(default="Nova", min_length=1, max_length=80)
    personality: str = ""
    system_prompt: str = ""

    backend: BackendSettings = Field(default_factory=BackendSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults for missing fields."""
    config_path = path or DEFAULT_CONFIG_PATH

    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            return Settings.model_validate(json.load(handle))

    return Settings()
