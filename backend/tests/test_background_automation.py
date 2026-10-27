"""Phase 26: UIA driver, isolated workspace and routing contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tools import gui_automation
from tools import som_vision
from tools import workspace as workspace_module
from tools.gui_automation import GUIAutomationService
from tools.schemas import TOOL_SCHEMAS
from tools.som_vision import DisplayBounds, SOMSnapshot, UIElement
from tools.uia_driver import UiaDriver, UiaError
from tools.workspace import Workspace, WorkspaceManager


def _schema(name: str) -> dict:
    return next(
        item["function"]
        for item in TOOL_SCHEMAS
        if item["function"]["name"] == name
    )


# --------------------------------------------------------------- contracts
def test_phase26_tools_exist_with_expected_parameters() -> None:
    query = _schema("ui_query")
    assert {"window", "window_handle", "query", "depth", "max_nodes"} <= set(
        query["parameters"]["properties"]
    )
    action = _schema("ui_action")
    assert {"element_id", "action", "value", "button", "clicks"} <= set(
        action["parameters"]["properties"]
    )
    assert "invoke" in action["parameters"]["properties"]["action"]["enum"]
    isolated = _schema("isolated_workspace")
    assert isolated["parameters"]["properties"]["action"]["enum"] == [
        "status",
        "attach",
        "capture",
        "release",
    ]


def test_analyze_screen_ui_exposes_headless_switch() -> None:
    properties = _schema("analyze_screen_ui")["parameters"]["properties"]
    assert "headless" in properties
    assert "window_handle" in properties


def test_confirmation_policy_for_background_tools() -> None:
    from tools.os_tools import TOOL_REQUIRES_CONFIRMATION

    assert TOOL_REQUIRES_CONFIRMATION["ui_query"] is False
    assert TOOL_REQUIRES_CONFIRMATION["ui_action"] is True
    assert TOOL_REQUIRES_CONFIRMATION["isolated_workspace"] is True


def test_gui_tool_selector_and_prompt_cover_the_hierarchy() -> None:
    from agent.chat import _GUI_TOOL_NAMES
    from agent.prompt import build_system_prompt
    from settings import Settings

    assert {"ui_query", "ui_action", "isolated_workspace"} <= _GUI_TOOL_NAMES
    prompt = build_system_prompt(Settings(agent_name="Compa"))
    assert "Priority 1" in prompt and "ui_query" in prompt
    assert "Priority 2" in prompt and "isolated_workspace" in prompt
    assert "physical cursor" in prompt


# --------------------------------------------------------------- UIA driver
class _FakeInfo:
    def __init__(self, name: str, control_type: str, handle: int = 0) -> None:
        self.name = name
        self.control_type = control_type
        self.automation_id = f"auto-{name}"
        self.class_name = f"class-{control_type}"
        self.handle = handle


class _FakeWrapper:
    def __init__(self, name: str, control_type: str, children=None) -> None:
        self.element_info = _FakeInfo(name, control_type)
        self._children = children or []

    def children(self):
        return self._children

    def is_enabled(self):
        return True

    def is_offscreen(self):
        return False


def _fake_tree() -> _FakeWrapper:
    save = _FakeWrapper("Save", "Button")
    cancel = _FakeWrapper("Cancel", "Button")
    edit = _FakeWrapper("Name", "Edit")
    group = _FakeWrapper("Group", "Pane", [save, cancel, edit])
    root = _FakeWrapper("Main window", "Window", [group])
    return root


def test_tree_assigns_stable_ids_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = UiaDriver()
    monkeypatch.setattr(driver, "resolve_window", lambda title=None, hwnd=None: {"hwnd": 1, "title": "Demo"})
    monkeypatch.setattr(driver, "_wrapper", lambda hwnd: _fake_tree())
    monkeypatch.setattr("tools.uia_driver.patterns_of", lambda wrapper: ("invoke",))
    monkeypatch.setattr("tools.uia_driver._rect", lambda wrapper: (0, 0, 10, 10))

    full = driver.tree(hwnd=1, depth=3, max_nodes=20)
    assert full["ok"] and full["input_mode"] == "background"
    ids = [item["id"] for item in full["elements"]]
    assert ids == ["u1", "u2", "u3", "u4", "u5"]

    filtered = driver.tree(hwnd=1, query="save")
    assert [item["name"] for item in filtered["elements"]] == ["Save"]

    miss = driver.tree(hwnd=1, query="nothing-matches")
    assert miss["count"] == 0 and miss["scanned"] == 5
    assert "without a query filter" in miss["message"]

    limited = driver.tree(hwnd=1, max_nodes=10)
    assert limited["count"] == 5 and limited["truncated"] is False


def test_tree_truncates_at_max_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = UiaDriver()
    monkeypatch.setattr(driver, "resolve_window", lambda title=None, hwnd=None: {"hwnd": 1, "title": "Demo"})
    children = [_FakeWrapper(f"Item {index}", "Button") for index in range(20)]
    monkeypatch.setattr(driver, "_wrapper", lambda hwnd: _FakeWrapper("Root", "Window", children))
    monkeypatch.setattr("tools.uia_driver.patterns_of", lambda wrapper: ("invoke",))
    monkeypatch.setattr("tools.uia_driver._rect", lambda wrapper: None)

    result = driver.tree(hwnd=1, max_nodes=10)
    assert result["truncated"] is True
    assert result["count"] == 10


def test_stale_element_id_is_rejected() -> None:
    driver = UiaDriver()
    with pytest.raises(UiaError, match="stale|unknown"):
        driver.act("u99", "invoke")


# --------------------------------------------------------- background click
class _FakePointer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class _FakeWorkspace:
    def __init__(self) -> None:
        self.clicks: list[tuple] = []
        self.types: list[tuple] = []

    def click(self, hwnd, x, y, *, button="left", clicks=1):
        self.clicks.append((hwnd, x, y, button, clicks))
        return {"ok": True, "via": "post_message", "cursor_moved": False}

    def type_text(self, hwnd, text, *, press_enter=False):
        self.types.append((hwnd, text, press_enter))
        return {"ok": True, "characters": len(text), "target_hwnd": hwnd, "via": "wm_char"}


def _service(monkeypatch: pytest.MonkeyPatch) -> tuple[GUIAutomationService, _FakePointer, _FakeWorkspace]:
    from settings import AutomationSettings

    pointer = _FakePointer()
    service = GUIAutomationService(AutomationSettings(enabled=True), pointer=pointer)
    fake = _FakeWorkspace()
    monkeypatch.setattr(gui_automation, "_workspace", lambda: fake)
    snapshot = SOMSnapshot(
        snapshot_id="som_test",
        elements=(
            UIElement(
                id=1,
                name="Save",
                control_type="Button",
                automation_id="save",
                left=10,
                top=10,
                right=110,
                bottom=40,
            ),
        ),
        display=DisplayBounds(index=1, left=0, top=0, width=800, height=600),
        window_title="Demo",
        screenshot="",
        image_width=800,
        image_height=600,
        created_at=time.time(),
        window_handle=4242,
        input_mode="background",
    )
    service.store.put(snapshot)
    service._background_target = snapshot
    # The fake handle cannot pass the live-window validation; the background
    # branch under test runs after it in production.
    service._require_element = lambda snapshot_id, element_id, element_name: (
        snapshot,
        snapshot.element(int(element_id)),
        None,
    )
    return service, pointer, fake


def test_background_click_never_moves_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    service, pointer, fake = _service(monkeypatch)
    result = service.click("som_test", 1, "Save")
    assert result["ok"] is True
    assert result["mode"] == "background"
    assert result["cursor_moved"] is False
    assert fake.clicks == [(4242, 60, 25, "left", 1)]
    assert pointer.calls == []


def test_background_type_uses_window_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    service, pointer, fake = _service(monkeypatch)
    result = service.type_text("hello", press_enter=True)
    assert result["ok"] is True and result["mode"] == "background"
    assert fake.types == [(4242, "hello", True)]
    assert pointer.calls == []


# ----------------------------------------------------------- isolated state
def test_workspace_public_shape_and_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "workspaces.json"
    monkeypatch.setattr(workspace_module, "STATE_PATH", state_path)
    item = Workspace(
        hwnd=123,
        title="Demo",
        mode="offscreen",
        original_rect=[10, 20, 300, 200],
        was_maximized=False,
        attached_at=time.time(),
    )
    state_path.write_text(
        json.dumps({"123": item.__dict__}),
        encoding="utf-8",
    )
    manager = WorkspaceManager()
    # Loading validates the handle against Windows; a fake handle is dropped.
    assert 123 not in manager._items
    public = item.to_public()
    assert public["visible_to_user"] is False
    assert public["mode"] == "offscreen"


def test_virtual_desktop_worker_reports_capability() -> None:
    from tools.workspace import VirtualDesktopBridge

    listing = VirtualDesktopBridge().list()
    assert isinstance(listing, dict)
    assert "ok" in listing
    if listing.get("ok"):
        assert isinstance(listing.get("desktops"), list)


# ------------------------------------------------------------ headless SoM
def test_headless_snapshot_requires_a_live_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    from settings import AutomationSettings

    parser = som_vision.SOMParser(AutomationSettings())
    monkeypatch.setattr("tools.som_vision._window_rect_handle", lambda hwnd: None)
    result = parser.snapshot(hwnd=999999, headless=True)
    assert result["ok"] is False
    assert "no longer exists" in result["error"]


def test_headless_analysis_never_attaches_implicitly() -> None:
    from settings import AutomationSettings, ToolSettings
    from tools.executor import ToolExecutor

    class FakeAutomation:
        def analyze(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("analysis must not start without an attached window")

    executor = ToolExecutor(ToolSettings(), automation_service=FakeAutomation())  # type: ignore[arg-type]
    result = executor.execute(
        "analyze_screen_ui",
        {"question": "the Save button", "window_title": "Not attached", "headless": True},
    )
    assert result["ok"] is False
    assert result["needs_attach"] is True
    assert "isolated_workspace" in result["error"]
