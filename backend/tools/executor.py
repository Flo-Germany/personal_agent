"""Validated dispatch from the model's tool calls to Python OS tools."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from settings import AutomationSettings, ToolSettings
from tools import os_tools, system_diagnosis, system_tools, window_tools
from tools.gui_automation import GUIAutomationService
from tools.schemas import TOOL_SCHEMAS

if TYPE_CHECKING:
    from tools.vision_tools import VisionService

logger = logging.getLogger(__name__)

_SEARCH_BOOLEAN_ALIASES = ("folders_only", "files_only", "include_folders")


def _normalize_search_aliases(parsed: dict[str, Any]) -> dict[str, Any]:
    """Map guessed boolean flags onto the schema's explicit ``kind`` field.

    Small local models sometimes send ``folders_only: true`` or
    ``include_folders: false`` instead of ``kind``. Accepting both keeps a
    folder search working without teaching the schema three overlapping knobs.
    Explicit ``kind`` always wins.
    """
    if "kind" in parsed:
        return {
            key: value
            for key, value in parsed.items()
            if key not in _SEARCH_BOOLEAN_ALIASES
        }

    normalized = dict(parsed)
    folders_only = normalized.pop("folders_only", None)
    files_only = normalized.pop("files_only", None)
    include_folders = normalized.pop("include_folders", None)

    if folders_only:
        normalized["kind"] = "folders"
    elif files_only:
        normalized["kind"] = "files"
    elif include_folders is False:
        normalized["kind"] = "files"
    return normalized


class ToolExecutor:
    """Bind tool names to their implementations and HITL flags."""

    def __init__(
        self,
        settings: ToolSettings,
        vision: VisionService | None = None,
        automation: AutomationSettings | None = None,
        automation_service: GUIAutomationService | None = None,
    ) -> None:
        self.settings = settings
        self.vision = vision
        self.automation = automation_service or GUIAutomationService(
            automation or AutomationSettings(),
            vision=vision,
        )
        self._parameters: dict[str, dict[str, Any]] = {
            schema["function"]["name"]: schema["function"].get("parameters", {})
            for schema in TOOL_SCHEMAS
        }

        tool_settings = settings

        def search(
            root: str = str(Path.home()),
            max_results: int | None = None,
            sort_by: str = "modified_desc",
            **kwargs: Any,
        ) -> dict[str, Any]:
            # Settings own the scan/time caps; the model may ask for fewer results.
            for key in ("max_scanned", "max_seconds"):
                kwargs.pop(key, None)

            allowed_filters = {
                "name_contains",
                "extension",
                "kind",
                "max_depth",
                "modified_after",
                "modified_before",
                "min_size",
                "max_size",
                "include_hidden",
                "include_junk",
                "all_drives",
                "recursive",
            }
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in allowed_filters
            }

            if max_results in (None, "", 0, "0"):
                requested = tool_settings.max_search_results
            else:
                try:
                    requested = int(max_results)
                except (TypeError, ValueError):
                    requested = tool_settings.max_search_results

            effective_results = max(
                1,
                min(requested, tool_settings.max_search_results),
            )
            return os_tools.search_system(
                root,
                max_results=effective_results,
                max_scanned=tool_settings.max_search_files,
                max_seconds=tool_settings.max_search_seconds,
                sort_by=sort_by,
                **kwargs,
            )

        def semantic_image_search(
            query: str,
            limit: int = 5,
            **_ignored: Any,
        ) -> dict[str, Any]:
            if vision is None:
                return {
                    "ok": False,
                    "error": "Semantic image search is not available.",
                }
            return vision.search(query, limit=limit)

        def analyze_screen(
            user_question: str,
            display: int | None = None,
            _screen_context: dict[str, Any] | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            if vision is None:
                return {
                    "ok": False,
                    "error": "Screen analysis is not available.",
                }
            context = _screen_context or {}
            return vision.analyze_screen(
                user_question,
                display=display,
                system_prompt=context.get("system_prompt"),
                tools=context.get("tools"),
            )

        def analyze_screen_ui(
            question: str,
            display: int | None = None,
            window_title: str | None = None,
            _screen_context: dict[str, Any] | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            # The screen context is accepted for parity with analyze_screen;
            # SoM grounding uses its own short JSON instruction.
            return self.automation.analyze(
                question,
                display=display,
                window_title=window_title,
            )

        def move_cursor_to(
            snapshot_id: str,
            element_id: int,
            element_name: str,
            duration_seconds: float | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            return self.automation.move(
                snapshot_id,
                element_id,
                element_name,
                duration_seconds=duration_seconds,
            )

        def click_element(
            snapshot_id: str,
            element_id: int,
            element_name: str,
            button: str = "left",
            clicks: int = 1,
            duration_seconds: float | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            return self.automation.click(
                snapshot_id,
                element_id,
                element_name,
                button=button,
                clicks=clicks,
                duration_seconds=duration_seconds,
            )

        def keyboard_type_text(
            text: str,
            press_enter: bool = False,
            interval_seconds: float | None = None,
            recipient: str | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            return self.automation.type_text(
                text,
                press_enter=press_enter,
                interval_seconds=interval_seconds,
                recipient=recipient,
            )

        def keyboard_press_keys(
            keys: list[str],
            interval_seconds: float | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            return self.automation.press_keys(
                keys,
                interval_seconds=interval_seconds,
            )

        def start_program(program_name: str) -> dict[str, Any]:
            finder = getattr(self.automation, "app_windows", None)
            if callable(finder):
                try:
                    existing = finder(program_name)
                except Exception:  # noqa: BLE001 - detection is best effort
                    existing = []
                if existing:
                    prefer = getattr(self.automation, "prefer_window", None)
                    if callable(prefer):
                        prefer(existing[0])
                    return {
                        "ok": True,
                        "already_running": True,
                        "windows": existing[:8],
                        "message": (
                            f"{program_name} is already running; use the "
                            f"existing window '{existing[0]}' instead of "
                            "opening another instance."
                        ),
                        "next_step": (
                            "Call analyze_screen_ui with that exact window "
                            "title. For a new document, use the app shortcut "
                            "(for example Ctrl+N) inside the existing window."
                        ),
                    }

            before: set[str] = set()
            titles = getattr(self.automation, "visible_window_titles", None)
            if callable(titles):
                try:
                    before = set(titles())
                except Exception:  # noqa: BLE001 - hint is best effort
                    before = set()

            result = os_tools.start_program(program_name)
            if result.get("ok"):
                note = getattr(self.automation, "note_launched_program", None)
                if callable(note):
                    note(
                        program_name,
                        result.get("program"),
                        before_titles=before,
                    )
            return result

        def propose_plan(
            steps: list[str] | None = None,
            **_ignored: Any,
        ) -> dict[str, Any]:
            cleaned: list[str] = []
            for step in steps or []:
                text = " ".join(str(step).split())
                if text:
                    cleaned.append(text[:240])
                if len(cleaned) >= 8:
                    break
            if not cleaned:
                return {
                    "ok": False,
                    "error": "The plan must contain at least one step.",
                }
            return {"ok": True, "steps": cleaned, "message": "Plan recorded."}

        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "propose_plan": propose_plan,
            "run_powershell_query": (
                lambda command: system_tools.run_powershell_query(
                    command,
                    timeout=tool_settings.powershell_timeout_seconds,
                    max_output_chars=tool_settings.powershell_max_output_chars,
                )
            ),
            "run_powershell_action": (
                lambda command, description: system_tools.run_powershell_action(
                    command,
                    description=description,
                    timeout=tool_settings.powershell_timeout_seconds,
                    max_output_chars=tool_settings.powershell_max_output_chars,
                )
            ),
            "start_program": start_program,
            "open_file": lambda path, program, **_ignored: os_tools.open_file(
                path,
                program,
            ),
            "get_open_windows": lambda **_ignored: window_tools.get_open_windows(),
            "read_window_content": (
                lambda window_title, **_ignored: window_tools.read_window_content(
                    window_title
                )
            ),
            "analyze_screen": analyze_screen,
            "get_desktop_context": lambda **_ignored: self.automation.get_context(),
            "analyze_screen_ui": analyze_screen_ui,
            "move_cursor_to": move_cursor_to,
            "click_element": click_element,
            "keyboard_type_text": keyboard_type_text,
            "keyboard_press_keys": keyboard_press_keys,
            "create_folder": lambda path: os_tools.create_folder(path),
            "create_file": lambda path, content, overwrite=False: os_tools.create_file(
                path,
                content,
                overwrite=overwrite,
            ),
            "copy_file": lambda source_path, destination_path: os_tools.copy_file(
                source_path,
                destination_path,
            ),
            "move_file_or_folder": lambda source_path, destination_path: (
                os_tools.move_file_or_folder(source_path, destination_path)
            ),
            "delete_file_or_folder": lambda path: os_tools.delete_file_or_folder(path),
            "read_file_content": lambda path: os_tools.read_file_content(
                path,
                max_bytes=tool_settings.max_read_bytes,
                redact_secrets=tool_settings.redact_secrets,
            ),
            "unzip_file": lambda archive_path, extract_to: os_tools.unzip_file(
                archive_path,
                extract_to,
                max_bytes=tool_settings.max_unzip_bytes,
            ),
            "search_system": search,
            "semantic_image_search": semantic_image_search,
            "system_diagnosis": (
                lambda sample_seconds=4.0, include_benchmark=True: (
                    system_diagnosis.run_diagnosis(
                        sample_seconds=sample_seconds,
                        include_benchmark=include_benchmark,
                    )
                )
            ),
        }

    def requires_confirmation(self, name: str) -> bool:
        return os_tools.requires_confirmation(name)

    def reset_automation(self) -> None:
        """Clear the killswitch halt and stale SoM snapshots for a new turn."""
        self.automation.reset()

    def release_vision(self) -> None:
        """Unload a native vision server after a screen task (keep_alive 0)."""
        release = getattr(self.vision, "release_native_vision", None)
        if callable(release):
            release()

    def automation_status(self) -> dict[str, Any]:
        return self.automation.status()

    def display_choices(self) -> list[dict[str, Any]]:
        """Return displays with previews, or an empty list without a vision service."""
        if self.vision is None:
            return []

        choices = getattr(self.vision, "display_choices", None)
        if choices is None:
            return []

        try:
            return list(choices())
        except Exception:  # noqa: BLE001 - display probing must not fail a turn
            logger.debug("Display enumeration failed", exc_info=True)
            return []

    def execute(
        self,
        name: str,
        arguments: str | dict[str, Any],
        *,
        screen_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one tool call, log it, and return a JSON-serializable result."""
        started = time.perf_counter()
        result = self._dispatch(
            name,
            arguments,
            screen_context=screen_context,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        ok = bool(result.get("ok"))
        log = logger.info if ok else logger.warning
        log(
            "tool_execution name=%s ok=%s duration_ms=%.1f",
            name,
            ok,
            duration_ms,
            extra={
                "tool": name,
                "ok": ok,
                "duration_ms": round(duration_ms, 1),
            },
        )
        return result

    def _dispatch(
        self,
        name: str,
        arguments: str | dict[str, Any],
        *,
        screen_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": f"Invalid JSON arguments for {name}: {exc}",
                }
        else:
            parsed = arguments

        if not isinstance(parsed, dict):
            return {"ok": False, "error": f"Arguments for {name} must be an object."}

        if name == "search_system":
            parsed = _normalize_search_aliases(parsed)

        schema = self._parameters.get(name)
        if schema:
            allowed = set(schema.get("properties", {}))
            parsed = {key: value for key, value in parsed.items() if key in allowed}
            required = schema.get("required", [])
            missing = [key for key in required if key not in parsed]
            if missing:
                joined = ", ".join(missing)
                return {
                    "ok": False,
                    "error": f"Missing required argument(s) for {name}: {joined}",
                }

        try:
            if name in {"analyze_screen", "analyze_screen_ui"}:
                return handler(**parsed, _screen_context=screen_context)
            return handler(**parsed)
        except TypeError as exc:
            return {"ok": False, "error": f"Invalid arguments for {name}: {exc}"}
        except os_tools.ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except window_tools.WindowToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the model
            logger.exception("Tool %s failed", name)
            return {"ok": False, "error": f"{name} failed: {exc}"}
