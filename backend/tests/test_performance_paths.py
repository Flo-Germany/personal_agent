"""Tests for the faster GUI/model paths: profiles, compaction, grounding, reuse."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PIL import Image

from agent.chat import _model_facing_tool_result, _profile_tool_names
from settings import AutomationSettings, ToolSettings
from tools.executor import ToolExecutor
from tools.gui_automation import _is_transient_window, _local_grounding
from tools.som_vision import (
    DisplayBounds,
    SOMSnapshot,
    SOMParser,
    UIElement,
    _screen_fingerprint,
)


def test_dynamic_tool_profile_keeps_gui_and_adds_hinted_tools() -> None:
    gui = _profile_tool_names("click the Save button in Notepad")
    assert {"click_element", "analyze_screen_ui", "keyboard_type_text"} <= gui
    assert "system_diagnosis" not in gui
    assert "unzip_file" not in gui

    files = _profile_tool_names("move the files from Downloads to the Desktop")
    assert "move_file_or_folder" in files
    assert "create_folder" in files

    diagnosis = _profile_tool_names("Run a system diagnosis of my PC")
    assert "system_diagnosis" in diagnosis

    images = _profile_tool_names("find pictures of a red car")
    assert "semantic_image_search" in images


def test_som_tool_result_is_compacted_for_the_model() -> None:
    element = {
        "id": 7,
        "name": "New Email",
        "type": "Button",
        "center": [160, 130],
        "rect": [100, 100, 220, 160],
        "source": "uia",
        "enabled": True,
    }
    result = {
        "ok": True,
        "snapshot_id": "som_123",
        "window_title": "Outlook",
        "display": {"index": 1},
        "process_name": "OUTLOOK.EXE",
        "context": {
            "active": {"title": "Outlook", "process_name": "OUTLOOK.EXE"},
            "windows": [{"title": "Outlook"}],
        },
        "screenshot": r"C:\shots\som.png",
        "files": [r"C:\shots\som.png"],
        "elements": [element],
        "coordinate_map": {
            "7": {"x": 160, "y": 130, "name": "New Email", "type": "Button"}
        },
        "suggested_id": 7,
        "suggested_element": element,
        "confidence": 0.95,
        "grounding": "local",
        "model_answer": None,
        "message": "Detected 1 element.",
        "next_step": "Click it.",
    }

    stored = json.loads(_model_facing_tool_result("analyze_screen_ui", result))

    assert stored["snapshot_id"] == "som_123"
    assert stored["suggested_id"] == 7
    assert stored["grounding"] == "local"
    assert "coordinate_map" not in stored
    assert "screenshot" not in stored
    assert stored["elements"] == [
        {"id": 7, "name": "New Email", "type": "Button", "center": [160, 130]}
    ]


def make_elements() -> list[UIElement]:
    return [
        UIElement(1, "New Email", "Button", "new", 0, 0, 10, 10),
        UIElement(2, "To", "Edit", "to", 0, 0, 10, 10),
        UIElement(3, "Subject", "Edit", "subject", 0, 0, 10, 10),
        UIElement(4, "Send", "Button", "send", 0, 0, 10, 10),
        UIElement(
            5,
            "Explorer (Ctrl+Shift+E)",
            "TabItem",
            "explorer",
            0,
            0,
            10,
            10,
        ),
        UIElement(6, "visual element", "Visual", "", 0, 0, 10, 10),
        UIElement(7, "Save", "Button", "save", 0, 0, 10, 10),
        UIElement(8, "Save As", "Button", "saveas", 0, 0, 10, 10),
    ]


def test_local_grounding_matches_names_without_a_vision_call() -> None:
    elements = make_elements()

    assert _local_grounding("click the New Email button", elements)["id"] == 1
    assert _local_grounding("type into the To field", elements)["id"] == 2
    assert _local_grounding("open the Explorer sidebar", elements)["id"] == 5
    assert _local_grounding("press Send", elements)["id"] == 4
    assert _local_grounding("click Save", elements)["id"] == 7

    # Vague requests and unlabeled visual regions still go to the model.
    assert _local_grounding("click the button", elements) is None
    assert _local_grounding("click the thing", elements) is None


def test_local_grounding_refuses_equally_good_candidates() -> None:
    elements = [
        UIElement(1, "Send", "Button", "a", 0, 0, 10, 10),
        UIElement(2, "Send", "Button", "b", 0, 0, 10, 10),
    ]
    assert _local_grounding("click Send", elements) is None


def test_transient_windows_are_not_app_targets() -> None:
    assert _is_transient_window("Pop-upHost")
    assert _is_transient_window("Tooltip")
    assert _is_transient_window("")
    assert not _is_transient_window("Untitled - Message (HTML)")
    assert not _is_transient_window("C:\\Temp - File Explorer")


def test_start_program_uses_an_already_running_instance() -> None:
    calls: list[tuple[str, object]] = []

    class FakeAutomation:
        def app_windows(self, program_name: str) -> list[str]:
            return ["Compa Browser Start - Google Chrome"]

        def prefer_window(self, title: str) -> None:
            calls.append(("prefer", title))

        def visible_window_titles(self) -> set[str]:
            return set()

        def note_launched_program(self, *args: object, **kwargs: object) -> None:
            calls.append(("launch", args))

        def analyze(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def move(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def click(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def type_text(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def press_keys(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {"ok": True}

        def get_context(self) -> dict[str, object]:
            return {"ok": True}

        def reset(self) -> None:
            return None

        def status(self) -> dict[str, object]:
            return {"ok": True}

    executor = ToolExecutor(
        ToolSettings(),
        automation_service=FakeAutomation(),  # type: ignore[arg-type]
    )

    result = executor.execute("start_program", {"program_name": "chrome"})

    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["windows"] == ["Compa Browser Start - Google Chrome"]
    assert calls == [("prefer", "Compa Browser Start - Google Chrome")]


def test_large_file_content_is_capped_for_the_model() -> None:
    body = "x" * 60000
    result = {
        "ok": True,
        "message": "Read big.md.",
        "path": r"C:\big.md",
        "size": len(body),
        "content": body,
    }

    stored = json.loads(_model_facing_tool_result("read_file_content", result))

    assert stored["truncated"] is True
    assert len(stored["content"]) < 13000
    assert "Truncated" in stored["content"]
    # The UI payload keeps the complete file.
    assert result["content"] == body


def test_snapshot_reuse_requires_identical_pixels(tmp_path: Path) -> None:
    settings = AutomationSettings(
        screenshot_dir=tmp_path,
        snapshot_reuse_seconds=20,
    )
    parser = SOMParser(settings)
    screenshot = tmp_path / "som.png"
    Image.new("RGB", (4, 4), (0, 0, 0)).save(screenshot)
    snapshot = SOMSnapshot(
        snapshot_id="som_reuse",
        elements=(),
        display=DisplayBounds(1, 0, 0, 100, 100, True),
        window_title="Test window",
        screenshot=str(screenshot),
        image_width=100,
        image_height=100,
        created_at=time.time(),
    )
    parser.store.put(snapshot)
    same = Image.new("RGB", (100, 100), (0, 0, 0))
    changed = Image.new("RGB", (100, 100), (255, 255, 255))
    parser._remember(
        snapshot,
        fingerprint=_screen_fingerprint(same),
        hwnd=1,
        display_index=1,
        rect=(0, 0, 100, 100),
        foreground=99,
        title="Test window",
    )

    assert (
        parser._reusable(
            image=same,
            hwnd=1,
            display_index=1,
            rect=(0, 0, 100, 100),
            foreground=99,
            title="Test window",
        )
        is not None
    )
    assert (
        parser._reusable(
            image=changed,
            hwnd=1,
            display_index=1,
            rect=(0, 0, 100, 100),
            foreground=99,
            title="Test window",
        )
        is None
    )
    assert (
        parser._reusable(
            image=same,
            hwnd=2,
            display_index=1,
            rect=(0, 0, 100, 100),
            foreground=99,
            title="Test window",
        )
        is None
    )
    assert (
        parser._reusable(
            image=same,
            hwnd=1,
            display_index=2,
            rect=(0, 0, 100, 100),
            foreground=99,
            title="Test window",
        )
        is None
    )
    # A popup that took focus invalidates the cached snapshot even when the
    # captured window looks identical.
    assert (
        parser._reusable(
            image=same,
            hwnd=1,
            display_index=1,
            rect=(0, 0, 100, 100),
            foreground=100,
            title="Test window",
        )
        is None
    )
    # A changed title (navigation, save, modified marker) invalidates the cache.
    assert (
        parser._reusable(
            image=same,
            hwnd=1,
            display_index=1,
            rect=(0, 0, 100, 100),
            foreground=99,
            title="Other page",
        )
        is None
    )
