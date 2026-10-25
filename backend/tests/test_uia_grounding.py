"""Unit tests for UIA verification and the strict OCR fallback filters."""

from __future__ import annotations

from settings import AutomationSettings
from tools.som_vision import (
    DetectedElement,
    _dedupe_detected,
    _is_verified_interactable,
    _ocr_text_ok,
)


def test_button_and_known_controls_are_accepted_by_type() -> None:
    assert _is_verified_interactable(
        "Button", invoke=False, focusable=False, width=100, height=30
    ) == (True, "type")
    assert _is_verified_interactable(
        "MenuItem", invoke=False, focusable=True, width=80, height=24
    ) == (True, "type")
    assert _is_verified_interactable(
        "Hyperlink", invoke=False, focusable=False, width=200, height=20
    ) == (True, "type")
    assert _is_verified_interactable(
        "Edit", invoke=False, focusable=True, width=300, height=28
    ) == (True, "type")


def test_plain_text_is_not_accepted() -> None:
    # The original bug: plain text blocks were marked as clickable.
    assert _is_verified_interactable(
        "Text", invoke=False, focusable=True, width=400, height=24
    ) == (False, "")
    # Clickable text (a real Invoke pattern) is allowed.
    assert _is_verified_interactable(
        "Text", invoke=True, focusable=False, width=200, height=24
    ) == (True, "invoke")


def test_structural_containers_are_rejected() -> None:
    for kind in ("ToolBar", "Tree", "Tab", "MenuBar", "Window"):
        assert _is_verified_interactable(
            kind, invoke=True, focusable=True, width=200, height=40
        ) == (False, "")


def test_generic_invoke_controls_need_plausible_geometry() -> None:
    # A status-bar item or a custom icon button.
    assert _is_verified_interactable(
        "Group", invoke=True, focusable=False, width=66, height=53
    ) == (True, "invoke")
    assert _is_verified_interactable(
        "Pane", invoke=False, focusable=True, width=111, height=48
    ) == (True, "focus")
    # A full editor pane, a separator, or a thin scroll region is not a target.
    assert _is_verified_interactable(
        "Group", invoke=True, focusable=False, width=3840, height=2158
    ) == (False, "")
    assert _is_verified_interactable(
        "Group", invoke=True, focusable=False, width=3841, height=10
    ) == (False, "")
    assert _is_verified_interactable(
        "Pane", invoke=False, focusable=True, width=3, height=36
    ) == (False, "")
    # Focus alone is not enough for a plain image.
    assert _is_verified_interactable(
        "Image", invoke=False, focusable=True, width=40, height=40
    ) == (False, "")
    assert _is_verified_interactable(
        "Image", invoke=True, focusable=False, width=40, height=40
    ) == (True, "invoke")


def test_dedupe_keeps_the_more_semantic_control() -> None:
    group = DetectedElement(
        "row", "Group", "", 10, 10, 400, 60, source="uia", verified_by="invoke"
    )
    tree_item = DetectedElement(
        "row", "TreeItem", "", 10, 10, 400, 60, source="uia", verified_by="type"
    )
    kept = _dedupe_detected([group, tree_item])
    assert [item.control_type for item in kept] == ["TreeItem"]

    button = DetectedElement(
        "Save", "Button", "", 20, 20, 120, 50, source="uia", verified_by="type"
    )
    container = DetectedElement(
        "toolbar", "Group", "", 10, 10, 400, 60, source="uia", verified_by="invoke"
    )
    kept = _dedupe_detected([container, button])
    assert [item.control_type for item in kept] == ["Button"]


def test_ocr_filter_rejects_paragraphs_and_bad_geometry() -> None:
    settings = AutomationSettings()
    assert _ocr_text_ok("Save", 80, 30, 0.9, settings)
    assert _ocr_text_ok("New Email", 130, 30, 0.8, settings)

    # More than four words is treated as a paragraph, not a control.
    assert not _ocr_text_ok(
        "This is a long paragraph line here",
        300,
        24,
        0.9,
        settings,
    )
    # A long, flat line is not a button.
    assert not _ocr_text_ok("This is a test", 320, 24, 0.9, settings)
    # Too big or too small.
    assert not _ocr_text_ok("OK", 600, 20, 0.9, settings)
    assert not _ocr_text_ok("x", 8, 8, 0.9, settings)
    # Low confidence.
    assert not _ocr_text_ok("Save", 80, 30, 0.2, settings)
