"""Cross-layer desktop context: windows, process tree, and UI focus.

The agent uses this before a high-impact action to answer three questions:

* Is the intended window still the foreground window?
* Is it the same process and rectangle as when the screenshot was taken?
* Is the target element still present, and is the focused control still a
  text field in that same process?

The module is deliberately read-only. It never moves the pointer or types.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psutil

from settings import AutomationSettings

logger = logging.getLogger(__name__)

_TEXT_CONTROL_TYPES = {
    "Edit",
    "Document",
    "RichEdit",
    "ComboBox",
    "Spinner",
}

_DIALOG_TITLES = (
    "error",
    "warning",
    "confirm",
    "save",
    "open file",
    "security",
    "permission",
    "update",
    "fehler",
    "warnung",
    "bestätigen",
)

_user32 = ctypes.windll.user32
try:
    _user32.SetProcessDpiAwarenessContext  # probe only
except AttributeError:
    pass

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - may already be set
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


_enable_dpi_awareness()


def _window_text(hwnd: int) -> str:
    length = int(_user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value.strip()


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_details(pid: int) -> dict[str, Any]:
    try:
        process = psutil.Process(pid)
        name = process.name()
        try:
            executable = process.exe()
        except (psutil.AccessDenied, OSError):
            executable = ""
        try:
            command_line = " ".join(process.cmdline()[:12])
        except (psutil.AccessDenied, OSError):
            command_line = ""
        return {
            "pid": int(pid),
            "name": name,
            "executable": executable,
            "command_line": command_line,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return {
            "pid": int(pid),
            "name": "",
            "executable": "",
            "command_line": "",
        }


def _process_chain(pid: int, limit: int = 6) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = pid
    for _ in range(limit):
        if current in seen or current <= 0:
            break
        seen.add(current)
        details = _process_details(current)
        chain.append(details)
        try:
            current = int(psutil.Process(current).ppid())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            break
    return chain


@dataclass(frozen=True)
class WindowContext:
    handle: int
    title: str
    process_id: int
    process_name: str
    executable: str
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool
    active: bool
    dialog_like: bool
    z_index: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    def to_public(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "title": self.title,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "executable": self.executable,
            "rect": [self.left, self.top, self.right, self.bottom],
            "minimized": self.minimized,
            "active": self.active,
            "dialog_like": self.dialog_like,
            "z_index": self.z_index,
        }


@dataclass(frozen=True)
class DesktopContext:
    active: WindowContext | None
    windows: tuple[WindowContext, ...]
    process_chain: tuple[dict[str, Any], ...]
    focused: dict[str, Any] | None
    captured_at: str

    def to_public(self, *, max_windows: int = 15) -> dict[str, Any]:
        return {
            "active": self.active.to_public() if self.active else None,
            "process_chain": list(self.process_chain),
            "focused": self.focused,
            "windows": [
                window.to_public()
                for window in self.windows[:max_windows]
            ],
            "dialogs": [
                window.to_public()
                for window in self.windows
                if window.dialog_like
            ][:8],
            "captured_at": self.captured_at,
        }

    @property
    def token(self) -> str:
        active = self.active
        if active is None:
            return ""
        raw = (
            f"{active.handle}|{active.title}|{active.process_id}|"
            f"{active.left},{active.top},{active.right},{active.bottom}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class DesktopContextService:
    """Read-only cross-layer context and pre-action validation."""

    def __init__(self, settings: AutomationSettings | None = None) -> None:
        self.settings = settings or AutomationSettings()

    def capture(self) -> DesktopContext:
        active_handle = int(_user32.GetForegroundWindow())
        windows: list[WindowContext] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                title = _window_text(hwnd)
                if not title:
                    return True
                pid = _process_id(hwnd)
                rect = _window_rect(hwnd)
                if rect is None:
                    return True
                info = _process_details(pid)
                class_name = _class_name(hwnd).casefold()
                title_folded = title.casefold()
                dialog_like = class_name == "#32770" or any(
                    marker in title_folded for marker in _DIALOG_TITLES
                )
                windows.append(
                    WindowContext(
                        handle=int(hwnd),
                        title=title,
                        process_id=pid,
                        process_name=info["name"],
                        executable=info["executable"],
                        left=rect[0],
                        top=rect[1],
                        right=rect[2],
                        bottom=rect[3],
                        minimized=bool(_user32.IsIconic(hwnd)),
                        active=int(hwnd) == active_handle,
                        dialog_like=dialog_like,
                        z_index=len(windows),
                    )
                )
            except Exception:  # noqa: BLE001 - a window can vanish mid-enum
                logger.debug("Window enumeration skipped one window", exc_info=True)
            return True

        _user32.EnumWindows(_ENUM_PROC(callback), 0)

        active = next(
            (window for window in windows if window.handle == active_handle),
            None,
        )
        focused = self.focused_control()
        chain = _process_chain(active.process_id) if active else []

        return DesktopContext(
            active=active,
            windows=tuple(windows),
            process_chain=tuple(chain),
            focused=focused,
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def focused_control(self) -> dict[str, Any] | None:
        """Return the UIA focused element, or None when it cannot be read."""
        try:
            from pywinauto.uia_defines import IUIA
            from pywinauto.uia_element_info import UIAElementInfo

            element = IUIA().iuia.GetFocusedElement()
            if element is None:
                return None
            info = UIAElementInfo(element)
            rectangle = getattr(info, "rectangle", None)
            rect = None
            if rectangle is not None:
                rect = [
                    int(getattr(rectangle, "left", 0)),
                    int(getattr(rectangle, "top", 0)),
                    int(getattr(rectangle, "right", 0)),
                    int(getattr(rectangle, "bottom", 0)),
                ]
            top_handle = None
            top = getattr(info, "top_level_parent", None)
            if top is not None:
                top_handle = int(getattr(top, "handle", 0) or 0) or None
            return {
                "control_type": str(getattr(info, "control_type", "") or ""),
                "name": str(getattr(info, "name", "") or ""),
                "automation_id": str(getattr(info, "automation_id", "") or ""),
                "class_name": str(getattr(info, "class_name", "") or ""),
                "process_id": int(getattr(info, "process_id", 0) or 0),
                "window_handle": top_handle,
                "rect": rect,
                "text_capable": str(getattr(info, "control_type", "") or "")
                in _TEXT_CONTROL_TYPES,
            }
        except Exception:  # noqa: BLE001 - focus can disappear or UIA can fail
            logger.debug("Could not read the focused UIA element", exc_info=True)
            return None

    def validate_snapshot(
        self,
        snapshot: Any,
        element: Any,
        *,
        tolerance_px: int | None = None,
    ) -> tuple[
        bool,
        str | None,
        str | None,
        dict[str, Any] | None,
        DesktopContext,
    ]:
        """Validate a planned element action against the live desktop.

        Returns ``(ok, failure_kind, reason, fresh_element, context)``. The
        failure kind is ``context_changed`` or ``element_missing``.
        """
        context = self.capture()
        hwnd = int(getattr(snapshot, "window_handle", 0) or 0)
        if not hwnd:
            # Visual-only snapshots have no UIA window to re-check.
            return True, None, None, None, context

        if not _user32.IsWindow(hwnd):
            return (
                False,
                "element_missing",
                "The target window was closed after the screenshot.",
                None,
                context,
            )
        if not _user32.IsWindowVisible(hwnd):
            return (
                False,
                "element_missing",
                "The target window is no longer visible.",
                None,
                context,
            )
        if _user32.IsIconic(hwnd):
            return (
                False,
                "context_changed",
                "The target window is minimized.",
                None,
                context,
            )

        foreground = int(_user32.GetForegroundWindow())
        if foreground != hwnd:
            # A topmost window can be clickable without being foreground. The
            # decisive check is what window owns the target pixel: if the point
            # is covered by another app, do not click.
            center = getattr(element, "center", None)
            if center is None:
                active_title = (
                    context.active.title if context.active else "another window"
                )
                return (
                    False,
                    "context_changed",
                    (
                        "The target window is no longer foreground; "
                        f"{active_title!r} is active."
                    ),
                    None,
                    context,
                )
            point = wintypes.POINT(int(center[0]), int(center[1]))
            hit = _user32.WindowFromPoint(point)
            root = _user32.GetAncestor(hit, 2) if hit else 0  # GA_ROOT
            if int(root or hit or 0) != hwnd:
                active_title = (
                    context.active.title if context.active else "another window"
                )
                return (
                    False,
                    "context_changed",
                    (
                        "The target element is covered by another window; "
                        f"{active_title!r} is active."
                    ),
                    None,
                    context,
                )

        title = _window_text(hwnd)
        expected_title = str(getattr(snapshot, "window_title", "") or "").strip()
        if expected_title and title.casefold() != expected_title.casefold():
            return (
                False,
                "context_changed",
                (
                    f"The window title changed from {expected_title!r} to "
                    f"{title!r}."
                ),
                None,
                context,
            )

        expected_pid = int(getattr(snapshot, "process_id", 0) or 0)
        if expected_pid and _process_id(hwnd) != expected_pid:
            return (
                False,
                "context_changed",
                "The window now belongs to a different process.",
                None,
                context,
            )

        expected_rect = getattr(snapshot, "window_rect", None)
        current_rect = _window_rect(hwnd)
        if expected_rect and current_rect:
            tolerance = (
                max(2, int(tolerance_px))
                if tolerance_px is not None
                else max(2, int(self.settings.validation_tolerance_px))
            )
            if any(
                abs(int(a) - int(b)) > tolerance
                for a, b in zip(expected_rect, current_rect)
            ):
                return (
                    False,
                    "context_changed",
                    (
                        "The window moved or resized after the screenshot; "
                        "the element coordinates may be stale."
                    ),
                    None,
                    context,
                )

        # A modal dialog disables its owner. Clicking the owner while the
        # dialog is open would silently do nothing (or hit the wrong layer).
        try:
            from pywinauto import Desktop

            owner_wrapper = (
                Desktop(backend="uia")
                .window(handle=int(hwnd))
                .wrapper_object()
            )
            if not owner_wrapper.is_enabled():
                return (
                    False,
                    "context_changed",
                    (
                        "The target window is disabled, usually because a "
                        "modal dialog is open. Handle the dialog first."
                    ),
                    None,
                    context,
                )
        except Exception:  # noqa: BLE001 - enabled state is best effort
            logger.debug("Could not read the target window enabled state")

        source = str(getattr(element, "source", "") or "")
        if source == "visual":
            return True, None, None, None, context

        fresh = self._relocate_element(hwnd, element)
        if fresh is None:
            return (
                False,
                "element_missing",
                (
                    f"Element #{getattr(element, 'id', '?')} "
                    f"{getattr(element, 'name', '')!r} is no longer present."
                ),
                None,
                context,
            )
        if fresh.get("moved"):
            return (
                False,
                "context_changed",
                (
                    f"Element #{getattr(element, 'id', '?')} moved after the "
                    "screenshot; re-analyze before clicking."
                ),
                None,
                context,
            )
        return True, None, None, fresh, context

    def _relocate_element(self, hwnd: int, element: Any) -> dict[str, Any] | None:
        from pywinauto import Desktop

        name = str(getattr(element, "name", "") or "").strip()
        if name.casefold().startswith("unnamed "):
            name = ""
        control_type = str(getattr(element, "control_type", "") or "").strip()
        expected_center = getattr(element, "center", None)
        tolerance = max(2, int(self.settings.validation_tolerance_px))

        try:
            wrapper = (
                Desktop(backend="uia")
                .window(handle=int(hwnd))
                .wrapper_object()
            )
            candidates = (
                wrapper.descendants(control_type=control_type)
                if control_type and control_type != "Visual"
                else wrapper.descendants()
            )
        except Exception:  # noqa: BLE001 - UIA may fail during a transition
            logger.debug("Could not re-read the UIA tree", exc_info=True)
            return None

        best: tuple[int, Any, Any] | None = None
        for candidate in candidates:
            try:
                info = candidate.element_info
                candidate_name = str(getattr(info, "name", "") or "").strip()
                if name and candidate_name.casefold() != name.casefold():
                    continue
                rect = candidate.rectangle()
                center = (
                    (int(rect.left) + int(rect.right)) // 2,
                    (int(rect.top) + int(rect.bottom)) // 2,
                )
                if expected_center:
                    delta = abs(center[0] - int(expected_center[0])) + abs(
                        center[1] - int(expected_center[1])
                    )
                else:
                    delta = 0
                if best is None or delta < best[0]:
                    best = (delta, candidate, rect)
            except Exception:  # noqa: BLE001
                continue

        if best is None:
            return None
        delta, candidate, rect = best
        if delta > tolerance:
            return {"moved": True, "delta": delta}
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
            "name": str(getattr(candidate.element_info, "name", "") or name),
            "control_type": control_type,
            "moved": False,
            "delta": delta,
        }

    def validate_focus(
        self,
        expected: dict[str, Any] | None,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        """Check that the focused control is still the approved text field."""
        if not expected:
            return False, "focus_changed", None

        focused = self.focused_control()
        if focused is None:
            return (
                False,
                "focus_changed",
                "The keyboard focus could not be verified; re-focus the field.",
            )

        expected_pid = int(expected.get("process_id") or 0)
        if expected_pid and int(focused.get("process_id") or 0) != expected_pid:
            return (
                False,
                "focus_changed",
                "Keyboard focus moved to a different application.",
            )

        expected_handle = int(expected.get("window_handle") or 0)
        focused_handle = int(focused.get("window_handle") or 0)
        if expected_handle and focused_handle and focused_handle != expected_handle:
            return (
                False,
                "focus_changed",
                "Keyboard focus moved to a different window.",
            )

        if not focused.get("text_capable"):
            return (
                False,
                "focus_changed",
                (
                    "The focused control is "
                    f"{focused.get('control_type') or 'unknown'}, not a text field."
                ),
            )
        return True, None, focused
