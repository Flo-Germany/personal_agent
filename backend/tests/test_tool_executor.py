"""Tests for tool schema shape and validated dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from settings import ToolSettings
from tools.executor import ToolExecutor
from tools.schemas import TOOL_SCHEMAS


def test_schemas_use_the_openai_function_format() -> None:
    names = set()

    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert len(function["description"]) > 40
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["additionalProperties"] is False
        assert isinstance(function["parameters"]["required"], list)
        names.add(function["name"])

    assert names == {
        "start_program",
        "open_file",
        "create_folder",
        "create_file",
        "copy_file",
        "move_file_or_folder",
        "delete_file_or_folder",
        "unzip_file",
        "read_file_content",
        "search_system",
        "semantic_image_search",
        "get_open_windows",
        "read_window_content",
        "analyze_screen",
    }


def test_start_program_schema_accepts_any_program() -> None:
    schema = next(
        item for item in TOOL_SCHEMAS if item["function"]["name"] == "start_program"
    )
    parameter = schema["function"]["parameters"]["properties"]["program_name"]

    assert parameter["type"] == "string"
    assert "enum" not in parameter
    assert "executable" in parameter["description"].lower()


def test_executor_rejects_unknown_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute("run_shell", {"command": "whoami"})

    assert result["ok"] is False
    assert "Unknown tool" in result["error"]


def test_executor_rejects_invalid_json(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute("create_folder", "{not json")

    assert result["ok"] is False
    assert "Invalid JSON" in result["error"]


def test_executor_rejects_unexpected_arguments(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute(
        "create_folder",
        {"path": str(tmp_path / "ok"), "sandbox": "C:\\Windows"},
    )

    assert result["ok"] is False
    assert "Invalid arguments" in result["error"]


def test_executor_runs_create_folder(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())
    target = tmp_path / "agent-output"

    result = executor.execute(
        "create_folder",
        json.dumps({"path": str(target)}),
    )

    assert result["ok"] is True
    assert target.is_dir()


def test_executor_requires_confirmation() -> None:
    executor = ToolExecutor(ToolSettings())

    assert executor.requires_confirmation("create_folder") is True
    assert executor.requires_confirmation("delete_file_or_folder") is True
    assert executor.requires_confirmation("start_program") is True
    assert executor.requires_confirmation("open_file") is True
    assert executor.requires_confirmation("read_file_content") is False
    assert executor.requires_confirmation("search_system") is False
    assert executor.requires_confirmation("get_open_windows") is False
    assert executor.requires_confirmation("read_window_content") is False
    assert executor.requires_confirmation("analyze_screen") is False


def test_executor_ignores_unknown_search_keys(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    executor = ToolExecutor(ToolSettings())

    result = executor.execute(
        "search_system",
        {
            "root": str(tmp_path),
            "size_desc": 0,
            "size_asc": 0,
            "none": 0,
        },
    )

    assert result["ok"] is True
    assert [item["name"] for item in result["results"]] == ["file.txt"]


def test_executor_passes_search_limits(tmp_path: Path) -> None:
    (tmp_path / "match.txt").write_text("x", encoding="utf-8")
    executor = ToolExecutor(ToolSettings(max_search_results=1))

    result = executor.execute("search_system", {"root": str(tmp_path), "name_contains": "match"})

    assert result["ok"] is True
    assert len(result["results"]) == 1
