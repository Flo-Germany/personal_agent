"""Turn failures into durable, deduplicated operating knowledge.

The service is deliberately deterministic: it classifies tool failures from
their structured result, and only writes a lesson after a later successful
action proves that a recovery worked. It never learns from a user-triggered
abort, and it never stores secrets or raw screenshots in the knowledge base.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from memory.knowledge import KnowledgeBase

logger = logging.getLogger(__name__)

_MAX_FACT_CHARS = 280
_GUI_TOOLS = {
    "analyze_screen_ui",
    "click_element",
    "move_cursor_to",
    "keyboard_type_text",
    "keyboard_press_keys",
}


@dataclass(frozen=True)
class FailureRecord:
    conversation_id: str
    tool: str
    arguments: dict[str, Any]
    classification: str
    strategy: str
    error: str
    screenshot: str | None
    created_at: float

    def to_public(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "classification": self.classification,
            "strategy": self.strategy,
            "error": self.error[:300],
            "screenshot": self.screenshot,
        }


def _one_line(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _summarize_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "click_element": ("element_name", "element_id", "snapshot_id"),
        "move_cursor_to": ("element_name", "element_id", "snapshot_id"),
        "analyze_screen_ui": ("question", "window_title"),
        "keyboard_type_text": ("text", "press_enter"),
        "keyboard_press_keys": ("keys",),
        "start_program": ("program_name",),
        "open_file": ("path", "program"),
        "search_system": ("root", "name_contains", "extension"),
        "create_folder": ("path",),
        "create_file": ("path",),
        "move_file_or_folder": ("source_path", "destination_path"),
        "copy_file": ("source_path", "destination_path"),
        "delete_file_or_folder": ("path",),
        "run_powershell_action": ("description", "command"),
        "run_powershell_query": ("command",),
        "get_desktop_context": (),
        "get_open_windows": (),
        "analyze_screen": ("user_question", "display"),
        "read_file_content": ("path",),
        "semantic_image_search": ("query",),
        "system_diagnosis": (),
        "propose_plan": ("steps",),
    }
    selected = {}
    for key in keys.get(tool, ()):
        if key not in arguments:
            continue
        value = arguments[key]
        if key == "text":
            selected[key] = _one_line(value, 60)
        elif key == "command":
            selected[key] = _one_line(value, 100)
        elif key == "steps" and isinstance(value, list):
            selected[key] = [_one_line(step, 50) for step in value[:5]]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            selected[key] = _one_line(value, 100) if isinstance(value, str) else value
    return selected


class SelfImprovementService:
    """Track failures, successful recoveries, and reusable workflows."""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        *,
        enabled: bool = True,
        min_tools_for_workflow: int = 3,
    ) -> None:
        self.knowledge = knowledge
        self.enabled = bool(enabled)
        self.min_tools_for_workflow = max(1, int(min_tools_for_workflow))
        self._lock = threading.RLock()
        self._pending: dict[str, FailureRecord] = {}
        self._turn_tools: dict[str, list[dict[str, Any]]] = {}
        self._lessons_saved = 0

    def begin_turn(self, conversation_id: str) -> None:
        with self._lock:
            self._pending.pop(conversation_id, None)
            self._turn_tools[conversation_id] = []

    def record_tool_result(
        self,
        conversation_id: str,
        tool: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> None:
        if not self.enabled:
            return
        result = result or {}
        arguments = arguments or {}
        ok = bool(result.get("ok"))
        summary = _summarize_arguments(str(tool), arguments)

        with self._lock:
            tools = self._turn_tools.setdefault(conversation_id, [])

            if ok:
                tools.append({"tool": tool, "arguments": summary})
                pending = self._pending.pop(conversation_id, None)
                if pending is not None:
                    lesson = self._recovery_lesson(pending, tool, summary)
                    if lesson:
                        self._save_fact(lesson)
                return

            if result.get("aborted"):
                # The human took control. Never learn a workaround around the
                # physical-input killswitch.
                self._pending.pop(conversation_id, None)
                return

            record = self._classify(
                conversation_id,
                str(tool),
                arguments,
                result,
                summary,
            )
            self._pending[conversation_id] = record

    def finish_turn(
        self,
        conversation_id: str,
        user_message: str,
        final_reply: str,
        *,
        success: bool,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            pending = self._pending.pop(conversation_id, None)
            tools = self._turn_tools.pop(conversation_id, [])

        if not success or pending is not None:
            return
        if len(tools) < self.min_tools_for_workflow:
            return
        lesson = self._workflow_lesson(user_message, tools)
        if lesson:
            self._save_fact(lesson)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "lessons_saved": self._lessons_saved,
                "pending_failures": {
                    key: value.to_public()
                    for key, value in self._pending.items()
                },
            }

    # ------------------------------------------------------------- classify
    def _classify(
        self,
        conversation_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        summary: dict[str, Any],
    ) -> FailureRecord:
        error = _one_line(
            result.get("error")
            or result.get("message")
            or "The tool action failed.",
            300,
        )
        lowered = error.casefold()

        if result.get("context_changed") or "context changed" in lowered:
            classification = "ui_context_shift"
            strategy = (
                "The UI moved or another window became active. Re-run "
                "analyze_screen_ui, verify the new window/process, and click "
                "with the fresh snapshot instead of retrying the stale ID."
            )
        elif result.get("element_missing") or "no longer present" in lowered:
            classification = "ui_element_missing"
            strategy = (
                "The element disappeared. Re-run analyze_screen_ui, choose the "
                "element from the new list, and only then click."
            )
        elif result.get("focus_changed") or result.get("needs_click") or (
            "focus" in lowered and "text" in lowered
        ):
            classification = "ui_focus_shift"
            strategy = (
                "Keyboard focus changed. Click the target text field with a "
                "fresh snapshot before typing; moving the cursor is not enough."
            )
        elif "no visible window matches" in lowered or "window was closed" in lowered:
            classification = "window_missing"
            strategy = (
                "Call get_open_windows and pass the exact current window title "
                "to analyze_screen_ui; newly launched apps may need a short wait."
            )
        elif "expired" in lowered or "snapshot is missing" in lowered:
            classification = "stale_snapshot"
            strategy = (
                "Snapshots are turn-local and short-lived. Run analyze_screen_ui "
                "again before using element IDs."
            )
        elif "disabled" in lowered:
            classification = "disabled_control"
            strategy = (
                "Do not click disabled controls. Choose an enabled element, "
                "use the menu/keyboard route, or use the CLI."
            )
        elif str(tool).startswith("run_powershell") and (
            result.get("exit_code") not in (None, 0)
            or "failed" in lowered
            or "error" in lowered
        ):
            classification = "cli_error"
            strategy = (
                "Inspect stderr, exit_code, and hint; re-resolve the exact path "
                "or PID with a read-only query, then retry the corrected command."
            )
        else:
            classification = "tool_error"
            strategy = (
                f"Inspect the {tool} result and verify the live state before "
                "trying a different safe approach; do not repeat the same call."
            )

        return FailureRecord(
            conversation_id=conversation_id,
            tool=str(tool),
            arguments=summary,
            classification=classification,
            strategy=strategy,
            error=error,
            screenshot=str(result.get("screenshot") or "") or None,
            created_at=time.time(),
        )

    def _recovery_lesson(
        self,
        failure: FailureRecord,
        success_tool: str,
        success_arguments: dict[str, Any],
    ) -> str:
        if failure.classification not in {
            "ui_context_shift",
            "ui_element_missing",
            "ui_focus_shift",
            "window_missing",
            "stale_snapshot",
            "disabled_control",
            "cli_error",
            "tool_error",
        }:
            return ""
        if failure.classification == "cli_error":
            lesson = (
                "Learned recovery: after a PowerShell failure, inspect "
                "stderr/exit_code, re-resolve the exact path or PID, then retry "
                "the corrected command."
            )
        elif failure.classification == "window_missing":
            lesson = (
                "Learned recovery: after a window mismatch, call "
                "get_open_windows, then pass the exact current title to "
                "analyze_screen_ui."
            )
        else:
            lesson = (
                f"Learned recovery: after {failure.tool} failed with "
                f"{failure.classification.replace('_', ' ')}, "
                f"{failure.strategy} Verified with {success_tool}."
            )
        return self._clean_fact(lesson)

    def _workflow_lesson(
        self,
        user_message: str,
        tools: list[dict[str, Any]],
    ) -> str:
        steps: list[str] = []
        for entry in tools:
            tool = str(entry.get("tool") or "")
            if not tool or tool == "propose_plan":
                continue
            args = entry.get("arguments") or {}
            detail = ""
            for key in (
                "program_name",
                "window_title",
                "element_name",
                "path",
                "query",
                "description",
            ):
                value = args.get(key)
                if value:
                    detail = f"({_one_line(value, 36)})"
                    break
            label = f"{tool}{detail}"
            if not steps or steps[-1].split("(", 1)[0] != tool:
                steps.append(label)
            if len(steps) >= 8:
                break
        # finish_turn already required enough successful raw calls. A workflow
        # with one repeated tool is not useful; two distinct stages are.
        if len(steps) < 2:
            return ""
        task = _one_line(user_message, 70)
        task = re.sub(r"[\"'`]+", "", task).strip()
        if len(task) >= 70 and " " in task:
            task = task.rsplit(" ", 1)[0]
        lesson = f"Workflow: for '{task}', use " + " -> ".join(steps) + "."
        return self._clean_fact(lesson)

    def _clean_fact(self, fact: str) -> str:
        text = " ".join(str(fact).split())
        # Never persist obvious credentials or long paths of secrets.
        text = re.sub(
            r"(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}",
            "[redacted]",
            text,
        )
        if len(text) > _MAX_FACT_CHARS:
            text = text[: _MAX_FACT_CHARS - 3].rstrip() + "..."
        return text

    def _save_fact(self, fact: str) -> None:
        if not fact:
            return
        try:
            added = self.knowledge.add_facts(
                {"learned notes": [fact]},
                deletions=["(none)", "(none recorded)"],
            )
        except Exception:  # noqa: BLE001 - learning must not break a turn
            logger.exception("Could not save a self-improvement fact")
            return
        if added:
            with self._lock:
                self._lessons_saved += len(added)
            logger.info("Saved self-improvement fact: %s", fact)
