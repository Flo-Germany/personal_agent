"""Tests for the Phase 21 autonomous experiment harness itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from agent.ollama_client import NativeOllamaClient
from tests.autonomous_evaluate import audit_frontend
from tests.autonomous_experiments import (
    build_experiments,
    mutate_with_typos,
)
from tests.autonomous_lib import (
    Check,
    Evaluation,
    ExperimentContext,
    TurnResult,
    check,
    clarification_checks,
    event_checks,
    file_text_checks,
    run_turn,
)


class FakeClient:
    """Minimal BackendClient stand-in for run_turn loop tests."""

    base_url = "http://fake"

    def __init__(self, scripted: list[list[dict[str, Any]]]) -> None:
        self.scripted = list(scripted)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.interrupted: list[str] = []

    def stream_events(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        self.calls.append((path, dict(payload)))
        events = self.scripted.pop(0)
        yield from events

    def interrupt(self, conversation_id: str) -> None:
        self.interrupted.append(conversation_id)


def _final(text: str = "Done.") -> dict[str, Any]:
    return {"type": "final", "reply": text, "streamed": True}


def test_run_turn_follows_approval_and_finishes() -> None:
    client = FakeClient(
        [
            [
                {
                    "type": "status",
                    "phase": "tool_started",
                    "tool": "create_file",
                    "message": "Creating file...",
                },
                {
                    "type": "action_required",
                    "approval_id": "abc",
                    "conversation_id": "c1",
                },
            ],
            [
                {
                    "type": "tool_result",
                    "tool": "create_file",
                    "ok": True,
                    "summary": "Created.",
                    "result": {"ok": True},
                },
                _final("Created the file."),
            ],
        ]
    )

    result = run_turn(client, "make a file", timeout=10)

    assert result.final == "Created the file."
    assert result.error is None
    assert [path for path, _ in client.calls] == ["/chat/stream", "/chat/approve"]
    assert client.calls[1][1]["approved"] is True


def test_run_turn_follows_display_choice() -> None:
    client = FakeClient(
        [
            [{"type": "display_required", "approval_id": "d1"}],
            [_final("Display analyzed.")],
        ]
    )

    result = run_turn(client, "look at my screen", timeout=10, display=2)

    assert result.final == "Display analyzed."
    assert client.calls[1][0] == "/chat/display"
    assert client.calls[1][1]["display"] == 2


def test_run_turn_reports_a_stream_that_ends_without_final() -> None:
    client = FakeClient(
        [
            [
                {
                    "type": "status",
                    "phase": "tool_started",
                    "tool": "search_system",
                    "message": "Searching...",
                }
            ]
        ]
    )

    result = run_turn(client, "find it", timeout=10)

    assert result.final is None
    assert result.error is not None
    assert "closed before a final" in result.error


def test_evaluation_scores_each_dimension_and_requires_all_checks() -> None:
    passing = Evaluation(
        [
            Check("a", "accuracy", True),
            Check("p", "performance", True),
            Check("u", "ux", True),
        ]
    )
    assert passing.score == 100.0
    assert passing.passed is True

    mixed = Evaluation(
        [
            Check("a", "accuracy", True),
            Check("u", "ux", False),
            Check("p", "performance", True),
        ]
    )
    assert mixed.dimensions() == {"accuracy": 100.0, "performance": 100.0, "ux": 0.0}
    assert mixed.score == 66.7
    assert mixed.passed is False


def test_clarification_checks_accept_a_question_and_reject_guessing() -> None:
    question = TurnResult(final="Which folder should I use?")
    assert all(item.ok for item in clarification_checks(question, label="edge"))

    guessing = TurnResult(
        final="Done.",
        events=[
            {
                "type": "tool_result",
                "tool": "create_file",
                "ok": True,
                "summary": "Created.",
                "result": {"ok": True},
            }
        ],
    )
    checks = {item.name: item for item in clarification_checks(guessing, label="edge")}
    assert checks["asks_for_clarification"].ok is False
    assert checks["no_destructive_guess"].ok is False


def test_event_checks_validate_pairing_and_compactness() -> None:
    turn = TurnResult(
        events=[
            {
                "type": "status",
                "phase": "tool_started",
                "tool": "read_file_content",
                "message": "Reading file...",
            },
            {
                "type": "tool_result",
                "tool": "read_file_content",
                "ok": True,
                "summary": "Read 4 bytes.",
                "result": {"ok": True},
            },
            {"type": "assistant_delta", "text": "Hello"},
            _final("Hello."),
        ]
    )
    checks = {item.name: item for item in event_checks(turn)}
    assert all(item.ok for item in checks.values()), checks

    unpaired = TurnResult(
        events=[
            {
                "type": "tool_result",
                "tool": "read_file_content",
                "ok": True,
                "summary": "Read.",
                "result": {},
            },
            _final("ok"),
        ]
    )
    assert {item.name: item for item in event_checks(unpaired)}[
        "tool_events_paired"
    ].ok is False


def test_file_text_checks_report_missing_and_wrong_artifacts(tmp_path: Path) -> None:
    ctx = ExperimentContext.create(tmp_path, "artifact")
    ctx.write("ok.txt", "phase 21")
    checks = file_text_checks(ctx, "ok.txt", must_equal="phase 21")
    assert all(item.ok for item in checks)

    missing = file_text_checks(ctx, "missing.txt")
    assert missing[0].ok is False

    wrong = file_text_checks(ctx, "ok.txt", must_equal="other")
    assert any(item.ok is False for item in wrong)


def test_typo_mutation_is_deterministic_and_preserves_paths() -> None:
    prompt = "Create the folder C:\\temp\\phase21 and copy the report there"
    mutated = mutate_with_typos(prompt, seed="run-1")

    assert mutated != prompt
    assert "C:\\temp\\phase21" in mutated
    assert mutate_with_typos(prompt, seed="run-1") == mutated


def test_catalog_has_the_required_coverage() -> None:
    experiments = build_experiments(include_gui=False)
    ids = {experiment.id for experiment in experiments}

    assert {
        "static_ui_contract",
        "cli_nested_tree",
        "cli_disk_metric",
        "cli_search_newest",
        "cli_move",
        "edge_ambiguous_place",
        "edge_contradiction",
        "edge_typo",
    }.issubset(ids)
    assert not any(item.requires_gui for item in experiments)

    with_gui = build_experiments(include_gui=True)
    assert {item.id for item in with_gui if item.requires_gui} == {
        "gui_notepad_save",
        "gui_editor_font",
    }


def test_experiment_prompt_uses_context_values(tmp_path: Path) -> None:
    ctx = ExperimentContext.create(tmp_path, "prompt")
    ctx.set("marker", "Phase21-test-marker")
    editor = next(
        item for item in build_experiments(include_gui=True) if item.id == "gui_editor_font"
    )
    prompt = editor.prompt(ctx)
    assert prompt is not None
    assert "Phase21-test-marker" in prompt

    tree = next(
        item for item in build_experiments() if item.id == "cli_nested_tree"
    )
    tree_prompt = tree.prompt(ctx)
    assert tree_prompt is not None
    assert str(ctx.workspace) in tree_prompt


def test_frontend_contract_audit_passes_on_the_repository() -> None:
    checks = audit_frontend()
    assert len(checks) >= 8
    failed = [item.name for item in checks if not item.ok]
    assert not failed, failed


def test_native_client_keep_alive_payload_is_verbatim() -> None:
    client = NativeOllamaClient(
        "http://127.0.0.1:11434/v1",
        keep_alive=0,
        num_ctx=4096,
    )
    payload = client._payload(  # noqa: SLF001 - protocol contract
        {"model": "test", "messages": [], "temperature": 0.0},
        stream=False,
    )
    assert payload["keep_alive"] == 0
    assert payload["options"]["num_ctx"] == 4096

    keep_warm = NativeOllamaClient(
        "http://127.0.0.1:11434/v1",
        keep_alive=1800,
        num_ctx=8192,
    )
    warm_payload = keep_warm._payload(  # noqa: SLF001 - protocol contract
        {"model": "test", "messages": [], "temperature": 0.0},
        stream=False,
    )
    assert warm_payload["keep_alive"] == 1800


def test_static_contract_experiment_is_scored_without_a_model(tmp_path: Path) -> None:
    experiment = next(
        item
        for item in build_experiments()
        if item.id == "static_ui_contract"
    )
    ctx = ExperimentContext.create(tmp_path, "static")
    checks = experiment.verifier(ctx, TurnResult.empty())
    assert checks
    assert all(item.dimension in {"ux", "accuracy"} for item in checks)


def test_report_json_round_trip(tmp_path: Path) -> None:
    from tests.autonomous_evaluate import summarize, write_report

    records = [
        {
            "id": "x",
            "category": "cli",
            "passed": True,
            "score": 100.0,
            "dimensions": {"accuracy": 100.0},
        }
    ]
    summary = summarize(records)
    report = {"run_id": "r", "experiments": records, "summary": summary}
    path = tmp_path / "report.json"
    write_report(report, path)
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["passed"] == 1


def test_summary_omits_dimensions_without_checks() -> None:
    from tests.autonomous_evaluate import summarize

    summary = summarize(
        [
            {
                "id": "static",
                "category": "static",
                "passed": True,
                "score": 100.0,
                "dimensions": {"accuracy": 100.0, "ux": 100.0},
            }
        ]
    )
    assert summary["dimensions"] == {"accuracy": 100.0, "ux": 100.0}


def test_failure_bundle_contains_failed_checks_and_focus(tmp_path: Path) -> None:
    from tests.autonomous_evaluate import failure_bundle, render_failure_brief

    report = {
        "run_id": "run-1",
        "model": "qwen3-vl:8b-instruct",
        "summary": {"total": 2, "passed": 1, "average_score": 50.0},
        "experiments": [
            {
                "id": "ok",
                "title": "passing",
                "category": "cli",
                "passed": True,
                "score": 100.0,
                "dimensions": {"accuracy": 100.0},
                "checks": [],
            },
            {
                "id": "broken",
                "title": "failing experiment",
                "category": "cli",
                "score": 0.0,
                "passed": False,
                "dimensions": {"ux": 0.0},
                "prompt": "do the thing",
                "final": "no",
                "error": "timed out",
                "tools": ["read_file_content"],
                "tool_arguments": [],
                "events_file": str(tmp_path / "events.jsonl"),
                "workspace": str(tmp_path),
                "checks": [
                    {
                        "name": "tool_events_paired",
                        "dimension": "ux",
                        "ok": False,
                        "detail": "missing start",
                        "weight": 1.0,
                    },
                    {
                        "name": "info_only",
                        "dimension": "info",
                        "ok": False,
                        "detail": "not scored",
                        "weight": 1.0,
                    },
                ],
            },
        ],
    }

    bundle = failure_bundle(report)
    assert [item["id"] for item in bundle["failures"]] == ["broken"]
    assert bundle["focus"] == ["broken:ux/tool_events_paired"]
    assert bundle["failures"][0]["failed_checks"][0]["name"] == "tool_events_paired"

    brief = render_failure_brief(bundle)
    assert "## broken - failing experiment" in brief
    assert "tool_events_paired" in brief
