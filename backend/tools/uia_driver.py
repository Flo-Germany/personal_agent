"""Windows UI Automation driver: background tree queries and pattern actions.

This is the Phase 26 primary path. UIA calls run out-of-process, so they work
while the target window is unfocused, covered, minimised, or sitting on another
virtual desktop. Nothing here moves the physical cursor or steals focus.

Element ids are short-lived (`u1`, `u2`, ...). They stay valid until the next
tree query for that window, which keeps stale-id clicks impossible.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_SETTEXT = 0x000C
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
_SEND_TIMEOUT_MS = 2_000

_SEND_MESSAGE_TIMEOUT: Any = None


def _send_message_timeout_fn() -> Any:
    """Win32 SendMessageTimeoutW with explicit 64-bit argument types."""
    global _SEND_MESSAGE_TIMEOUT
    if _SEND_MESSAGE_TIMEOUT is None:
        function = _user32().SendMessageTimeoutW
        function.argtypes = [
            wt.HWND,
            wt.UINT,
            wt.WPARAM,
            wt.LPARAM,
            wt.UINT,
            wt.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        function.restype = ctypes.c_size_t
        _SEND_MESSAGE_TIMEOUT = function
    return _SEND_MESSAGE_TIMEOUT


def send_message_timeout(
    hwnd: int,
    message: int,
    wparam: int = 0,
    lparam: int = 0,
    *,
    timeout_ms: int = _SEND_TIMEOUT_MS,
) -> int:
    """Send a window message without risking an unbounded block.

    ``SendMessageW`` waits forever when the target application stops pumping
    messages (a hung renderer, a modal file dialog in a broken state). The
    agent then blocks a worker thread and the conversation lock with it.
    """
    result = ctypes.c_size_t()
    ok = _send_message_timeout_fn()(
        int(hwnd),
        int(message),
        int(wparam),
        int(lparam),
        SMTO_BLOCK | SMTO_ABORTIFHUNG,
        max(1, int(timeout_ms)),
        ctypes.byref(result),
    )
    if not ok:
        raise UiaError(
            f"Window {int(hwnd)} stopped responding to background input."
        )
    return int(result.value)

_MAX_NODES = 220
_MAX_DEPTH = 6

# Public name -> UIAutomation pattern name understood by pywinauto.
_PATTERN_NAMES: dict[str, str] = {
    "invoke": "Invoke",
    "value": "Value",
    "toggle": "Toggle",
    "select": "SelectionItem",
    "expand": "ExpandCollapse",
    "scroll": "ScrollItem",
    "text": "Text",
}


class UiaError(RuntimeError):
    """The window or element could not be used through UI Automation."""


@dataclass
class CachedElement:
    element_id: str
    wrapper: Any
    hwnd: int
    window_hwnd: int
    name: str
    control_type: str
    automation_id: str
    patterns: tuple[str, ...]
    created_at: float = field(default_factory=time.time)


def _user32():
    return ctypes.windll.user32


def cursor_position() -> tuple[int, int]:
    point = wt.POINT()
    _user32().GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


class _CursorGuard:
    """Observes the physical pointer so a run can prove it was not touched.

    It deliberately never restores the position: moving the cursor back would
    hijack a mouse the user is actively using. These drivers do not call any
    cursor API, so `moved` can only mean the user moved it during the call.
    """

    def __enter__(self) -> "_CursorGuard":
        self.before = cursor_position()
        self.after = self.before
        self.moved = False
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.after = cursor_position()
        self.moved = self.after != self.before


def _safe(attribute: str, wrapper: Any) -> Any:
    try:
        return getattr(wrapper, attribute)
    except Exception:  # noqa: BLE001 - unsupported patterns raise COM errors
        return None


def patterns_of(wrapper: Any) -> tuple[str, ...]:
    """Real UIA pattern support, queried through GetCurrentPattern.

    pywinauto's iface_* properties are lazy and never raise on access, so the
    only reliable probe is asking the element for each pattern id.
    """
    from pywinauto.uia_defines import get_elem_interface

    info = _safe("element_info", wrapper)
    raw = getattr(info, "element", None) if info is not None else None
    if raw is None:
        return ()
    found: list[str] = []
    for name, pattern in _PATTERN_NAMES.items():
        try:
            get_elem_interface(raw, pattern)
        except Exception:  # noqa: BLE001 - NoPatternInterfaceError and COM errors
            continue
        found.append(name)
    return tuple(found)


def _text(value: Any) -> str:
    return str(value or "")


def _rect(wrapper: Any) -> tuple[int, int, int, int] | None:
    try:
        rect = wrapper.rectangle()
    except Exception:  # noqa: BLE001
        return None
    try:
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:  # noqa: BLE001
        return None


class UiaDriver:
    """One tree cache per window, plus pattern-based actions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window_hwnd: int = 0
        self._window_title: str = ""
        self._cache: dict[str, CachedElement] = {}
        self._generation = 0

    # ------------------------------------------------------------ discovery
    def resolve_window(self, title: str | None = None, hwnd: int | None = None) -> dict[str, Any]:
        """Resolve a target window by handle or fuzzy title."""
        if hwnd:
            return self._window_info(int(hwnd))
        if not title:
            raise UiaError("Pass a window title or a window handle.")

        from tools.window_tools import _matching_windows  # local import: optional dep

        matches = _matching_windows(title)
        if not matches:
            raise UiaError(f"No visible window matches {title!r}.")
        requested = title.strip().casefold()
        best = min(
            matches,
            key=lambda window: (
                _text(getattr(window, "title", "")).strip().casefold() != requested,
                not bool(getattr(window, "isActive", False)),
                len(_text(getattr(window, "title", ""))),
            ),
        )
        handle = int(getattr(best, "_hWnd", 0) or 0)
        if not handle:
            raise UiaError(f"Could not obtain a window handle for {title!r}.")
        return self._window_info(handle)

    @staticmethod
    def _window_info(hwnd: int) -> dict[str, Any]:
        user32 = _user32()
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "hwnd": int(hwnd),
            "title": buffer.value,
            "class_name": class_name.value,
            "process_id": int(pid.value),
            "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
            "visible": bool(user32.IsWindowVisible(hwnd)),
        }

    @staticmethod
    def _wrapper(hwnd: int) -> Any:
        from pywinauto import Desktop

        try:
            return Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        except Exception as exc:  # noqa: BLE001
            raise UiaError(
                f"UI Automation cannot attach to window {hwnd}: {exc}"
            ) from exc

    # ---------------------------------------------------------------- tree
    def tree(
        self,
        *,
        title: str | None = None,
        hwnd: int | None = None,
        depth: int = 4,
        max_nodes: int = 120,
        include_offscreen: bool = True,
        query: str | None = None,
    ) -> dict[str, Any]:
        window = self.resolve_window(title, hwnd)
        handle = int(window["hwnd"])
        root = self._wrapper(handle)

        depth = max(1, min(int(depth or 4), _MAX_DEPTH))
        max_nodes = max(10, min(int(max_nodes or 120), _MAX_NODES))
        needle = (query or "").strip().casefold()

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._cache.clear()
            self._window_hwnd = handle
            self._window_title = window["title"]

        elements: list[dict[str, Any]] = []
        wrappers: dict[str, CachedElement] = {}
        queue: deque[tuple[Any, int, str | None]] = deque([(root, 0, None)])
        index = 0
        truncated = False

        while queue:
            wrapper, level, parent_id = queue.popleft()
            if len(elements) >= max_nodes:
                truncated = True
                break
            index += 1
            element_id = f"u{index}"
            info = _safe("element_info", wrapper)
            name = _text(getattr(info, "name", ""))
            control_type = _text(getattr(info, "control_type", ""))
            automation_id = _text(getattr(info, "automation_id", ""))
            patterns = patterns_of(wrapper)
            rect = _rect(wrapper)
            try:
                enabled = bool(wrapper.is_enabled())
            except Exception:  # noqa: BLE001
                enabled = True
            try:
                offscreen = bool(wrapper.is_offscreen())
            except Exception:  # noqa: BLE001
                offscreen = False

            if offscreen and not include_offscreen:
                continue
            matches_query = (
                not needle
                or needle in name.casefold()
                or needle in automation_id.casefold()
                or needle in control_type.casefold()
            )
            interactable = bool(patterns) or control_type.casefold() in {
                "button",
                "edit",
                "checkbox",
                "radio button",
                "combo box",
                "list item",
                "menu item",
                "tab item",
                "hyperlink",
                "slider",
            }

            element = {
                "id": element_id,
                "parent": parent_id,
                "depth": level,
                "name": name[:120],
                "control_type": control_type,
                "automation_id": automation_id[:80],
                "class_name": _text(getattr(info, "class_name", ""))[:80],
                "enabled": enabled,
                "offscreen": offscreen,
                "patterns": list(patterns),
                "interactable": interactable,
                "rect": list(rect) if rect else None,
            }
            if matches_query or not needle:
                elements.append(element)
            wrappers[element_id] = CachedElement(
                element_id=element_id,
                wrapper=wrapper,
                hwnd=int(getattr(info, "handle", 0) or 0),
                window_hwnd=handle,
                name=name,
                control_type=control_type,
                automation_id=automation_id,
                patterns=patterns,
            )

            if level < depth:
                try:
                    children = wrapper.children()
                except Exception:  # noqa: BLE001 - a branch can vanish mid-walk
                    children = []
                for child in children:
                    queue.append((child, level + 1, element_id))

        with self._lock:
            if generation == self._generation:
                self._cache = wrappers

        interactable_count = sum(1 for item in elements if item["interactable"])
        total = len(wrappers)
        message = (
            f"UI Automation tree for {window['title']!r}: "
            f"{len(elements)} element(s), {interactable_count} interactable."
            + (" Result truncated; raise max_nodes or narrow the query." if truncated else "")
        )
        if needle and not elements:
            sample = [
                item.name for item in wrappers.values() if item.name
            ][:8]
            message = (
                f"No element matched query {query!r}, but the window exposes "
                f"{total} node(s). Call ui_query again without a query filter "
                "to list them"
                + (f". Some names: {sample}" if sample else ".")
            )
        return {
            "ok": True,
            "window": window,
            "generation": generation,
            "elements": elements,
            "count": len(elements),
            "scanned": total,
            "interactable": interactable_count,
            "truncated": truncated,
            "input_mode": "background",
            "message": message,
        }

    # -------------------------------------------------------------- actions
    def _element(self, element_id: str) -> CachedElement:
        with self._lock:
            cached = self._cache.get(str(element_id))
        if cached is None:
            raise UiaError(
                f"Element {element_id!r} is unknown or stale. Call ui_query again "
                "and use the fresh id."
            )
        if time.time() - cached.created_at > 300:
            raise UiaError(
                f"Element {element_id!r} expired. Call ui_query again."
            )
        return cached

    def act(
        self,
        element_id: str,
        action: str,
        *,
        value: str | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> dict[str, Any]:
        cached = self._element(element_id)
        action = action.strip().lower()
        detail: dict[str, Any] = {
            "element_id": element_id,
            "action": action,
            "name": cached.name,
            "control_type": cached.control_type,
            "automation_id": cached.automation_id,
            "via": "",
        }
        with _CursorGuard() as guard:
            try:
                if action == "invoke":
                    detail["via"] = "invoke_pattern"
                    cached.wrapper.iface_invoke.Invoke()
                elif action == "set_value":
                    detail["via"] = "value_pattern"
                    cached.wrapper.iface_value.SetValue(str(value or ""))
                    detail["value"] = _text(cached.wrapper.iface_value.CurrentValue)
                elif action == "toggle":
                    detail["via"] = "toggle_pattern"
                    cached.wrapper.iface_toggle.Toggle()
                    detail["state"] = _text(cached.wrapper.iface_toggle.CurrentToggleState)
                elif action == "select":
                    detail["via"] = "selection_item_pattern"
                    cached.wrapper.iface_selection_item.Select()
                elif action == "expand":
                    detail["via"] = "expand_collapse_pattern"
                    cached.wrapper.iface_expand_collapse.Expand()
                elif action == "collapse":
                    detail["via"] = "expand_collapse_pattern"
                    cached.wrapper.iface_expand_collapse.Collapse()
                elif action == "scroll_into_view":
                    detail["via"] = "scroll_item_pattern"
                    cached.wrapper.iface_scroll_item.ScrollIntoView()
                elif action == "focus":
                    detail["via"] = "uia_set_focus"
                    cached.wrapper.set_focus()
                elif action == "get_text":
                    detail["via"] = "text_pattern"
                    detail["text"] = _text(
                        cached.wrapper.iface_text.DocumentRange.GetText(-1)
                    )[:4000]
                elif action == "click":
                    detail.update(self._background_click(cached, button, clicks))
                elif action == "type":
                    detail.update(self._type_into(cached, str(value or "")))
                else:
                    raise UiaError(
                        f"Unknown action {action!r}. Use invoke, set_value, toggle, "
                        "select, expand, collapse, scroll_into_view, focus, "
                        "get_text, click or type."
                    )
            except UiaError:
                raise
            except Exception as exc:  # noqa: BLE001 - COM errors are tool errors
                raise UiaError(
                    f"{action} on {cached.control_type or 'element'} "
                    f"{cached.name!r} failed: {exc}"
                ) from exc
        detail["cursor_moved"] = False
        detail["cursor_stable"] = not guard.moved
        detail["ok"] = True
        detail["message"] = (
            f"{action} on {cached.control_type or 'element'} "
            f"{cached.name!r} via {detail['via']} (background, no focus change)."
        )
        return detail

    def _background_click(
        self, cached: CachedElement, button: str, clicks: int
    ) -> dict[str, Any]:
        rect = _rect(cached.wrapper)
        if rect is None:
            raise UiaError(f"{cached.name!r} has no clickable rectangle.")
        x = (rect[0] + rect[2]) // 2
        y = (rect[1] + rect[3]) // 2
        target = cached.hwnd or cached.window_hwnd
        point = wt.POINT(x, y)
        if not _user32().ScreenToClient(target, ctypes.byref(point)):
            raise UiaError("Could not convert the element point to client coordinates.")
        lparam = (point.y << 16) | (point.x & 0xFFFF)
        down, up, flag = (
            (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON)
            if button == "right"
            else (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON)
        )
        for _ in range(max(1, min(int(clicks or 1), 2))):
            _user32().PostMessageW(target, down, flag, lparam)
            time.sleep(0.02)
            _user32().PostMessageW(target, up, 0, lparam)
            time.sleep(0.05)

        # Confirm the app reacted when it exposes a cheap observable (focus,
        # selection or value). Absence of a change is not an error: many clicks
        # open menus or navigate.
        return {
            "via": "post_message",
            "point": [x, y],
            "client_point": [point.x, point.y],
            "target_hwnd": int(target),
        }

    @staticmethod
    def _type_into(cached: CachedElement, text: str) -> dict[str, Any]:
        if "value" in cached.patterns:
            cached.wrapper.iface_value.SetValue(text)
            return {
                "via": "value_pattern",
                "value": _text(cached.wrapper.iface_value.CurrentValue),
            }
        if cached.hwnd:
            # WM_SETTEXT wants a pointer in lparam; the buffer stays alive for
            # the duration of the synchronous (timed) call.
            buffer = ctypes.create_unicode_buffer(text)
            send_message_timeout(cached.hwnd, WM_SETTEXT, 0, ctypes.addressof(buffer))
            return {"via": "wm_settext", "target_hwnd": cached.hwnd}
        raise UiaError(
            "This element exposes neither ValuePattern nor its own window "
            "handle, so background typing is not possible. Use the foreground "
            "path only if the user allows it."
        )
