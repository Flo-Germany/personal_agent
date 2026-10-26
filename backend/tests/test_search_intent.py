"""The user's wording decides whether a search is for folders or files.

A small model may omit ``kind`` or mislabel a folder request. These tests pin
the deterministic correction so "suche nach allen Ordnern" can never come back
with a list of files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.chat import ChatService, _search_kind_hint
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import MemorySettings, OllamaSettings, Settings, ToolSettings
from tools import os_tools
from tools.executor import ToolExecutor


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        agent_name="TestBot",
        personality="Direct.",
        system_prompt="Answer briefly.",
        personality_file=tmp_path / "personality.md",
        ollama=OllamaSettings(model="qwen3-vl:8b-instruct", max_retries=0),
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
        tools=ToolSettings(sandbox_dir=tmp_path / "sandbox"),
    )


class RecordingTools:
    """Minimal stand-in that records the arguments a search received."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, name: str, arguments: Any, **_: Any) -> dict[str, Any]:
        self.executed.append((name, arguments))
        return {"ok": True, "message": "recorded", "results": []}

    def requires_confirmation(self, name: str) -> bool:
        return False


def make_service(
    tmp_path: Path,
    tools: Any | None = None,
) -> tuple[ChatService, ConversationStore]:
    settings = make_settings(tmp_path)
    store = ConversationStore()
    service = ChatService(
        settings,
        store,
        KnowledgeBase(settings.memory.resolved_knowledge_path()),
        tools=tools or RecordingTools(),  # type: ignore[arg-type]
        client=object(),
    )
    return service, store


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # The exact report from the app: a German folder request.
        ("suche nach allen ordnern auf dem desktop", "folders"),
        ("Suche nach allen Ordnern auf dem Desktop!", "folders"),
        ("Ordner auf dem Desktop", "folders"),
        ("welche ordner gibt es auf dem desktop", "folders"),
        ("Zeige mir alle Verzeichnisse", "folders"),
        ("Search for all folders on my desktop", "folders"),
        ("list my folders", "folders"),
        ("how many directories are there", "folders"),
        ("show me all the folders", "folders"),
        # Files stay files.
        ("suche nach allen dateien auf dem desktop", "files"),
        ("find all files in the Downloads folder", "files"),
        ("find the newest log in the Downloads folder", None),
        ("search C:/Projects for report.pdf", "files"),
        # Both, and ambiguous.
        ("Zeige mir alle Ordner und Dateien auf dem Desktop", "any"),
        ("open the folder Downloads", None),
        ("was ist im Ordner Downloads", None),
        # A path/output extension must not turn a folder request into files.
        (
            "Somewhere under C:/tmp there is a folder named 'Archive 2026'. "
            "Write its path to C:/tmp/found.txt.",
            "folders",
        ),
        (
            "Suche nach allen Ordnern und schreibe ihre Namen in "
            "C:/tmp/ordner.txt.",
            "folders",
        ),
        (
            "find the report.pdf in the Downloads folder",
            "files",
        ),
    ],
)
def test_search_kind_hint(message: str, expected: str | None) -> None:
    assert _search_kind_hint(message) == expected


def test_german_folder_request_forces_kind_folders(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "suche nach allen ordnern auf dem desktop")

    service._execute_tool(
        "search_system",
        {"root": str(tmp_path)},
        conversation_id,
    )

    name, arguments = tools.executed[-1]
    assert name == "search_system"
    assert arguments["kind"] == "folders"
    assert arguments["root"] == str(tmp_path)


def test_model_kind_is_corrected_when_the_user_asked_for_folders(
    tmp_path: Path,
) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "Search for all folders on my desktop")

    service._execute_tool(
        "search_system",
        '{"root": "C:\\\\Users", "kind": "files"}',
        conversation_id,
    )

    _, arguments = tools.executed[-1]
    assert isinstance(arguments, dict)
    assert arguments["kind"] == "folders"


def test_extension_filter_wins_over_folder_wording(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "alle ordner als pdf")

    service._execute_tool(
        "search_system",
        {"root": str(tmp_path), "extension": "pdf", "kind": "folders"},
        conversation_id,
    )

    _, arguments = tools.executed[-1]
    assert arguments["kind"] == "files"


def test_ambiguous_wording_leaves_the_tool_arguments_alone(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "open the folder Downloads")

    service._execute_tool("search_system", {"root": str(tmp_path)}, conversation_id)

    _, arguments = tools.executed[-1]
    assert "kind" not in arguments


def test_desktop_request_is_limited_to_the_top_level(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "suche nach allen ordnern auf dem desktop")

    service._execute_tool(
        "search_system",
        {"root": "desktop", "kind": "folders", "recursive": True},
        conversation_id,
    )

    _, arguments = tools.executed[-1]
    assert arguments["recursive"] is False
    assert arguments["kind"] == "folders"


def test_deep_search_wording_keeps_recursion(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "find every folder on the desktop inside all subfolders",
    )

    service._execute_tool("search_system", {"root": "desktop"}, conversation_id)

    _, arguments = tools.executed[-1]
    assert "recursive" not in arguments


def test_scope_guard_does_not_touch_explicit_deep_searches(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "list folders in my project")

    service._execute_tool(
        "search_system",
        {"root": str(tmp_path), "recursive": True},
        conversation_id,
    )

    _, arguments = tools.executed[-1]
    assert arguments["recursive"] is True


def test_end_to_end_german_folder_search_returns_only_folders(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "desktop"
    workspace.mkdir()
    (workspace / "Projekte").mkdir()
    (workspace / "Archiv").mkdir()
    (workspace / "notiz.txt").write_text("x", encoding="utf-8")
    (workspace / "link.url").write_text("x", encoding="utf-8")

    settings = make_settings(tmp_path)
    store = ConversationStore()
    service = ChatService(
        settings,
        store,
        KnowledgeBase(settings.memory.resolved_knowledge_path()),
        tools=ToolExecutor(settings.tools),
        client=object(),
    )
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "suche nach allen ordnern auf dem desktop",
    )

    result = service._execute_tool(
        "search_system",
        {"root": str(workspace), "kind": "any"},
        conversation_id,
    )

    assert result["kind"] == "folders"
    assert {item["name"] for item in result["results"]} == {"Projekte", "Archiv"}
    assert all(item["type"] == "folder" for item in result["results"])
    assert result["summary"]["files"] == 0
    # The tree is built from folder nodes only.
    root = result["tree"][0]
    assert all(child["type"] == "folder" for child in root["children"])


def test_english_file_search_still_returns_files(tmp_path: Path) -> None:
    workspace = tmp_path / "downloads"
    workspace.mkdir()
    (workspace / "report.pdf").write_text("x", encoding="utf-8")
    (workspace / "reports").mkdir()

    settings = make_settings(tmp_path)
    store = ConversationStore()
    service = ChatService(
        settings,
        store,
        KnowledgeBase(settings.memory.resolved_knowledge_path()),
        tools=ToolExecutor(settings.tools),
        client=object(),
    )
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "find all files in the Downloads folder named report",
    )

    result = service._execute_tool(
        "search_system",
        {"root": str(workspace), "name_contains": "report"},
        conversation_id,
    )

    assert result["kind"] == "files"
    assert [item["name"] for item in result["results"]] == ["report.pdf"]


def test_search_intent_does_not_change_other_tools(tmp_path: Path) -> None:
    tools = RecordingTools()
    service, store = make_service(tmp_path, tools)
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "suche nach allen ordnern")

    service._execute_tool("get_open_windows", {}, conversation_id)

    assert tools.executed[-1] == ("get_open_windows", {})


def test_os_tools_default_returns_folders_too(tmp_path: Path) -> None:
    (tmp_path / "Ordner").mkdir()
    (tmp_path / "datei.txt").write_text("x", encoding="utf-8")

    result = os_tools.search_system(str(tmp_path))

    types = {item["type"] for item in result["results"]}
    assert types == {"folder", "file"}
