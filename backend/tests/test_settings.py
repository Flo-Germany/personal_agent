"""Tests for local .env loading and the packaged app's writable data folder."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from settings import DATA_ROOT, PROJECT_ROOT, _load_dotenv, resolve_data_path


def test_dotenv_loads_values_without_overriding_os(
    monkeypatch,
    tmp_path: Path,
) -> None:
    key = "PERSONAL_AGENT_TEST_ENV"
    path = tmp_path / ".env"
    path.write_text(f'{key}="from-file"\n', encoding="utf-8")

    monkeypatch.delenv(key, raising=False)
    _load_dotenv(path)
    assert os.environ[key] == "from-file"

    monkeypatch.setenv(key, "from-os")
    _load_dotenv(path)
    assert os.environ[key] == "from-os"


def test_resolve_data_path_keeps_absolute_paths(tmp_path: Path) -> None:
    target = tmp_path / "memory" / "knowledge_base.md"

    assert resolve_data_path(target) == target


def test_resolve_data_path_anchors_relative_paths() -> None:
    assert resolve_data_path(Path("memory/knowledge_base.md")) == (
        DATA_ROOT / "memory" / "knowledge_base.md"
    )


def test_data_dir_override_moves_writable_paths(tmp_path: Path) -> None:
    """The packaged launcher sets PERSONAL_AGENT_DATA_DIR for a writable root."""
    data_dir = tmp_path / "app-data"
    script = "\n".join(
        [
            "from settings import DATA_ROOT, DEFAULT_CONFIG_PATH, load_settings",
            "settings = load_settings()",
            "print(DATA_ROOT)",
            "print(DEFAULT_CONFIG_PATH)",
            "print(settings.memory.resolved_knowledge_path())",
            "print(settings.vision.resolved_index_path())",
            "print(settings.automation.resolved_screenshot_dir())",
            "print(settings.resolved_personality_path())",
        ]
    )

    env = os.environ.copy()
    env["PERSONAL_AGENT_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT / "backend",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[0] == str(data_dir)
    # Until the launcher seeds the data folder, bundled defaults are read from
    # the install folder so the first start still works.
    assert lines[1] == str(PROJECT_ROOT / "config" / "settings.json")
    assert lines[2] == str(data_dir / "memory" / "knowledge_base.md")
    assert lines[3] == str(data_dir / "memory" / "vision_index.sqlite3")
    assert lines[4] == str(data_dir / "memory" / "screenshots")
    assert lines[5] == str(PROJECT_ROOT / "config" / "personality.md")
