"""High-level GUI automation service used by the agent tools."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from settings import AutomationSettings
from tools.desktop_context import DesktopContextService
from tools.input_failsafe import (
    AutomationAbortedException,
    InputFailsafe,
)
from tools.mouse_control import SmoothPointer
from tools.som_vision import (
    SOMSnapshot,
    SOMParser,
    SnapshotStore,
    UIElement,
)

if TYPE_CHECKING:
    from tools.vision_tools import VisionService

logger = logging.getLogger(__name__)

_TEXT_CONTROL_TYPES = {
    "Edit",
    "Document",
    "RichEdit",
    "ComboBox",
    "Spinner",
}


def _parse_grounding(answer: str) -> dict[str, Any]:
    """Extract ``{"id": n, ...}`` from a small model's JSON-ish answer."""
    text = str(answer or "").strip()
    if not text:
        return {}

    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    candidates: list[str] = []
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    candidates.append(cleaned)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw_id = parsed.get("id")
        element_id = 0
        if isinstance(raw_id, (int, float)):
            element_id = int(raw_id)
        elif isinstance(raw_id, str):
            digits = re.search(r"\d+", raw_id)
            if digits:
                element_id = int(digits.group(0))
        confidence = parsed.get("confidence")
        try:
            confidence_value = (
                float(confidence) if confidence is not None else None
            )
        except (TypeError, ValueError):
            confidence_value = None
        return {
            "id": element_id,
            "reason": str(parsed.get("reason") or "").strip(),
            "confidence": confidence_value,
        }

    # Last resort: a bare integer is still a useful suggestion.
    digits = re.search(r"\b(\d+)\b", cleaned)
    if digits:
        return {"id": int(digits.group(1)), "reason": "", "confidence": None}
    return {}


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _names_match(requested: str, actual: str) -> bool:
    left = _normalize_name(requested)
    right = _normalize_name(actual)
    if not left:
        return True
    if not right:
        return False
    return left in right or right in left


def _encode_image(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


class GUIAutomationService:
    """Set-of-Mark analysis plus guarded pointer/keyboard actions."""

    def __init__(
        self,
        settings: AutomationSettings,
        vision: VisionService | None = None,
        *,
        parser: SOMParser | None = None,
        store: SnapshotStore | None = None,
        guard: InputFailsafe | None = None,
        pointer: SmoothPointer | None = None,
        grounder: Callable[[str, SOMSnapshot, str], dict[str, Any]] | None = None,
        context: DesktopContextService | None = None,
    ) -> None:
        self.settings = settings
        self.vision = vision
        self.parser = parser or SOMParser(settings)
        self.context = context or DesktopContextService(settings)
        self.store = store or getattr(self.parser, "store", None) or SnapshotStore()
        self.guard = guard or InputFailsafe(
            enabled=settings.enabled,
            arm_delay_seconds=settings.arm_delay_seconds,
            move_threshold_pixels=settings.move_threshold_pixels,
        )
        self.pointer = pointer or SmoothPointer(settings, self.guard)
        self._grounder = grounder
        self._last_analysis: dict[str, Any] | None = None
        # Typing is only allowed after a click focused a text-capable control.
        # Moving the cursor alone must never be treated as focus.
        self._focused = False
        self._focus_context: dict[str, Any] | None = None
        self._last_screenshot: str | None = None
        # After start_program, prefer the window belonging to the launched app
        # instead of whichever window happens to be active.
        self._preferred_window_title: str | None = None

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Clear the halt and stale snapshots for a new user turn."""
        self.guard.reset()
        self.store.clear()
        self._last_analysis = None
        self._focused = False
        self._focus_context = None
        self._last_screenshot = None
        self._preferred_window_title = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "guard": self.guard.status(),
            "snapshots": self.store.count,
            "has_analysis": self._last_analysis is not None,
            "preferred_window_title": self._preferred_window_title,
            "last_screenshot": self._last_screenshot,
            "focus_context": self._focus_context,
        }

    def get_context(self) -> dict[str, Any]:
        context = self.context.capture()
        return {"ok": True, **context.to_public(), "context_token": context.token}

    def visible_window_titles(self) -> set[str]:
        from tools.som_vision import _visible_windows

        return {
            str(getattr(window, "title", "") or "").strip().casefold()
            for window in _visible_windows()
            if str(getattr(window, "title", "") or "").strip()
        }

    def note_launched_program(
        self,
        program_name: str,
        executable_path: str | None = None,
        *,
        before_titles: set[str] | None = None,
    ) -> None:
        """Remember the visible window of a program the agent just launched."""
        import re
        from pathlib import Path

        from tools.som_vision import _visible_windows

        generic = {
            "exe",
            "com",
            "the",
            "app",
            "windows",
            "system32",
            "program",
            "files",
            "programfiles",
            "local",
            "roaming",
            "microsoft",
            "apps",
            "windowsapps",
            "user",
            "users",
            "desktop",
        }
        tokens: set[str] = set()
        for value in (program_name, executable_path or ""):
            text = str(value or "").strip()
            if not text:
                continue
            stem = Path(text).stem.casefold()
            if len(stem) >= 3 and stem not in generic:
                tokens.add(stem)
            # Only split a friendly program name, never the full executable
            # path: "C:\Windows\System32\notepad.exe" must not match a window
            # called "Windows Input Experience".
            if not re.search(r"[\\/]", text):
                for part in re.split(r"[^0-9a-z]+", text.casefold()):
                    if len(part) >= 3 and part not in generic:
                        tokens.add(part)
        if not tokens:
            return

        excluded = {"compa", "personal agent", "ai agent"}
        before = {
            str(title).strip().casefold()
            for title in (before_titles or set())
            if str(title).strip()
        }
        deadline = time.monotonic() + 3.0
        while True:
            try:
                windows = _visible_windows()
            except Exception:  # noqa: BLE001
                windows = []
            matches = []
            for window in windows:
                title = str(getattr(window, "title", "") or "").strip()
                folded = title.casefold()
                if not title or folded in excluded:
                    continue
                if any(token in folded for token in tokens):
                    matches.append(window)
            if matches:
                # Prefer a window that did not exist before the launch. Give a
                # new app a short grace period to create its top-level window
                # instead of immediately selecting an older instance.
                new_matches = [
                    window
                    for window in matches
                    if str(getattr(window, "title", "") or "").strip().casefold()
                    not in before
                ]
                candidates = new_matches
                if not candidates and before and time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                if not candidates:
                    candidates = matches

                chosen = min(
                    candidates,
                    key=lambda window: (
                        not bool(getattr(window, "isActive", False)),
                        len(str(window.title)),
                    ),
                )
                self._preferred_window_title = str(chosen.title).strip()
                logger.info(
                    "GUI automation will prefer %r for %r",
                    self._preferred_window_title,
                    program_name,
                )
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.2)

    # ------------------------------------------------------------- analysis
    def analyze(
        self,
        question: str,
        *,
        display: int | None = None,
        window_title: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            return {
                "ok": False,
                "error": (
                    "GUI automation is disabled in settings. Set "
                    "automation.enabled to true to use it."
                ),
            }
        if self.guard.halted:
            return self._halted_result()

        intent = str(question or "").strip()
        if not intent:
            return {
                "ok": False,
                "error": "A question or target description is required.",
            }

        requested_title = window_title or self._preferred_window_title
        try:
            result = self.parser.snapshot(
                display=display,
                window_title=requested_title,
            )
        except Exception as exc:  # noqa: BLE001 - report a readable tool error
            logger.exception("Set-of-Mark capture failed")
            return {
                "ok": False,
                "error": f"Set-of-Mark capture failed: {exc}",
            }

        # A stale preferred window must not block an otherwise valid request.
        if (
            not result.get("ok")
            and window_title is None
            and requested_title is not None
        ):
            self._preferred_window_title = None
            try:
                result = self.parser.snapshot(display=display, window_title=None)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Set-of-Mark capture retry failed")
                return {
                    "ok": False,
                    "error": f"Set-of-Mark capture failed: {exc}",
                }

        if not result.get("ok"):
            return result

        snapshot: SOMSnapshot = result["snapshot"]
        self.store.put(snapshot)
        self._last_analysis = snapshot.to_public()
        self._last_screenshot = snapshot.screenshot
        live_context = self.context.capture()

        base: dict[str, Any] = {
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "window_title": snapshot.window_title,
            "display": snapshot.display.to_public(),
            "window_handle": snapshot.window_handle,
            "process_id": snapshot.process_id,
            "process_name": snapshot.process_name,
            "context": live_context.to_public(),
            "context_token": live_context.token,
            "screenshot": snapshot.screenshot,
            "files": result.get("files") or [snapshot.screenshot],
            "elements": [item.to_public() for item in snapshot.elements],
            "coordinate_map": snapshot.coordinate_map(),
            "suggested_id": 0,
            "suggested_element": None,
            "confidence": None,
            "model_answer": None,
            "message": str(result.get("message") or ""),
        }

        if not snapshot.elements:
            base["next_step"] = (
                "No interactable elements were detected. Ask the user to "
                "bring the target window to the front, pass its exact "
                "window_title, or use the CLI instead. Do not guess "
                "coordinates."
            )
            return base

        try:
            image_b64 = _encode_image(snapshot.screenshot)
            grounding = self._ground(intent, snapshot, image_b64)
        except Exception as exc:  # noqa: BLE001 - grounding is best effort
            logger.warning("SoM grounding failed: %s", exc)
            base["grounding_error"] = str(exc)
            base["message"] = (
                f"{base['message']} Vision grounding failed, so no element "
                "was suggested. Choose from the element names if one is "
                "unambiguous, otherwise re-run or use the CLI."
            )
            return base

        if not grounding:
            grounding = {}
        if "model_answer" in grounding:
            base["model_answer"] = grounding.get("model_answer")

        try:
            suggested_id = int(grounding.get("id") or 0)
        except (TypeError, ValueError):
            suggested_id = 0
        element = snapshot.element(suggested_id) if suggested_id else None
        if element is None:
            suggested_id = 0

        base["suggested_id"] = suggested_id
        base["suggested_element"] = element.to_public() if element else None
        base["confidence"] = grounding.get("confidence")
        base["message"] = str(result.get("message") or "")

        if element is not None:
            base["message"] += (
                f" Vision suggests #{element.id} '{element.name}'"
                f" ({element.control_type})."
            )
            base["next_step"] = (
                "Verify that the suggested element name matches the user's "
                "intent. If it does, call click_element with "
                f"snapshot_id='{snapshot.snapshot_id}', "
                f"element_id={element.id}, and "
                f"element_name='{element.name}'. If it does not, choose the "
                "correct ID from elements. Never invent an ID and never "
                "reuse this snapshot after the UI changes."
            )
        else:
            base["message"] += (
                " The vision model did not suggest a confident element."
            )
            base["next_step"] = (
                f"The detected window was {snapshot.window_title!r}. If that "
                "is not the intended app, call get_open_windows and re-run "
                "analyze_screen_ui with its exact window_title; do not answer "
                "from the wrong window. Otherwise ask the user which element "
                "they mean, or re-run with a more specific target description. "
                "Do not guess an ID."
            )
        return base

    def _ground(
        self,
        question: str,
        snapshot: SOMSnapshot,
        image_b64: str,
    ) -> dict[str, Any]:
        if self._grounder is not None:
            result = self._grounder(question, snapshot, image_b64)
            if isinstance(result, str):
                parsed = _parse_grounding(result)
                parsed["model_answer"] = result
                return parsed
            return dict(result)
        if self.vision is None:
            raise RuntimeError(
                "No vision service is available for SoM grounding."
            )
        answer = self.vision.ground_ui(question, image_b64, snapshot.elements)
        parsed = _parse_grounding(answer)
        parsed["model_answer"] = answer
        return parsed

    # ------------------------------------------------------------- pointer
    def move(
        self,
        snapshot_id: str,
        element_id: int,
        element_name: str,
        *,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        snapshot, element, error = self._require_element(
            snapshot_id,
            element_id,
            element_name,
        )
        if error is not None:
            return error
        assert snapshot is not None and element is not None
        x, y = element.center

        try:
            with self.guard.execution():
                self.pointer.move_to(
                    x,
                    y,
                    duration=duration_seconds,
                )
        except AutomationAbortedException as exc:
            return self._aborted_result(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cursor move failed")
            return {"ok": False, "error": f"Could not move the cursor: {exc}"}

        return {
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "element": element.to_public(),
            "point": [x, y],
            "message": (
                f"Moved the cursor smoothly to #{element.id} "
                f"'{element.name}'."
            ),
        }

    def click(
        self,
        snapshot_id: str,
        element_id: int,
        element_name: str,
        *,
        button: str = "left",
        clicks: int = 1,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        snapshot, element, error = self._require_element(
            snapshot_id,
            element_id,
            element_name,
        )
        if error is not None:
            return error
        assert snapshot is not None and element is not None
        x, y = element.center

        try:
            with self.guard.execution():
                self.pointer.click(
                    x,
                    y,
                    button=button,
                    clicks=clicks,
                    duration=duration_seconds,
                )
        except AutomationAbortedException as exc:
            return self._aborted_result(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("UI click failed")
            return {"ok": False, "error": f"Could not click the element: {exc}"}

        self._focused = element.control_type in _TEXT_CONTROL_TYPES
        self._focus_context = (
            {
                "process_id": snapshot.process_id,
                "window_handle": snapshot.window_handle,
                "element_name": element.name,
            }
            if self._focused
            else None
        )
        return {
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "element": element.to_public(),
            "point": [x, y],
            "button": button,
            "clicks": clicks,
            "focused_text_field": self._focused,
            "message": (
                f"Clicked #{element.id} '{element.name}' "
                f"({button}, {clicks} time(s))."
            ),
            "next_step": (
                "If this was a text field, call keyboard_type_text now. "
                "Otherwise re-run analyze_screen_ui before the next click or "
                "before typing into a field that just appeared."
            ),
        }

    # ------------------------------------------------------------- keyboard
    def type_text(
        self,
        text: str,
        *,
        press_enter: bool = False,
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"ok": False, "error": "GUI automation is disabled."}
        if self.guard.halted:
            return self._halted_result()
        if not self._focused or self._focus_context is None:
            return {
                "ok": False,
                "needs_click": True,
                "error": (
                    "No text field was clicked, so typing would go to the "
                    "wrong place. Call click_element on the target Edit or "
                    "Document element first; moving the cursor does not focus "
                    "a field."
                ),
            }

        if self.settings.pre_action_validation:
            focus_ok, focus_kind, focused = self.context.validate_focus(
                self._focus_context
            )
            if not focus_ok:
                return {
                    "ok": False,
                    "focus_changed": True,
                    "error": (
                        "Keyboard focus is no longer on the approved text "
                        "field. Click the field again before typing."
                    ),
                    "focus_kind": focus_kind,
                    "focused_control": focused,
                    "fresh_context": self.context.capture().to_public(),
                    "screenshot": self._last_screenshot,
                    "next_step": (
                        "Call analyze_screen_ui again, click the target text "
                        "field, and then type. Do not type into whatever "
                        "happens to be focused."
                    ),
                }

        value = str(text or "")
        if not value:
            return {"ok": False, "error": "The text to type must not be empty."}
        if len(value) > 8000:
            return {
                "ok": False,
                "error": "Text is too long; type at most 8000 characters.",
            }

        try:
            with self.guard.execution():
                self.pointer.type_text(
                    value,
                    interval=interval_seconds,
                    press_enter=press_enter,
                )
        except AutomationAbortedException as exc:
            return self._aborted_result(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Keyboard typing failed")
            return {"ok": False, "error": f"Could not type the text: {exc}"}

        self._focused = True
        return {
            "ok": True,
            "characters": len(value),
            "pressed_enter": bool(press_enter),
            "message": (
                f"Typed {len(value)} character(s) into the focused field"
                + (" and pressed Enter." if press_enter else ".")
            ),
        }

    def press_keys(
        self,
        keys: Sequence[str],
        *,
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"ok": False, "error": "GUI automation is disabled."}
        if self.guard.halted:
            return self._halted_result()

        cleaned = [str(key).strip() for key in keys or [] if str(key).strip()]
        if not cleaned:
            return {"ok": False, "error": "At least one key is required."}

        try:
            with self.guard.execution():
                self.pointer.press_keys(
                    cleaned,
                    interval=interval_seconds,
                )
        except AutomationAbortedException as exc:
            return self._aborted_result(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Keyboard shortcut failed")
            return {"ok": False, "error": f"Could not press the keys: {exc}"}

        # Tab/Enter can move focus to another field; keep typing allowed.
        self._focused = True
        return {
            "ok": True,
            "keys": cleaned,
            "message": f"Pressed {'+'.join(cleaned)}.",
        }

    # ------------------------------------------------------------- helpers
    def _require_element(
        self,
        snapshot_id: str,
        element_id: int,
        element_name: str,
    ) -> tuple[SOMSnapshot | None, UIElement | None, dict[str, Any] | None]:
        if not self.settings.enabled:
            return None, None, {
                "ok": False,
                "error": "GUI automation is disabled.",
            }
        if self.guard.halted:
            return None, None, self._halted_result()

        snapshot = self.store.get(str(snapshot_id or ""))
        if snapshot is None:
            return None, None, {
                "ok": False,
                "error": (
                    "That SoM snapshot is missing or expired. Call "
                    "analyze_screen_ui again before clicking."
                ),
            }

        try:
            requested_id = int(element_id)
        except (TypeError, ValueError):
            return None, None, {
                "ok": False,
                "error": "element_id must be an integer from the element list.",
            }

        element = snapshot.element(requested_id)
        if element is None:
            available = ", ".join(str(item.id) for item in snapshot.elements[:20])
            return None, None, {
                "ok": False,
                "error": (
                    f"Element #{requested_id} is not in snapshot "
                    f"{snapshot.snapshot_id}. Available IDs: {available}."
                ),
            }

        if element_name and not _names_match(element_name, element.name):
            return None, None, {
                "ok": False,
                "error": (
                    f"Element #{element.id} is named '{element.name}', not "
                    f"'{element_name}'. Re-run analyze_screen_ui and use the "
                    "name from the current element list."
                ),
            }
        if not element.enabled:
            return None, None, {
                "ok": False,
                "error": (
                    f"Element #{element.id} '{element.name}' is disabled. "
                    "Do not click disabled controls."
                ),
            }

        if self.settings.pre_action_validation:
            ok, kind, reason, fresh, fresh_context = self.context.validate_snapshot(
                snapshot,
                element,
            )
            if not ok:
                return None, None, self._context_failure(
                    kind,
                    reason,
                    fresh_context,
                )
            if fresh:
                from dataclasses import replace

                element = replace(
                    element,
                    left=int(fresh["left"]),
                    top=int(fresh["top"]),
                    right=int(fresh["right"]),
                    bottom=int(fresh["bottom"]),
                    name=str(fresh.get("name") or element.name),
                )

        return snapshot, element, None

    def _context_failure(
        self,
        kind: str | None,
        reason: str | None,
        context: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": reason or "The live UI no longer matches the snapshot.",
            "fresh_context": context.to_public() if context is not None else None,
            "screenshot": self._last_screenshot,
            "next_step": (
                "The live UI changed. Call analyze_screen_ui again and use the "
                "new snapshot_id. Do not retry this stale element ID."
            ),
        }
        if kind == "element_missing":
            result["element_missing"] = True
        else:
            result["context_changed"] = True
        return result

    def _aborted_result(
        self,
        exc: AutomationAbortedException,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "aborted": True,
            "error": str(exc),
            "screenshot": self._last_screenshot,
            "message": (
                "Automation stopped because physical mouse or keyboard input "
                "was detected. Do not retry any GUI tool in this turn. Tell "
                "the user the automation was stopped and ask how to continue."
            ),
        }

    def _halted_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "aborted": True,
            "halted": True,
            "error": (
                self.guard.abort_reason
                or "Automation is halted until the user asks again."
            ),
            "message": (
                "GUI automation is halted to keep the user in control. Ask "
                "the user to confirm before trying again."
            ),
        }
