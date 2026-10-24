"""Tests for window enumeration and UI Automation helpers."""

from __future__ import annotations

import pytest

from tools import window_tools


class FakeWindow:
    def __init__(
        self,
        title: str,
        *,
        visible: bool = True,
        active: bool = False,
        minimized: bool = False,
        hwnd: int = 123,
    ) -> None:
        self.title = title
        self.visible = visible
        self.isActive = active
        self.isMinimized = minimized
        self._hWnd = hwnd
        self.left = 10
        self.top = 20
        self.width = 800
        self.height = 600


def test_get_open_windows_filters_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [
        FakeWindow("Notepad"),
        FakeWindow("Active App", active=True),
        FakeWindow("Minimized", minimized=True),
        FakeWindow("Hidden", visible=False),
        FakeWindow(""),
    ]

    monkeypatch.setattr(
        "pygetwindow.getAllWindows",
        lambda: windows,
    )

    result = window_tools.get_open_windows()

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["active_window"] == "Active App"
    assert [item["title"] for item in result["windows"]] == [
        "Active App",
        "Notepad",
    ]
    assert result["windows"][0]["active"] is True


def test_read_window_content_requires_a_title() -> None:
    with pytest.raises(window_tools.WindowToolError):
        window_tools.read_window_content("   ")


def test_read_window_content_reports_missing_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pygetwindow.getAllWindows",
        lambda: [FakeWindow("Notepad")],
    )

    with pytest.raises(window_tools.WindowToolError) as error:
        window_tools.read_window_content("Calculator")

    assert "No visible window matches" in str(error.value)


def test_read_window_content_matches_fuzzy_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pygetwindow.getAllWindows",
        lambda: [FakeWindow("*Window awareness – Notepad")],
    )

    matches = window_tools._matching_windows("Window awareness ? Notepad")

    assert [window.title for window in matches] == [
        "*Window awareness – Notepad"
    ]


def test_read_window_content_reads_uia_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = FakeWindow("notes.txt - Notepad", active=True)

    monkeypatch.setattr(
        window_tools,
        "_matching_windows",
        lambda _title: [window],
    )
    monkeypatch.setattr(
        window_tools,
        "_extract_window_text",
        lambda _hwnd, _title: ("hello\nworld", 4, "uia"),
    )

    result = window_tools.read_window_content("Notepad")

    assert result["ok"] is True
    assert result["window_title"] == "notes.txt - Notepad"
    assert result["text"] == "hello\nworld"
    assert result["char_count"] == 11
    assert result["backend"] == "uia"
