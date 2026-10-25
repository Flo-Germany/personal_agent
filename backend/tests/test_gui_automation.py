"""Unit tests for Set-of-Mark parsing, guarded pointer tools, and dispatch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from settings import AutomationSettings, ToolSettings
from tools.executor import ToolExecutor
from tools.gui_automation import GUIAutomationService, _parse_grounding
from tools.input_failsafe import AutomationAbortedException, InputFailsafe
from tools.schemas import TOOL_SCHEMAS
from tools.som_vision import (
    DisplayBounds,
    SOMSnapshot,
    SOMParser,
    SnapshotStore,
    UIElement,
)


class FakeParser:
    def __init__(self, snapshot: SOMSnapshot) -> None:
        self.snapshot_value = snapshot
        self.store = SnapshotStore()
        self.calls: list[tuple[int | None, str | None]] = []

    def snapshot(
        self,
        *,
        display: int | None = None,
        window_title: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((display, window_title))
        self.store.put(self.snapshot_value)
        return {
            "ok": True,
            "snapshot": self.snapshot_value,
            "message": f"Detected {len(self.snapshot_value.elements)} elements.",
            "files": [self.snapshot_value.screenshot],
        }


class FakePointer:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []
        self.clicks: list[tuple[int, int, str, int]] = []
        self.typed: list[str] = []
        self.keys: list[list[str]] = []

    def move_to(
        self,
        x: int,
        y: int,
        *,
        duration: float | None = None,
        highlight: bool = True,
    ) -> None:
        self.moves.append((x, y))

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        duration: float | None = None,
        highlight: bool = True,
    ) -> None:
        self.clicks.append((x, y, button, clicks))

    def type_text(
        self,
        text: str,
        *,
        interval: float | None = None,
        press_enter: bool = False,
    ) -> None:
        self.typed.append(text)

    def press_keys(
        self,
        keys: list[str],
        *,
        interval: float | None = None,
    ) -> None:
        self.keys.append(list(keys))


def make_image(path: Path, width: int = 800, height: int = 600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), (18, 18, 22))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 220, 160], outline=(200, 200, 200), width=2)
    draw.rectangle([300, 200, 460, 250], outline=(200, 200, 200), width=2)
    image.save(path)


def make_snapshot(
    tmp_path: Path,
    *,
    disabled: bool = False,
) -> SOMSnapshot:
    screenshot = tmp_path / "som.png"
    make_image(screenshot)
    display = DisplayBounds(
        index=1,
        left=0,
        top=0,
        width=800,
        height=600,
        primary=True,
    )
    elements = (
        UIElement(
            id=1,
            name="New Email",
            control_type="Button",
            automation_id="NewEmailButton",
            left=100,
            top=100,
            right=220,
            bottom=160,
            enabled=not disabled,
        ),
        UIElement(
            id=2,
            name="To",
            control_type="Edit",
            automation_id="ToField",
            left=300,
            top=200,
            right=460,
            bottom=250,
        ),
    )
    return SOMSnapshot(
        snapshot_id="som_test_123",
        elements=elements,
        display=display,
        window_title="Outlook - Mock",
        screenshot=str(screenshot),
        image_width=800,
        image_height=600,
        created_at=time.time(),
    )


def make_service(
    tmp_path: Path,
    *,
    disabled: bool = False,
    grounder: Any | None = None,
) -> tuple[GUIAutomationService, FakeParser, FakePointer, InputFailsafe]:
    snapshot = make_snapshot(tmp_path, disabled=disabled)
    parser = FakeParser(snapshot)
    pointer = FakePointer()
    guard = InputFailsafe(enabled=False, arm_delay_seconds=0.0)
    service = GUIAutomationService(
        AutomationSettings(enabled=True),
        parser=parser,  # type: ignore[arg-type]
        store=parser.store,
        guard=guard,
        pointer=pointer,  # type: ignore[arg-type]
        grounder=grounder
        or (lambda question, snap, image: {"id": 1, "confidence": 0.9}),
    )
    return service, parser, pointer, guard


def test_annotation_writes_numbered_screenshot(tmp_path: Path) -> None:
    parser = SOMParser(
        AutomationSettings(
            screenshot_dir=tmp_path / "shots",
            screenshot_max_width=1600,
        )
    )
    snapshot = make_snapshot(tmp_path)
    image = Image.open(snapshot.screenshot).convert("RGB")

    path, width, height = parser._annotate(
        image,
        snapshot.elements,
        snapshot.display,
    )

    saved = Path(path)
    assert saved.is_file()
    assert saved.parent == tmp_path / "shots"
    assert (width, height) == (800, 600)
    reopened = Image.open(saved)
    assert reopened.size == (800, 600)
    # The red button box should be present near its top-left corner.
    assert reopened.getpixel((101, 101)) != (18, 18, 22)


def test_snapshot_store_expires_old_snapshots() -> None:
    store = SnapshotStore(ttl_seconds=10)
    snapshot = SOMSnapshot(
        snapshot_id="old",
        elements=(),
        display=DisplayBounds(1, 0, 0, 100, 100, True),
        window_title="",
        screenshot="",
        image_width=100,
        image_height=100,
        created_at=0.0,
    )
    store.put(snapshot)

    assert store.get("old") is None
    assert store.count == 0


def test_analyze_returns_elements_map_and_suggestion(tmp_path: Path) -> None:
    service, parser, _pointer, _guard = make_service(tmp_path)

    result = service.analyze(
        "the New Email button",
        window_title="Outlook - Mock",
    )

    assert result["ok"] is True
    assert result["snapshot_id"] == "som_test_123"
    assert result["suggested_id"] == 1
    assert result["suggested_element"]["name"] == "New Email"
    assert result["coordinate_map"]["1"]["x"] == 160
    assert result["coordinate_map"]["1"]["y"] == 130
    assert result["files"] == [str(tmp_path / "som.png")]
    assert "click_element" in result["next_step"]
    assert parser.calls == [(None, "Outlook - Mock")]


def test_analyze_without_elements_returns_no_guess(tmp_path: Path) -> None:
    service, _parser, _pointer, _guard = make_service(tmp_path)
    empty = SOMSnapshot(
        snapshot_id="som_empty",
        elements=(),
        display=DisplayBounds(1, 0, 0, 800, 600, True),
        window_title="Empty",
        screenshot=str(tmp_path / "som.png"),
        image_width=800,
        image_height=600,
        created_at=time.time(),
    )
    service.parser.snapshot_value = empty  # type: ignore[attr-defined]

    result = service.analyze("anything", window_title="Empty")

    assert result["ok"] is True
    assert result["suggested_id"] == 0
    assert result["elements"] == []
    assert "No interactable elements" in result["next_step"]


def test_click_uses_the_snapshot_center_and_rejects_wrong_name(
    tmp_path: Path,
) -> None:
    service, _parser, pointer, _guard = make_service(tmp_path)

    analysis = service.analyze("the New Email button")
    snapshot_id = analysis["snapshot_id"]

    ok = service.click(snapshot_id, 1, "New Email")
    assert ok["ok"] is True
    assert pointer.clicks == [(160, 130, "left", 1)]

    pointer.clicks.clear()
    wrong = service.click(snapshot_id, 1, "Send")
    assert wrong["ok"] is False
    assert "not 'Send'" in wrong["error"]
    assert pointer.clicks == []


def test_click_rejects_expired_snapshot_and_disabled_element(
    tmp_path: Path,
) -> None:
    service, _parser, pointer, _guard = make_service(tmp_path)
    analysis = service.analyze("the New Email button")
    snapshot_id = analysis["snapshot_id"]

    stale = service.click("som_missing", 1, "New Email")
    assert stale["ok"] is False
    assert "expired" in stale["error"]
    assert pointer.clicks == []

    disabled_service, _disabled_parser, disabled_pointer, _guard2 = make_service(
        tmp_path / "disabled",
        disabled=True,
    )
    disabled_analysis = disabled_service.analyze("the New Email button")
    disabled = disabled_service.click(
        disabled_analysis["snapshot_id"],
        1,
        "New Email",
    )
    assert disabled["ok"] is False
    assert "disabled" in disabled["error"]
    assert disabled_pointer.clicks == []


def test_move_typing_and_keys_use_the_pointer(tmp_path: Path) -> None:
    service, _parser, pointer, _guard = make_service(tmp_path)
    analysis = service.analyze("the To field")
    snapshot_id = analysis["snapshot_id"]

    moved = service.move(snapshot_id, 2, "To")
    assert moved["ok"] is True
    assert pointer.moves == [(380, 225)]

    blocked = service.type_text("hello")
    assert blocked["ok"] is False
    assert blocked["needs_click"] is True
    assert pointer.typed == []

    clicked = service.click(snapshot_id, 2, "To")
    assert clicked["ok"] is True
    assert clicked["focused_text_field"] is True

    typed = service.type_text("hello")
    assert typed["ok"] is True
    assert pointer.typed == ["hello"]

    pressed = service.press_keys(["ctrl", "s"])
    assert pressed["ok"] is True
    assert pointer.keys == [["ctrl", "s"]]


def test_launched_program_prefers_the_new_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWindow:
        def __init__(self, title: str, active: bool = False) -> None:
            self.title = title
            self.isActive = active

    service, parser, _pointer, _guard = make_service(tmp_path)
    existing = FakeWindow("http.txt - Notepad")
    new = FakeWindow("Unbenannt - Notepad")
    input_experience = FakeWindow("Windows Input Experience", active=True)
    calls = {"count": 0}

    def fake_visible() -> list[FakeWindow]:
        calls["count"] += 1
        if calls["count"] == 1:
            return [input_experience, existing]
        return [input_experience, existing, new]

    monkeypatch.setattr(
        "tools.som_vision._visible_windows",
        fake_visible,
    )

    service.note_launched_program(
        "notepad",
        r"C:\Windows\System32\notepad.exe",
        before_titles={"http.txt - notepad"},
    )

    assert service._preferred_window_title == "Unbenannt - Notepad"

    service.analyze("the text editor")
    assert parser.calls[-1] == (None, "Unbenannt - Notepad")


def test_physical_input_aborts_and_halts_gui_tools(tmp_path: Path) -> None:
    service, _parser, pointer, guard = make_service(tmp_path)
    analysis = service.analyze("the New Email button")
    snapshot_id = analysis["snapshot_id"]

    guard.begin_execution()
    guard.simulate_physical_input("Physical mouse twitch")
    aborted = service.click(snapshot_id, 1, "New Email")

    assert aborted["ok"] is False
    assert aborted["aborted"] is True
    assert "physical" in aborted["error"].lower()
    assert pointer.clicks == []

    blocked = service.analyze("the New Email button")
    assert blocked["aborted"] is True

    service.reset()
    after = service.analyze("the New Email button")
    assert after["ok"] is True
    assert after["suggested_id"] == 1


def test_input_failsafe_detects_injected_flags() -> None:
    class Data:
        def __init__(self, flags: int) -> None:
            self.flags = flags

    assert InputFailsafe._is_injected(Data(0x01), keyboard=False) is True
    assert InputFailsafe._is_injected(Data(0x00), keyboard=False) is False
    assert InputFailsafe._is_injected(Data(0x10), keyboard=True) is True
    assert InputFailsafe._is_injected(Data(0x12), keyboard=True) is True
    assert InputFailsafe._is_injected(Data(0x01), keyboard=True) is False
    assert InputFailsafe._is_injected(Data(0x00), keyboard=True) is False


def test_parse_grounding_handles_json_fences_and_bare_ids() -> None:
    assert _parse_grounding('```json\n{"id": 3, "confidence": 0.8}\n```') == {
        "id": 3,
        "reason": "",
        "confidence": 0.8,
    }
    assert _parse_grounding("The answer is 7.")["id"] == 7
    assert _parse_grounding("no idea") == {}


def test_gui_tools_are_registered_with_correct_confirmation_flags() -> None:
    names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert {
        "analyze_screen_ui",
        "move_cursor_to",
        "click_element",
        "keyboard_type_text",
        "keyboard_press_keys",
    }.issubset(names)

    executor = ToolExecutor(ToolSettings())
    assert executor.requires_confirmation("analyze_screen_ui") is False
    for name in (
        "move_cursor_to",
        "click_element",
        "keyboard_type_text",
        "keyboard_press_keys",
    ):
        assert executor.requires_confirmation(name) is True


def test_executor_dispatches_gui_tools(tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeAutomation:
        def analyze(self, question, *, display=None, window_title=None):
            calls.append(("analyze", question, display, window_title))
            return {"ok": True, "message": "mapped"}

        def move(self, snapshot_id, element_id, element_name, **kwargs):
            calls.append(("move", snapshot_id, element_id, element_name))
            return {"ok": True}

        def click(self, snapshot_id, element_id, element_name, **kwargs):
            calls.append(("click", snapshot_id, element_id, element_name))
            return {"ok": True}

        def type_text(self, text, **kwargs):
            calls.append(("type", text))
            return {"ok": True}

        def press_keys(self, keys, **kwargs):
            calls.append(("keys", keys))
            return {"ok": True}

        def reset(self):
            calls.append(("reset",))

        def status(self):
            return {"ok": True}

    executor = ToolExecutor(ToolSettings(), automation_service=FakeAutomation())  # type: ignore[arg-type]

    assert executor.execute(
        "analyze_screen_ui",
        {"question": "the New Email button", "window_title": "Outlook"},
    )["ok"] is True
    assert executor.execute(
        "click_element",
        {
            "snapshot_id": "som_1",
            "element_id": 1,
            "element_name": "New Email",
        },
    )["ok"] is True
    assert executor.execute(
        "move_cursor_to",
        {"snapshot_id": "som_1", "element_id": 1, "element_name": "New Email"},
    )["ok"] is True
    assert executor.execute("keyboard_type_text", {"text": "hello"})["ok"] is True
    assert executor.execute("keyboard_press_keys", {"keys": ["ctrl", "s"]})["ok"] is True
    executor.reset_automation()

    assert calls == [
        ("analyze", "the New Email button", None, "Outlook"),
        ("click", "som_1", 1, "New Email"),
        ("move", "som_1", 1, "New Email"),
        ("type", "hello"),
        ("keys", ["ctrl", "s"]),
        ("reset",),
    ]
