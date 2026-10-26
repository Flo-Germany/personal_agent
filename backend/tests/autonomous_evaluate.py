"""Scoring, static UI audits, and report rendering for Phase 21.

The evaluator is deliberately deterministic: every score comes from a named
check with evidence in the SSE stream, a filesystem artifact, a GUI result
file, or the frontend source contract. No subjective "looks nice" call is made
by the script; screenshots and event logs are captured so the builder agent
can review visual regressions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.autonomous_lib import (
    DIMENSIONS,
    REPO_ROOT,
    Check,
    Evaluation,
    Experiment,
    ExperimentContext,
    TurnResult,
    check,
    clarification_checks,
    event_checks,
    performance_checks,
    shorten,
    tool_choice_checks,
)


# --------------------------------------------------------------------------- #
# Runtime evaluation
# --------------------------------------------------------------------------- #
def evaluate_turn(
    experiment: Experiment,
    ctx: ExperimentContext,
    turn: TurnResult,
    *,
    include_service_checks: bool = True,
) -> Evaluation:
    checks: list[Check] = []

    if experiment.prompt_template is not None:
        checks.append(
            check(
                "turn_completed",
                "accuracy",
                turn.ok,
                turn.error or "final answer received",
            )
        )
        checks.extend(
            tool_choice_checks(
                turn,
                expected=experiment.expected_tools,
                forbidden=experiment.forbidden_tools,
            )
        )
        if experiment.expect_clarification:
            checks.extend(
                clarification_checks(turn, label=experiment.id)
            )
        checks.extend(event_checks(turn))
        checks.extend(
            performance_checks(
                turn,
                timeout=experiment.timeout,
                max_tools=experiment.max_tools,
            )
        )

    checks.extend(experiment.verifier(ctx, turn))

    # Keep one check per name while preserving order; later evidence wins.
    unique: dict[str, Check] = {}
    for item in checks:
        unique[item.name] = item
    evaluation = Evaluation(checks=list(unique.values()))

    if not include_service_checks:
        evaluation.checks = [
            item for item in evaluation.checks if item.dimension != "performance"
        ]
    return evaluation


# --------------------------------------------------------------------------- #
# Frontend / UX static contract
# --------------------------------------------------------------------------- #
def _read(relative: str) -> str:
    try:
        return (REPO_ROOT / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def audit_frontend() -> list[Check]:
    """Check the structural contract the live status UI relies on.

    This catches the common "the data exists but the UI drops it" class of
    regression without needing a screenshot parser.
    """
    tool_status = _read("frontend/src/components/ToolStatus.tsx")
    app = _read("frontend/src/App.tsx")
    types = _read("frontend/src/types.ts")
    api = _read("frontend/src/lib/api.ts")
    chat = _read("backend/agent/chat.py")

    checks = [
        check(
            "ui_status_component_exists",
            "ux",
            bool(tool_status.strip()),
            "frontend/src/components/ToolStatus.tsx",
        ),
        check(
            "ui_status_rendered_in_app",
            "ux",
            "ToolStatus" in app and "status" in app,
            "App.tsx renders ToolStatus for status messages",
        ),
        check(
            "ui_status_data_typed",
            "ux",
            "ToolStatusData" in types,
            "ToolStatusData is part of the shared types",
        ),
        check(
            "ui_status_is_animated",
            "ux",
            "animate-spin" in tool_status or "animate-pulse" in tool_status,
            "the running task has an animated indicator",
        ),
        check(
            "ui_status_is_compact",
            "ux",
            "truncate" in tool_status
            and ("max-h-" in tool_status or "overflow-hidden" in tool_status),
            "labels are truncated and long details are height-capped",
        ),
        check(
            "ui_status_has_stable_anchor",
            "ux",
            "data-" in tool_status or "aria-" in tool_status,
            "the status block exposes an accessible/stable DOM anchor",
        ),
        check(
            "ui_status_collapses",
            "ux",
            "setExpanded" in tool_status and "expanded" in tool_status,
            "the task list can collapse after completion",
        ),
        check(
            "ui_status_shows_progress",
            "ux",
            "completed" in tool_status and "steps" in tool_status,
            "the compact header shows completed/total steps",
        ),
        check(
            "ui_status_shows_error_detail",
            "ux",
            "failed" in tool_status and "task.detail" in tool_status,
            "failed tasks keep expandable error details",
        ),
        check(
            "ui_status_no_raw_json",
            "ux",
            "JSON.stringify" not in tool_status,
            "raw event JSON is never rendered in the chat",
        ),
        check(
            "ui_status_rendered_for_status_role",
            "ux",
            '"status"' in app or "'status'" in app,
            "App.tsx routes status messages to ToolStatus",
        ),
        check(
            "ui_status_handles_tool_result",
            "ux",
            "tool_result" in api,
            "the client consumes tool_result events",
        ),
        check(
            "event_contract_tool_started",
            "accuracy",
            '"phase": "tool_started"' in chat,
            "the backend emits tool_started before execution",
        ),
        check(
            "event_contract_tool_result",
            "accuracy",
            "_tool_result_event" in chat or '"type": "tool_result"' in chat,
            "the backend emits structured tool_result events",
        ),
        check(
            "ui_thinking_component_exists",
            "ux",
            bool(_read("frontend/src/components/ThinkingBlock.tsx").strip()),
            "frontend/src/components/ThinkingBlock.tsx",
        ),
        check(
            "ui_thinking_is_collapsible",
            "ux",
            "aria-expanded" in _read("frontend/src/components/ThinkingBlock.tsx")
            and "expanded" in _read("frontend/src/components/ThinkingBlock.tsx"),
            "the thought process collapses into a compact button",
        ),
        check(
            "ui_thinking_rendered_for_assistant",
            "ux",
            "ThinkingBlock" in _read("frontend/src/components/MessageItem.tsx"),
            "MessageItem renders the thought process above the answer",
        ),
        check(
            "event_contract_thinking",
            "accuracy",
            '"type": "assistant_thinking"' in chat,
            "the backend streams reasoning deltas",
        ),
        check(
            "metrics_include_thinking",
            "ux",
            '"thinking_ms"' in chat,
            "thinking duration is reported for analysis",
        ),
    ]
    return checks


# --------------------------------------------------------------------------- #
# Report helpers
# --------------------------------------------------------------------------- #
def experiment_record(
    experiment: Experiment,
    ctx: ExperimentContext,
    turn: TurnResult,
    evaluation: Evaluation,
    *,
    events_file: str | None = None,
    mutated: bool = False,
) -> dict[str, Any]:
    return {
        "id": experiment.id,
        "title": experiment.title,
        "category": experiment.category,
        "requires_gui": experiment.requires_gui,
        "mutated": mutated,
        "prompt": shorten(turn.prompt, 1000),
        "duration_s": round(turn.elapsed, 2),
        "final": shorten(turn.final_text(), 1200),
        "error": turn.error,
        "tools": turn.tool_names(),
        "tool_arguments": [
            {
                "tool": event.get("tool"),
                "arguments": event.get("arguments"),
                "message": event.get("message"),
            }
            for event in turn.tool_starts
        ],
        "metrics": turn.metrics_final,
        "score": evaluation.score,
        "dimensions": evaluation.dimensions(),
        "passed": evaluation.passed,
        "checks": [item.as_dict() for item in evaluation.checks],
        "events_file": events_file,
        "workspace": str(ctx.workspace),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for record in records if record["passed"])
    scores = [record["score"] for record in records]
    by_category: dict[str, dict[str, Any]] = {}
    for record in records:
        bucket = by_category.setdefault(
            record["category"],
            {"total": 0, "passed": 0, "score": 0.0},
        )
        bucket["total"] += 1
        bucket["passed"] += int(record["passed"])
        bucket["score"] += record["score"]

    for bucket in by_category.values():
        bucket["score"] = round(bucket["score"] / bucket["total"], 1) if bucket["total"] else 0.0

    dimension_totals: dict[str, list[float]] = {name: [] for name in DIMENSIONS}
    for record in records:
        for name, value in record["dimensions"].items():
            dimension_totals.setdefault(name, []).append(value)

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "success_rate": round(100.0 * passed / total, 1) if total else 0.0,
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "by_category": by_category,
        "dimensions": {
            name: round(sum(values) / len(values), 1)
            for name, values in dimension_totals.items()
            if values
        },
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"Run: {report['run_id']}  model={report.get('model') or 'unknown'}  "
        f"{report.get('base_url')}",
        f"Passed {summary['passed']}/{summary['total']} "
        f"({summary['success_rate']}%), average score {summary['average_score']}",
        "Dimensions: "
        + ", ".join(
            f"{name}={value}" for name, value in summary.get("dimensions", {}).items()
        ),
        "",
    ]

    for record in report["experiments"]:
        status = "PASS" if record["passed"] else "FAIL"
        lines.append(
            f"[{status}] {record['score']:5.1f}  {record['id']:<28} "
            f"{record['duration_s']:6.1f}s  {record['category']}"
        )
        if not record["passed"]:
            for item in record["checks"]:
                if not item["ok"] and item["dimension"] in DIMENSIONS:
                    lines.append(
                        f"        - {item['dimension']}/{item['name']}: "
                        f"{item['detail']}"
                    )
    return "\n".join(lines)


def failure_bundle(report: dict[str, Any]) -> dict[str, Any]:
    """Structured evidence for the builder agent's next patch."""
    failures = []
    for record in report["experiments"]:
        if record["passed"]:
            continue
        failed_checks = [
            item
            for item in record["checks"]
            if not item["ok"] and item["dimension"] in DIMENSIONS
        ]
        failures.append(
            {
                "id": record["id"],
                "title": record["title"],
                "category": record["category"],
                "score": record["score"],
                "dimensions": record["dimensions"],
                "prompt": record["prompt"],
                "final": record["final"],
                "error": record["error"],
                "tools": record["tools"],
                "tool_arguments": record["tool_arguments"],
                "failed_checks": failed_checks,
                "events_file": record["events_file"],
                "workspace": record["workspace"],
            }
        )

    focus: list[str] = []
    for failure in failures:
        for item in failure["failed_checks"]:
            focus.append(f"{failure['id']}:{item['dimension']}/{item['name']}")
    return {
        "run_id": report["run_id"],
        "model": report.get("model"),
        "summary": report["summary"],
        "failures": failures,
        "focus": focus,
    }


def render_failure_brief(bundle: dict[str, Any]) -> str:
    lines = [
        f"# Compa Phase 21 failures - {bundle['run_id']}",
        "",
        f"Summary: {bundle['summary']['passed']}/{bundle['summary']['total']} passed, "
        f"score {bundle['summary']['average_score']}",
        "",
    ]
    for failure in bundle["failures"]:
        lines.extend(
            [
                f"## {failure['id']} - {failure['title']}",
                f"- Prompt: {failure['prompt']}",
                f"- Tools: {', '.join(failure['tools']) or 'none'}",
                f"- Error: {failure['error'] or 'none'}",
                "- Failed checks:",
            ]
        )
        for item in failure["failed_checks"]:
            lines.append(
                f"  - `{item['dimension']}/{item['name']}`: {item['detail']}"
            )
        lines.extend(
            [
                f"- Events: `{failure['events_file']}`",
                f"- Workspace: `{failure['workspace']}`",
                "",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "audit_frontend",
    "evaluate_turn",
    "experiment_record",
    "failure_bundle",
    "load_report",
    "render_failure_brief",
    "render_summary",
    "summarize",
    "write_report",
]
