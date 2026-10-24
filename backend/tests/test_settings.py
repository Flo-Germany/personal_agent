"""Tests for local .env loading."""

from __future__ import annotations

import os
from pathlib import Path

from settings import _load_dotenv


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
