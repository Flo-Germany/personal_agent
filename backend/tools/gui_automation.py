"""High-level GUI automation service used by the agent tools."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from settings import AutomationSettings
from tools.app_knowledge import guide_for
from tools.desktop_context import (
    DesktopContextService,
    _process_details,
    _process_id,
    force_foreground,
    foreground_handle,
)
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


def _is_transient_window(title: str) -> bool:
    """Popups/tooltips that must not become the working window."""
    text = str(title or "").strip().casefold()
    if not text:
        return True
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if compact in {"popuphost", "tooltip", "popup"}:
        return True
    return "tooltip" in compact or text.startswith("msctls_")


def _ground_text(value: str) -> str:
    text = re.sub(r"[^0-9a-z\u00c0-\u024f]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def _ground_name(value: str) -> str:
    """Normalize a control name for local matching.

    Accessibility names often carry shortcut hints or ellipses
    ("Explorer (Ctrl+Shift+E)", "Save as..."). Those tokens never appear in a
    user request, so they are removed before comparing.
    """
    text = re.sub(r"\([^)]*\)", " ", str(value or ""))
    text = re.sub(r"\.{2,}\s*$", "", text.strip())
    return _ground_text(text)


def _local_grounding(
    intent: str,
    elements: Sequence[UIElement],
) -> dict[str, Any] | None:
    """Match a well-labeled control by name before spending a vision call.

    Native controls expose exact names ("New Email", "Subject", "Save"), so a
    literal mention in the request is already a reliable signal. Only an
    unambiguous winner is returned; vague requests still go to the model.
    """
    intent_text = _ground_text(intent)
    if len(intent_text) < 2:
        return None

    scored: list[tuple[float, int, UIElement]] = []
    for element in elements:
        if not element.enabled:
            continue
        name_text = _ground_name(element.name)
        if not name_text or name_text.startswith("visual element"):
            continue

        if name_text in intent_text and (
            len(name_text) >= 5
            or re.search(
                rf"(?<![0-9a-z]){re.escape(name_text)}(?![0-9a-z])",
                intent_text,
            )
        ):
            # A literal name mention is the strongest signal.
            score = 0.9 + min(0.09, len(name_text) / 200.0)
        else:
            score = SequenceMatcher(None, intent_text, name_text).ratio()
            if score < 0.84:
                continue
        scored.append((score, len(name_text), element))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0]
    if len(scored) > 1 and scored[1][0] >= best[0] - 0.01:
        # Two equally good candidates: only the screenshot can decide.
        return None

    return {
        "id": best[2].id,
        "reason": "matched the element name without a vision call",
        "confidence": round(min(0.98, best[0]), 2),
    }


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
        # Human-like state awareness: what was done in this turn and what the
        # desktop looked like afterwards, used to break click loops.
        self._action_log: list[dict[str, Any]] = []
        # The window the agent is currently working in. Keyboard input is only
        # sent after this window is verifiably foreground.
        self._target_title: str | None = None
        self._target_hwnd: int | None = None
        self._target_pid: int | None = None

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Clear the halt and stale snapshots for a new user turn."""
        self.guard.reset()
        self.store.clear()
        reset_cache = getattr(self.parser, "reset", None)
        if callable(reset_cache):
            try:
                reset_cache()
            except Exception:  # noqa: BLE001 - cache reset must not fail a turn
                logger.debug("Could not reset the snapshot cache", exc_info=True)
        self._last_analysis = None
        self._focused = False
        self._focus_context = None
        self._last_screenshot = None
        self._preferred_window_title = None
        self._action_log = []
        self._target_title = None
        self._target_hwnd = None
        self._target_pid = None

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
        active = getattr(context, "active", None)
        app_windows = [
            window.title
            for window in (getattr(context, "windows", ()) or ())
            if active and window.process_id == active.process_id
        ]
        guide = guide_for(
            active.process_name if active else "",
            active.title if active else "",
            open_window_titles=app_windows,
        )
        return {
            "ok": True,
            **context.to_public(),
            "app": guide,
            "app_windows": app_windows[:8],
            "preferred_window_title": self._preferred_window_title,
            "recent_actions": self._recent_actions(),
            "context_token": context.token,
        }

    def visible_window_titles(self) -> set[str]:
        from tools.som_vision import _visible_windows

        return {
            str(getattr(window, "title", "") or "").strip().casefold()
            for window in _visible_windows()
            if str(getattr(window, "title", "") or "").strip()
        }

    def prefer_window(self, title: str) -> None:
        """Make the next analyze_screen_ui call target this window."""
        text = str(title or "").strip()
        if text:
            self._preferred_window_title = text

    def app_windows(
        self,
        program_name: str,
        executable_path: str | None = None,
    ) -> list[str]:
        """Visible windows that belong to an app, best candidate first."""
        from pathlib import Path

        from tools.app_knowledge import profile_for
        from tools.som_vision import _visible_windows

        profile = profile_for(
            Path(executable_path or program_name).name,
            "",
        )
        processes = {name.casefold() for name in profile.processes}
        stem = Path(str(program_name or "")).stem.casefold()
        tokens = {stem} if len(stem) >= 3 else set()
        excluded = {"personal agent", "compa"}

        matches: list[tuple[bool, str]] = []
        for window in _visible_windows():
            title = str(getattr(window, "title", "") or "").strip()
            if not title or title.casefold() in excluded:
                continue
            folded = title.casefold()
            active = bool(getattr(window, "isActive", False))
            if tokens and any(token in folded for token in tokens):
                matches.append((active, title))
                continue
            if profile.key == "generic":
                continue
            hwnd = int(getattr(window, "_hWnd", 0) or 0)
            pid = _process_id(hwnd) if hwnd else 0
            process_name = (
                str(_process_details(pid).get("name", "")).casefold()
                if pid
                else ""
            )
            if process_name in processes or any(
                re.search(pattern, title, re.IGNORECASE)
                for pattern in profile.title_patterns
            ):
                matches.append((active, title))

        matches.sort(key=lambda item: (not item[0], len(item[1])))
        return [title for _active, title in matches]

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

        from tools.app_knowledge import profile_for

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

        # Known apps are matched by process name and title pattern as well.
        # Outlook's window is called "Inbox - user@..." and contains neither
        # "outlook" nor "exe", so title tokens alone would miss it.
        profile = profile_for(
            Path(executable_path or program_name).name,
            "",
        )
        profile_processes = {
            name.casefold() for name in profile.processes
        }
        if not tokens and profile.key == "generic":
            return

        excluded = {"compa", "personal agent", "ai agent"}
        before = {
            str(title).strip().casefold()
            for title in (before_titles or set())
            if str(title).strip()
        }
        process_names: dict[int, str] = {}

        def matches_app(window: Any) -> bool:
            title = str(getattr(window, "title", "") or "").strip()
            folded = title.casefold()
            if not title or folded in excluded:
                return False
            if tokens and any(token in folded for token in tokens):
                return True
            if profile.key == "generic":
                return False
            hwnd = int(getattr(window, "_hWnd", 0) or 0)
            pid = _process_id(hwnd) if hwnd else 0
            if pid and pid not in process_names:
                process_names[pid] = str(
                    _process_details(pid).get("name", "")
                ).casefold()
            if pid and process_names.get(pid) in profile_processes:
                return True
            return any(
                re.search(pattern, title, re.IGNORECASE)
                for pattern in profile.title_patterns
            )

        deadline = time.monotonic() + 8.0
        while True:
            try:
                windows = _visible_windows()
            except Exception:  # noqa: BLE001
                windows = []
            matches = [window for window in windows if matches_app(window)]
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

    # ------------------------------------------------------ state awareness
    @staticmethod
    def _action_key(
        window_title: str,
        element: UIElement,
    ) -> tuple[Any, ...]:
        """Identify an action independently of the short-lived snapshot ID."""
        title = " ".join(str(window_title or "").casefold().split())
        name = " ".join(str(element.name or "").casefold().split())
        if element.source == "visual" or name.startswith("visual element"):
            center_x, center_y = element.center
            return ("visual", title, round(center_x / 24), round(center_y / 24))
        return (
            "uia",
            title,
            name,
            str(element.control_type or "").casefold(),
        )

    def _windows(self) -> dict[int, dict[str, Any]]:
        return self.context.quick_windows()

    def _wait_for_change(
        self,
        before: dict[int, dict[str, Any]],
        *,
        timeout: float = 0.9,
    ) -> dict[int, dict[str, Any]]:
        """Poll cheaply until the desktop changes, then stop waiting."""
        baseline = self.context.state_signature(before)
        deadline = time.monotonic() + max(0.0, timeout)
        current = before
        while time.monotonic() < deadline:
            time.sleep(0.06)
            current = self._windows()
            if self.context.state_signature(current) != baseline:
                return current
        return self._windows()

    @staticmethod
    def _window_brief(item: dict[str, Any]) -> dict[str, Any]:
        details = _process_details(int(item.get("process_id") or 0))
        return {
            "title": item.get("title"),
            "handle": item.get("handle"),
            "process_name": details.get("name", ""),
            "process_id": item.get("process_id"),
            "active": bool(item.get("active")),
            "minimized": bool(item.get("minimized")),
        }

    def _describe_change(
        self,
        before: dict[int, dict[str, Any]],
        after: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        opened = [
            self._window_brief(after[handle])
            for handle in after.keys() - before.keys()
        ]
        closed = [
            {
                "title": before[handle].get("title"),
                "process_id": before[handle].get("process_id"),
            }
            for handle in before.keys() - after.keys()
        ]
        retitled = [
            {
                "from": before[handle].get("title"),
                "to": after[handle].get("title"),
            }
            for handle in before.keys() & after.keys()
            if before[handle].get("title") != after[handle].get("title")
        ]
        active = next(
            (item for item in after.values() if item.get("active")),
            None,
        )
        focus = (
            {"title": active.get("title"), "process_id": active.get("process_id")}
            if active
            else None
        )
        changed = bool(opened or closed or retitled)
        before_focus = next(
            (item for item in before.values() if item.get("active")),
            None,
        )
        focus_changed = bool(
            (before_focus or {}).get("handle") != (active or {}).get("handle")
            or (before_focus or {}).get("title") != (active or {}).get("title")
        )
        focused_control = getattr(self.context, "focused_control", lambda: None)()
        return {
            "ui_changed": changed or focus_changed,
            "window_opened": opened[:6],
            "window_closed": closed[:6],
            "title_changed": retitled[:6],
            "focus": focus,
            "focused_control": (
                {
                    "name": focused_control.get("name"),
                    "control_type": focused_control.get("control_type"),
                    "text_capable": focused_control.get("text_capable"),
                }
                if focused_control
                else None
            ),
            "state_token": self.context.state_signature(after),
        }

    def _record_action(
        self,
        tool: str,
        window_title: str,
        element: UIElement | None,
        delta: dict[str, Any],
        *,
        detail: str = "",
    ) -> None:
        entry = {
            "tool": tool,
            "window": str(window_title or ""),
            "name": str(getattr(element, "name", "") or detail),
            "key": self._action_key(window_title, element) if element else None,
            "state": delta.get("state_token"),
            "delta": delta,
            "at": time.monotonic(),
        }
        self._action_log.append(entry)
        if len(self._action_log) > 24:
            del self._action_log[:-24]

    def _prefer_opened_window(
        self,
        snapshot: SOMSnapshot,
        delta: dict[str, Any],
    ) -> None:
        """Follow a newly opened window like a human would."""
        for change in delta.get("title_changed") or []:
            if (
                self._preferred_window_title
                and change.get("from") == self._preferred_window_title
                and change.get("to")
            ):
                self._preferred_window_title = str(change["to"])

        opened = [
            item
            for item in delta.get("window_opened") or []
            if item.get("title") and not _is_transient_window(item["title"])
        ]
        if not opened:
            return

        same_process = [
            item
            for item in opened
            if snapshot.process_id
            and int(item.get("process_id") or 0) == int(snapshot.process_id)
        ]
        candidates = same_process or opened
        chosen = min(
            candidates,
            key=lambda item: (
                not bool(item.get("active")),
                bool(item.get("minimized")),
            ),
        )
        self._preferred_window_title = str(chosen["title"])
        self._set_target(
            str(chosen["title"]),
            int(chosen.get("handle") or 0) or None,
            int(chosen.get("process_id") or 0) or None,
        )
        logger.info(
            "GUI automation now prefers the opened window %r",
            self._preferred_window_title,
        )

    def _loop_result(
        self,
        snapshot: SOMSnapshot,
        element: UIElement,
        current: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Refuse to repeat a click that produced no state change."""
        if element.control_type in _TEXT_CONTROL_TYPES:
            # Clicking a text field again is how a person refocuses it; it is
            # idempotent and must not be blocked by the loop guard.
            return None
        signature = self.context.state_signature(current)
        key = self._action_key(snapshot.window_title, element)
        for entry in reversed(self._action_log[-6:]):
            if entry.get("key") == key and entry.get("state") == signature:
                return {
                    "ok": False,
                    "loop_detected": True,
                    "element": element.to_public(),
                    "window_title": snapshot.window_title,
                    "error": (
                        f"'{element.name}' was already clicked in "
                        f"{snapshot.window_title or 'this window'} and the desktop "
                        "has not changed since. Do not click it again."
                    ),
                    "recent_actions": self._recent_actions(),
                    "next_step": (
                        "Do not repeat this click. If the expected window opened, "
                        "work in that window (analyze_screen_ui with its title). "
                        "Otherwise use the app's keyboard shortcut, wait for the "
                        "app to finish, or ask the user."
                    ),
                }
        return None

    def _set_target(
        self,
        title: str | None,
        hwnd: int | None,
        pid: int | None,
    ) -> None:
        if title:
            self._target_title = str(title)
        if hwnd:
            self._target_hwnd = int(hwnd)
            focused_hwnd = int(
                (self._focus_context or {}).get("window_handle") or 0
            )
            if focused_hwnd and focused_hwnd != int(hwnd):
                # The clicked field belongs to a different window than the one
                # we just analyzed; do not treat it as focused anymore.
                self._focused = False
                self._focus_context = None
        if pid:
            self._target_pid = int(pid)

    def _ensure_input_target(self) -> tuple[bool, str | None]:
        """Bring the working window forward; never type into another app."""
        if not self.settings.verify_foreground:
            return True, None
        hwnd = int(
            (self._focus_context or {}).get("window_handle")
            or self._target_hwnd
            or 0
        )
        if not hwnd:
            return False, (
                "No target window is known for this turn. Call "
                "analyze_screen_ui on the window first; keyboard input is not "
                "sent blindly to whatever happens to be focused."
            )
        if foreground_handle() == hwnd:
            return True, None
        if force_foreground(hwnd) and foreground_handle() == hwnd:
            return True, None
        title = self._target_title or "the target window"
        return False, (
            f"Could not bring {title!r} to the foreground, so keyboard input "
            "was not sent. Move or click into the window, then retry."
        )

    def _recent_actions(self, limit: int = 4) -> list[str]:
        notes: list[str] = []
        for entry in self._action_log[-limit:]:
            delta = entry.get("delta") or {}
            text = f"{entry.get('tool')} '{entry.get('name')}' in '{entry.get('window')}'"
            opened = [
                item.get("title")
                for item in delta.get("window_opened") or []
                if item.get("title")
            ]
            retitled = delta.get("title_changed") or []
            if opened:
                text += " -> opened: " + ", ".join(opened[:3])
            elif retitled:
                text += " -> title: " + ", ".join(
                    f"{item.get('from')} => {item.get('to')}"
                    for item in retitled[:2]
                )
            elif delta.get("ui_changed") is False:
                text += " -> no visible change"
            notes.append(text)
        return notes

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
        self._set_target(
            snapshot.window_title,
            snapshot.window_handle,
            snapshot.process_id,
        )
        window_foreground = (
            bool(snapshot.window_handle)
            and foreground_handle() == int(snapshot.window_handle)
        )

        app_windows = [
            window.title
            for window in (getattr(live_context, "windows", ()) or ())
            if snapshot.process_id and window.process_id == snapshot.process_id
        ]
        if snapshot.window_title and snapshot.window_title not in app_windows:
            app_windows.insert(0, snapshot.window_title)
        guide = guide_for(
            snapshot.process_name,
            snapshot.window_title,
            open_window_titles=app_windows,
        )

        base: dict[str, Any] = {
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "window_title": snapshot.window_title,
            "display": snapshot.display.to_public(),
            "window_handle": snapshot.window_handle,
            "process_id": snapshot.process_id,
            "process_name": snapshot.process_name,
            "window_foreground": window_foreground,
            "app": guide,
            "app_windows": app_windows[:8],
            "preferred_window_title": self._preferred_window_title,
            "recent_actions": self._recent_actions(),
            "context": live_context.to_public(),
            "context_token": live_context.token,
            "screenshot": snapshot.screenshot,
            "files": result.get("files") or [snapshot.screenshot],
            "reused": bool(result.get("reused")),
            "elements": [item.to_public() for item in snapshot.elements],
            "coordinate_map": snapshot.coordinate_map(),
            "suggested_id": 0,
            "suggested_element": None,
            "confidence": None,
            "grounding": "none",
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

        grounding: dict[str, Any] | None = None
        grounding_source = "none"
        if self.settings.local_grounding:
            grounding = _local_grounding(intent, snapshot.elements)
            if grounding is not None:
                grounding_source = "local"

        if grounding is None:
            try:
                image_b64 = _encode_image(snapshot.screenshot)
                grounding = self._ground(intent, snapshot, image_b64)
                grounding_source = "vision"
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
        base["grounding"] = grounding_source

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

        warning = str(guide.get("warning") or "").strip()
        if warning:
            base["message"] += f" {warning}"

        if element is not None:
            source_label = (
                "Name match" if grounding_source == "local" else "Vision"
            )
            base["message"] += (
                f" {source_label} suggests #{element.id} '{element.name}'"
                f" ({element.control_type})."
            )
            base["next_step"] = (
                "Verify that the suggested element name matches the user's "
                "intent. If it does, call click_element with "
                f"snapshot_id='{snapshot.snapshot_id}', "
                f"element_id={element.id}, and "
                f"element_name='{element.name}'. If it does not, choose the "
                "correct ID from elements. Never invent an ID. If the layout "
                "did not change after the last action, you may reuse this "
                "snapshot for the next element; the backend re-validates the "
                "live window and element positions before acting."
            )
            shortcuts = guide.get("shortcuts") or {}
            if shortcuts:
                base["next_step"] += (
                    " Prefer the app shortcut when it fits: "
                    + ", ".join(
                        f"{name} = {combo}"
                        for name, combo in list(shortcuts.items())[:6]
                    )
                    + "."
                )
            if warning:
                base["next_step"] += (
                    " Do not open a second window: " + warning
                )
            if (
                (snapshot.process_name or "").casefold() == "explorer.exe"
                and "address" in element.name.casefold()
                and "bar" in element.name.casefold()
            ):
                base["next_step"] = (
                    "Do not click the breadcrumb address bar: it opens a pop-up "
                    "and does not focus the edit. Call keyboard_press_keys with "
                    "['ctrl','l'] to focus the address bar, then "
                    "keyboard_type_text with the full path and press_enter=true."
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
        # Only controls with a numbered box are visible to the vision model.
        # The rest of the element list is still available for local name
        # matching and for the planner.
        visible = tuple(
            item
            for item in snapshot.elements
            if not snapshot.annotated_ids or item.id in snapshot.annotated_ids
        )
        answer = self.vision.ground_ui(question, image_b64, visible)
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

        before = self._windows()
        loop = self._loop_result(snapshot, element, before)
        if loop is not None:
            return loop

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

        after = self._wait_for_change(
            before,
            timeout=self.settings.action_settle_seconds,
        )
        delta = self._describe_change(before, after)
        self._record_action(
            "click",
            snapshot.window_title,
            element,
            delta,
        )
        self._prefer_opened_window(snapshot, delta)

        live_focus = self.context.focused_control()
        text_capable = bool(
            live_focus
            and live_focus.get("text_capable")
            and (
                not snapshot.process_id
                or int(live_focus.get("process_id") or 0) == int(snapshot.process_id)
            )
        )

        # Explorer's breadcrumb address bar opens a pop-up instead of focusing
        # the edit. Ctrl+L is the reliable way to reach it, exactly as a person
        # would. Recover once, inside the same approved click.
        if (
            not text_capable
            and "address" in element.name.casefold()
            and "bar" in element.name.casefold()
        ):
            try:
                with self.guard.synthetic():
                    self.pointer.press_keys(["ctrl", "l"], interval=0)
                time.sleep(0.12)
                live_focus = self.context.focused_control()
                text_capable = bool(
                    live_focus
                    and live_focus.get("text_capable")
                    and (
                        not snapshot.process_id
                        or int(live_focus.get("process_id") or 0)
                        == int(snapshot.process_id)
                    )
                )
                if text_capable:
                    delta["focus_recovered"] = "ctrl+l"
                    delta["focused_control"] = {
                        "name": live_focus.get("name"),
                        "control_type": live_focus.get("control_type"),
                        "text_capable": True,
                    }
            except Exception:  # noqa: BLE001 - focus recovery is best effort
                logger.debug("Address-bar focus recovery failed", exc_info=True)

        if text_capable and live_focus:
            self._focused = True
            self._focus_context = {
                "process_id": int(live_focus.get("process_id") or 0),
                "window_handle": int(live_focus.get("window_handle") or 0) or None,
                "element_name": str(
                    live_focus.get("name") or element.name
                ),
            }
        else:
            self._focused = False
            self._focus_context = None

        opened_titles = [
            item.get("title")
            for item in delta.get("window_opened") or []
            if item.get("title") and not _is_transient_window(item["title"])
        ]
        if opened_titles:
            next_step = (
                "A new window opened: "
                + ", ".join(f"'{title}'" for title in opened_titles[:3])
                + ". Do not click the same control again. Work in that window: "
                "call analyze_screen_ui with its exact title (or without a "
                "window_title, which now targets it)."
            )
        elif not delta.get("ui_changed"):
            next_step = (
                "No visible change followed. Do not repeat this click. Check "
                "the app shortcut in app.shortcuts, wait for the application, "
                "or ask the user."
            )
        elif self._focused:
            next_step = "Call keyboard_type_text now to fill this field."
        else:
            next_step = (
                "Continue with the next element from the same snapshot if the "
                "layout did not change; re-run analyze_screen_ui after a "
                "navigation, dialog, or resize."
            )

        return {
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "element": element.to_public(),
            "point": [x, y],
            "button": button,
            "clicks": clicks,
            "focused_text_field": self._focused,
            "outcome": delta,
            "recent_actions": self._recent_actions(),
            "message": (
                f"Clicked #{element.id} '{element.name}' "
                f"({button}, {clicks} time(s))."
            ),
            "next_step": next_step,
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

        target_ok, target_error = self._ensure_input_target()
        if not target_ok:
            return {
                "ok": False,
                "focus_changed": True,
                "error": target_error,
                "fresh_context": self.context.capture().to_public(),
                "screenshot": self._last_screenshot,
                "next_step": (
                    "Call analyze_screen_ui on the target window, then click "
                    "the text field before typing."
                ),
            }

        if not self._focused or self._focus_context is None:
            return {
                "ok": False,
                "needs_click": True,
                "error": (
                    "No text field was clicked or keyboard-focused, so typing "
                    "would go to the wrong place. Click the target Edit or "
                    "Document element first, or focus it with the app shortcut "
                    "(for example Ctrl+L in a browser) and check "
                    "outcome.focused_control."
                ),
            }

        if self.settings.pre_action_validation:
            focus_ok, focus_kind, focused = self.context.validate_focus(
                self._focus_context
            )
            if not focus_ok:
                # Explorer and browsers often focus a child edit inside a
                # popup (the address bar dropdown). Accept that when the live
                # focus is a text field in the same process with the same name.
                live = self.context.focused_control()
                expected = self._focus_context or {}
                expected_pid = int(expected.get("process_id") or 0)
                if (
                    live
                    and live.get("text_capable")
                    and (
                        not expected_pid
                        or int(live.get("process_id") or 0) == expected_pid
                    )
                    and _names_match(
                        str(expected.get("element_name") or ""),
                        str(live.get("name") or ""),
                    )
                ):
                    expected["element_name"] = str(live.get("name") or "")
                    self._focus_context = expected
                    focus_ok = True
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
                            "Call analyze_screen_ui again, click the target "
                            "text field, and then type. Do not type into "
                            "whatever happens to be focused."
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

        before = self._windows()
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

        after = self._wait_for_change(
            before,
            timeout=(
                self.settings.action_settle_seconds
                if press_enter
                else min(self.settings.action_settle_seconds, 0.35)
            ),
        )
        delta = self._describe_change(before, after)
        active_title = (delta.get("focus") or {}).get("title") or ""
        self._record_action(
            "type",
            active_title,
            None,
            delta,
            detail=f"typed {len(value)} characters",
        )
        self._focused = True
        return {
            "ok": True,
            "characters": len(value),
            "pressed_enter": bool(press_enter),
            "outcome": delta,
            "recent_actions": self._recent_actions(),
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

        target_ok, target_error = self._ensure_input_target()
        if not target_ok:
            return {
                "ok": False,
                "focus_changed": True,
                "error": target_error,
                "fresh_context": self.context.capture().to_public(),
                "screenshot": self._last_screenshot,
                "next_step": (
                    "Analyze the target window or click into it, then retry the "
                    "shortcut."
                ),
            }

        before = self._windows()
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

        after = self._wait_for_change(
            before,
            timeout=self.settings.action_settle_seconds,
        )
        delta = self._describe_change(before, after)
        active = next(
            (item for item in before.values() if item.get("active")),
            {},
        )
        self._record_action(
            "keys",
            str(active.get("title") or ""),
            None,
            delta,
            detail="+".join(cleaned),
        )

        opened_titles = [
            item.get("title")
            for item in delta.get("window_opened") or []
            if item.get("title") and not _is_transient_window(item["title"])
        ]
        next_step = None
        if opened_titles:
            self._preferred_window_title = str(opened_titles[0])
            next_step = (
                "A new window opened: "
                + ", ".join(f"'{title}'" for title in opened_titles[:3])
                + ". Call analyze_screen_ui with its exact title (or without a "
                "window_title) before the next action."
            )

        # A shortcut such as Ctrl+L or Tab can move focus into another field.
        # Only allow typing afterwards when the live focus really is a text
        # control in the window we are working in.
        focused = self.context.focused_control()
        if (
            focused
            and focused.get("text_capable")
            and (
                not self._target_pid
                or int(focused.get("process_id") or 0) == self._target_pid
            )
        ):
            self._focused = True
            self._focus_context = {
                "process_id": int(focused.get("process_id") or 0),
                "window_handle": int(focused.get("window_handle") or 0) or None,
                "element_name": str(focused.get("name") or ""),
            }
        else:
            self._focused = False
            self._focus_context = None
        result: dict[str, Any] = {
            "ok": True,
            "keys": cleaned,
            "outcome": delta,
            "recent_actions": self._recent_actions(),
            "message": f"Pressed {'+'.join(cleaned)}.",
        }
        if next_step:
            result["next_step"] = next_step
        return result

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
