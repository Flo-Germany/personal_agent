"""Tests for deterministic failure classification and durable lessons."""

from __future__ import annotations

from pathlib import Path

from agent.self_improvement import SelfImprovementService
from memory.knowledge import KnowledgeBase


def make_service(tmp_path: Path, **kwargs) -> tuple[SelfImprovementService, KnowledgeBase]:
    knowledge = KnowledgeBase(tmp_path / "knowledge.md")
    knowledge.ensure_file()
    return (
        SelfImprovementService(knowledge, **kwargs),
        knowledge,
    )


def test_context_shift_then_reanalysis_saves_a_recovery_fact(
    tmp_path: Path,
) -> None:
    service, knowledge = make_service(tmp_path)
    service.begin_turn("c1")

    service.record_tool_result(
        "c1",
        "click_element",
        {"element_name": "New Email", "element_id": 3},
        {
            "ok": False,
            "context_changed": True,
            "error": "The target window is no longer foreground.",
            "screenshot": "C:\\tmp\\snapshot.png",
        },
    )
    service.record_tool_result(
        "c1",
        "analyze_screen_ui",
        {"question": "the New Email button", "window_title": "Outlook"},
        {"ok": True, "snapshot_id": "som_2", "elements": []},
    )
    service.finish_turn("c1", "Draft an email", "Done.", success=True)

    text = knowledge.read()
    assert "Learned recovery" in text
    assert "context" in text.lower()
    assert service.status()["lessons_saved"] >= 1


def test_multistep_success_saves_an_optimized_workflow(tmp_path: Path) -> None:
    service, knowledge = make_service(tmp_path)
    service.begin_turn("c2")

    for tool, arguments in (
        ("start_program", {"program_name": "notepad"}),
        ("get_open_windows", {}),
        (
            "analyze_screen_ui",
            {"question": "the text editor", "window_title": "Notepad"},
        ),
        (
            "click_element",
            {"element_name": "Text-Editor", "element_id": 4},
        ),
        ("keyboard_type_text", {"text": "hello"}),
    ):
        service.record_tool_result(
            "c2",
            tool,
            arguments,
            {"ok": True},
        )

    service.finish_turn("c2", "Open Notepad and type hello", "Done.", success=True)

    text = knowledge.read()
    assert "Workflow:" in text
    assert "start_program" in text
    assert "keyboard_type_text" in text


def test_user_abort_never_becomes_a_lesson(tmp_path: Path) -> None:
    service, knowledge = make_service(tmp_path)
    service.begin_turn("c3")

    service.record_tool_result(
        "c3",
        "click_element",
        {"element_name": "Send"},
        {
            "ok": False,
            "aborted": True,
            "error": "Physical keyboard input detected.",
        },
    )
    service.record_tool_result(
        "c3",
        "get_desktop_context",
        {},
        {"ok": True},
    )
    service.finish_turn("c3", "Send it", "Stopped.", success=True)

    assert "Learned recovery" not in knowledge.read()
    assert "Workflow:" not in knowledge.read()


def test_unresolved_failure_does_not_save_a_workflow(tmp_path: Path) -> None:
    service, knowledge = make_service(tmp_path)
    service.begin_turn("c4")

    for tool in ("start_program", "analyze_screen_ui", "click_element"):
        service.record_tool_result(
            "c4",
            tool,
            {},
            {
                "ok": False,
                "element_missing": True,
                "error": "Element is no longer present.",
            },
        )

    service.finish_turn("c4", "Do the thing", "It failed.", success=True)

    assert "Workflow:" not in knowledge.read()


def test_recovery_facts_are_deduplicated(tmp_path: Path) -> None:
    service, knowledge = make_service(tmp_path)

    for _ in range(2):
        service.begin_turn("c5")
        service.record_tool_result(
            "c5",
            "click_element",
            {"element_name": "New Email"},
            {
                "ok": False,
                "context_changed": True,
                "error": "Window moved.",
            },
        )
        service.record_tool_result(
            "c5",
            "analyze_screen_ui",
            {"question": "the New Email button"},
            {"ok": True},
        )
        service.finish_turn("c5", "Draft", "Done.", success=True)

    text = knowledge.read()
    assert text.count("Learned recovery") == 1
