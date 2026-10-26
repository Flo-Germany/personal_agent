"""Tests for tool schema shape and validated dispatch."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

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
        "propose_plan",
        "run_powershell_query",
        "run_powershell_action",
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
        "system_diagnosis",
        "get_open_windows",
        "read_window_content",
        "analyze_screen",
        "get_desktop_context",
        "analyze_screen_ui",
        "move_cursor_to",
        "click_element",
        "keyboard_type_text",
        "keyboard_press_keys",
    }


def test_powershell_action_schema_requires_a_description() -> None:
    schema = next(
        item
        for item in TOOL_SCHEMAS
        if item["function"]["name"] == "run_powershell_action"
    )

    assert schema["function"]["parameters"]["required"] == [
        "command",
        "description",
    ]
    assert "state-changing" in schema["function"]["description"]
    assert "approval" in schema["function"]["description"]


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


def test_propose_plan_records_clean_steps(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute(
        "propose_plan",
        {"steps": ["  Create  the folder ", "Write the file", ""]},
    )

    assert result["ok"] is True
    assert result["steps"] == ["Create the folder", "Write the file"]


def test_executor_rejects_invalid_json(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute("create_folder", "{not json")

    assert result["ok"] is False
    assert "Invalid JSON" in result["error"]


def test_executor_ignores_unexpected_arguments(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolSettings())
    target = tmp_path / "ok"

    result = executor.execute(
        "create_folder",
        {"path": str(target), "sandbox": "C:\\Windows"},
    )

    # Models sometimes add an extra descriptive key; ignore it instead of
    # failing the whole action.
    assert result["ok"] is True
    assert target.is_dir()


def test_executor_rejects_missing_required_arguments() -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute("read_file_content", {})

    assert result["ok"] is False
    assert "Missing required argument" in result["error"]


def test_executor_logs_structured_tool_execution(
    tmp_path: Path,
    caplog: Any,
) -> None:
    executor = ToolExecutor(ToolSettings())

    with caplog.at_level(logging.INFO, logger="tools.executor"):
        result = executor.execute(
            "create_folder",
            {"path": str(tmp_path / "logged")},
        )

    assert result["ok"] is True
    record = next(
        record
        for record in caplog.records
        if record.message.startswith("tool_execution")
    )
    assert getattr(record, "tool") == "create_folder"
    assert getattr(record, "ok") is True
    assert getattr(record, "duration_ms") >= 0


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

    assert executor.requires_confirmation("run_powershell_action") is True
    assert executor.requires_confirmation("run_powershell_query") is False
    assert executor.requires_confirmation("create_folder") is True
    assert executor.requires_confirmation("delete_file_or_folder") is True
    assert executor.requires_confirmation("start_program") is True
    assert executor.requires_confirmation("open_file") is True
    assert executor.requires_confirmation("read_file_content") is False
    assert executor.requires_confirmation("search_system") is False
    assert executor.requires_confirmation("semantic_image_search") is False
    assert executor.requires_confirmation("system_diagnosis") is False
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


def test_executor_maps_guessed_folder_flags(tmp_path: Path) -> None:
    """Models sometimes send folders_only/include_folders instead of kind."""
    archive = tmp_path / "Archive"
    archive.mkdir()
    (tmp_path / "archive.txt").write_text("x", encoding="utf-8")
    executor = ToolExecutor(ToolSettings())

    folders = executor.execute(
        "search_system",
        {"root": str(tmp_path), "name_contains": "archive", "folders_only": True},
    )
    assert [item["name"] for item in folders["results"]] == ["Archive"]

    files = executor.execute(
        "search_system",
        {"root": str(tmp_path), "name_contains": "archive", "include_folders": False},
    )
    assert [item["name"] for item in files["results"]] == ["archive.txt"]

    both = executor.execute(
        "search_system",
        {"root": str(tmp_path), "name_contains": "archive", "kind": "any"},
    )
    assert len(both["results"]) == 2


def test_executor_forwards_kind_and_depth(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "hit.txt").write_text("x", encoding="utf-8")
    executor = ToolExecutor(ToolSettings())

    shallow = executor.execute(
        "search_system",
        {"root": str(tmp_path), "name_contains": "hit", "max_depth": 1},
    )
    assert shallow["results"] == []

    matched = executor.execute(
        "search_system",
        {
            "root": str(tmp_path),
            "name_contains": "hit",
            "kind": "files",
            "max_depth": 3,
        },
    )
    assert [item["name"] for item in matched["results"]] == ["hit.txt"]
    assert matched["kind"] == "files"
