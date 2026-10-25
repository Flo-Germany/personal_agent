"""UIA-grounded Set-of-Mark UI parsing for desktop automation.

Every marked element comes from one of two auditable sources:

* **UIA** (primary): a real node in the Windows accessibility tree with a
  verified rectangle. Only definitively interactive control types, or elements
  exposing the UIA Invoke pattern / keyboard focus, are kept. Plain text
  blocks and structural containers are rejected.
* **OCR** (fallback, only when UIA exposes almost nothing): text lines found by
  RapidOCR and passed through strict filters (at most four words, natural
  aspect ratio, plausible control size). Pixel-only contour guessing is gone.

Every snapshot produces:
  * an annotated screenshot with a unique number over each element,
  * a numbered element list with control type, verification source and absolute
    screen rectangle,
  * a compact coordinate map (ID -> center x/y),
  * a short-lived ``snapshot_id`` used by click/move tools.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from settings import AutomationSettings

logger = logging.getLogger(__name__)


def _enable_dpi_awareness() -> None:
    """Use physical pixels for UIA rectangles and pointer coordinates."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - may already be set by pywinauto
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001 - best effort
            pass


_enable_dpi_awareness()

# Control types whose semantics are unambiguously interactive. These are
# accepted without extra COM pattern queries.
_DEFINITELY_INTERACTIVE = {
    "Button",
    "SplitButton",
    "MenuItem",
    "Hyperlink",
    "CheckBox",
    "RadioButton",
    "Edit",
    "ComboBox",
    "TabItem",
    "ListItem",
    "TreeItem",
    "DataItem",
    "Slider",
    "Spinner",
    "Document",
    "RichEdit",
}

# Structural containers and decorations. They may be keyboard focusable, but
# clicking them targets the container, not a control.
_NEVER_INTERACTABLE = {
    "Window",
    "Dialog",
    "TitleBar",
    "StatusBar",
    "ScrollBar",
    "Separator",
    "Thumb",
    "ProgressBar",
    "Header",
    "HeaderItem",
    "ToolBar",
    "MenuBar",
    "Menu",
    "List",
    "Tree",
    "Tab",
    "Table",
    "GroupBox",
}

# Types that are only accepted when they expose Invoke (text links, custom
# image buttons) or, for generic custom surfaces, keyboard focus.
_CONDITIONAL_INTERACTABLE = {
    "Text",
    "Image",
    "Group",
    "Pane",
    "Custom",
    "DataGrid",
    "TableItem",
}

# Plausible geometry for conditional controls (pixels). This is what keeps
# large editor panes, separators, and status-bar containers out.
_CONDITIONAL_MIN_SIDE = 8
_CONDITIONAL_MAX_WIDTH = 700
_CONDITIONAL_MAX_HEIGHT = 220
_CONDITIONAL_MIN_ASPECT = 0.15
_CONDITIONAL_MAX_ASPECT = 14.0

_SEMANTIC_PRIORITY = {
    "Button": 0,
    "SplitButton": 0,
    "Hyperlink": 0,
    "MenuItem": 0,
    "CheckBox": 0,
    "RadioButton": 0,
    "Edit": 0,
    "Document": 0,
    "RichEdit": 0,
    "ComboBox": 0,
    "TabItem": 0,
    "TreeItem": 0,
    "ListItem": 0,
    "DataItem": 0,
    "Slider": 0,
    "Spinner": 0,
    "OcrText": 1,
    "Text": 2,
    "Image": 2,
    "Group": 3,
    "Pane": 3,
    "Custom": 3,
}


def _is_verified_interactable(
    control_type: str,
    *,
    invoke: bool,
    focusable: bool,
    width: int,
    height: int,
) -> tuple[bool, str]:
    """Return ``(accepted, verified_by)`` for one UIA node.

    The allowlist covers well-known controls. Other nodes are accepted only
    with a real Invoke pattern (or, for generic custom surfaces, keyboard
    focus) and a plausible control-sized rectangle. This rejects plain text
    blocks, editor panes, separators, and other containers.
    """
    kind = str(control_type or "").strip()
    if not kind or kind in _NEVER_INTERACTABLE:
        return False, ""
    if kind in _DEFINITELY_INTERACTIVE:
        return True, "type"
    if kind not in _CONDITIONAL_INTERACTABLE:
        # Unknown custom UIA types are treated like custom surfaces.
        if not (invoke or focusable):
            return False, ""
    if kind in {"Text", "Image"} and not invoke:
        return False, ""
    if not (invoke or focusable):
        return False, ""
    if width < _CONDITIONAL_MIN_SIDE or height < _CONDITIONAL_MIN_SIDE:
        return False, ""
    if width > _CONDITIONAL_MAX_WIDTH or height > _CONDITIONAL_MAX_HEIGHT:
        return False, ""
    aspect = width / max(1, height)
    if aspect < _CONDITIONAL_MIN_ASPECT or aspect > _CONDITIONAL_MAX_ASPECT:
        return False, ""
    return True, ("invoke" if invoke else "focus")


@dataclass(frozen=True)
class DetectedElement:
    """A verified candidate before it is numbered and persisted."""

    name: str
    control_type: str
    automation_id: str
    left: int
    top: int
    right: int
    bottom: int
    enabled: bool = True
    source: str = "uia"
    verified_by: str = "type"


_TYPE_COLORS = {
    "Button": (255, 99, 71),
    "SplitButton": (255, 99, 71),
    "Edit": (64, 156, 255),
    "ComboBox": (64, 156, 255),
    "CheckBox": (52, 211, 153),
    "RadioButton": (52, 211, 153),
    "MenuItem": (167, 139, 250),
    "TabItem": (167, 139, 250),
    "ListItem": (251, 191, 36),
    "DataItem": (251, 191, 36),
    "TreeItem": (251, 191, 36),
    "Hyperlink": (56, 189, 248),
    "Slider": (244, 114, 182),
    "Spinner": (244, 114, 182),
    "Document": (64, 156, 255),
    "RichEdit": (64, 156, 255),
    "OcrText": (251, 146, 60),
}
_DEFAULT_COLOR = (251, 191, 36)

# Which controls get a numbered box in the annotated screenshot. Input fields
# are the most valuable visual targets; ribbon/toolbar buttons are usually
# better handled by name (local grounding) or an app shortcut.
_ANNOTATION_PRIORITY = {
    "Edit": 0,
    "Document": 0,
    "RichEdit": 0,
    "ComboBox": 0,
    "CheckBox": 0,
    "RadioButton": 0,
    "OcrText": 0,
    "ListItem": 1,
    "TreeItem": 1,
    "DataItem": 1,
    "TabItem": 1,
    "Hyperlink": 1,
    "Slider": 1,
    "Spinner": 1,
    "Button": 2,
    "SplitButton": 2,
    "MenuItem": 2,
}


def _annotation_order(elements: Iterable[UIElement]) -> tuple[UIElement, ...]:
    return tuple(
        sorted(
            elements,
            key=lambda item: (
                _ANNOTATION_PRIORITY.get(item.control_type, 1),
                item.top,
                item.left,
            ),
        )
    )


@dataclass(frozen=True)
class DisplayBounds:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def intersects(self, left: int, top: int, right: int, bottom: int) -> bool:
        return (
            right > self.left
            and left < self.right
            and bottom > self.top
            and top < self.bottom
        )

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def to_public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
        }


@dataclass(frozen=True)
class UIElement:
    id: int
    name: str
    control_type: str
    automation_id: str
    left: int
    top: int
    right: int
    bottom: int
    source: str = "uia"
    enabled: bool = True
    verified_by: str = "type"

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.control_type,
            "center": list(self.center),
            "rect": [self.left, self.top, self.right, self.bottom],
            "source": self.source,
            "enabled": self.enabled,
            "verified_by": self.verified_by,
        }


@dataclass(frozen=True)
class SOMSnapshot:
    snapshot_id: str
    elements: tuple[UIElement, ...]
    display: DisplayBounds
    window_title: str
    screenshot: str
    image_width: int
    image_height: int
    created_at: float
    # IDs that have a numbered box in the annotated screenshot. The full element
    # list can be larger; those extra controls are still clickable by name/ID.
    annotated_ids: tuple[int, ...] = ()
    # "uia", "ocr", "uia+ocr", or "none".
    detection: str = "uia"
    uia_scanned: int = 0
    uia_accepted: int = 0
    # Live-context anchors used by pre-action validation.
    window_handle: int | None = None
    process_id: int | None = None
    process_name: str = ""
    window_rect: tuple[int, int, int, int] | None = None

    def element(self, element_id: int) -> UIElement | None:
        return next((item for item in self.elements if item.id == element_id), None)

    def coordinate_map(self) -> dict[str, dict[str, Any]]:
        return {
            str(item.id): {
                "x": item.center[0],
                "y": item.center[1],
                "name": item.name,
                "type": item.control_type,
            }
            for item in self.elements
        }

    def to_public(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "window_title": self.window_title,
            "display": self.display.to_public(),
            "screenshot": self.screenshot,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "window_handle": self.window_handle,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "window_rect": list(self.window_rect) if self.window_rect else None,
            "elements": [item.to_public() for item in self.elements],
            "coordinate_map": self.coordinate_map(),
            "annotated_ids": list(self.annotated_ids),
            "detection": self.detection,
            "uia_scanned": self.uia_scanned,
            "uia_accepted": self.uia_accepted,
        }


class SnapshotStore:
    """Short-lived snapshots so stale element IDs cannot be used."""

    def __init__(self, *, max_items: int = 20, ttl_seconds: float = 300.0) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(10.0, float(ttl_seconds))
        self._items: OrderedDict[str, SOMSnapshot] = OrderedDict()

    def put(self, snapshot: SOMSnapshot) -> None:
        self._prune()
        self._items[snapshot.snapshot_id] = snapshot
        self._items.move_to_end(snapshot.snapshot_id)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def get(self, snapshot_id: str) -> SOMSnapshot | None:
        self._prune()
        snapshot = self._items.get(str(snapshot_id))
        if snapshot is None:
            return None
        self._items.move_to_end(snapshot.snapshot_id)
        return snapshot

    def clear(self) -> None:
        self._items.clear()

    @property
    def count(self) -> int:
        self._prune()
        return len(self._items)

    def _prune(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        stale = [
            key
            for key, snapshot in self._items.items()
            if snapshot.created_at < cutoff
        ]
        for key in stale:
            self._items.pop(key, None)


def _monitors() -> list[DisplayBounds]:
    try:
        import mss

        with mss.MSS() as capture:
            raw = [dict(item) for item in list(capture.monitors)[1:]]
    except Exception:  # noqa: BLE001 - display probing must not crash a turn
        logger.debug("Could not enumerate displays", exc_info=True)
        return []

    monitors: list[DisplayBounds] = []
    for index, item in enumerate(raw, start=1):
        width = int(item.get("width", 0))
        height = int(item.get("height", 0))
        if width <= 0 or height <= 0:
            continue
        monitors.append(
            DisplayBounds(
                index=index,
                left=int(item.get("left", 0)),
                top=int(item.get("top", 0)),
                width=width,
                height=height,
            )
        )

    if not monitors:
        return []

    primary = next(
        (
            monitor
            for monitor in monitors
            if monitor.left == 0 and monitor.top == 0
        ),
        monitors[0],
    )
    return [
        replace(monitor, primary=(monitor.index == primary.index))
        for monitor in monitors
    ]


def _display_for_point(
    monitors: list[DisplayBounds],
    x: int,
    y: int,
) -> DisplayBounds | None:
    for monitor in monitors:
        if monitor.contains(x, y):
            return monitor
    return None


def _display_for_rect(
    monitors: list[DisplayBounds],
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> DisplayBounds | None:
    best: tuple[int, DisplayBounds] | None = None
    for monitor in monitors:
        overlap_width = max(
            0,
            min(right, monitor.right) - max(left, monitor.left),
        )
        overlap_height = max(
            0,
            min(bottom, monitor.bottom) - max(top, monitor.top),
        )
        area = overlap_width * overlap_height
        if area > 0 and (best is None or area > best[0]):
            best = (area, monitor)
    if best is not None:
        return best[1]
    return _display_for_point(
        monitors,
        (left + right) // 2,
        (top + bottom) // 2,
    )


def _capture_display(monitor: DisplayBounds):
    return _capture_region(
        (monitor.left, monitor.top, monitor.right, monitor.bottom)
    )


def _capture_region(rect: tuple[int, int, int, int]):
    """Capture a screen rectangle as an RGB image (left, top, right, bottom)."""
    import mss
    from PIL import Image

    left, top, right, bottom = (int(value) for value in rect)
    width = max(1, right - left)
    height = max(1, bottom - top)

    with mss.MSS() as capture:
        shot = capture.grab(
            {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        )
        return Image.frombytes("RGB", shot.size, shot.rgb)


def _clip_rect(
    rect: tuple[int, int, int, int],
    display: DisplayBounds,
) -> tuple[int, int, int, int]:
    """Clip a window rectangle to the display it belongs to."""
    left, top, right, bottom = (int(value) for value in rect)
    return (
        max(left, display.left),
        max(top, display.top),
        min(right, display.right),
        min(bottom, display.bottom),
    )


def _screen_fingerprint(image: Any) -> bytes:
    """Small grayscale thumbnail used to detect an unchanged screen."""
    from PIL import Image

    return image.convert("L").resize(
        (64, 36),
        Image.Resampling.BILINEAR,
    ).tobytes()


def _fingerprint_distance(first: bytes, second: bytes) -> float:
    """Mean absolute difference of two fingerprints, normalized to 0..1."""
    if not first or not second or len(first) != len(second):
        return 1.0
    total = sum(abs(a - b) for a, b in zip(first, second))
    return total / (len(first) * 255.0)


def _foreground_handle() -> int:
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001 - best effort
        return 0


def _visible_windows() -> list[Any]:
    try:
        import pygetwindow as gw
    except Exception:  # noqa: BLE001 - optional dependency
        return []

    try:
        windows = gw.getAllWindows()
    except Exception:  # noqa: BLE001 - desktop session issues
        return []

    return [
        window
        for window in windows
        if str(getattr(window, "title", "") or "").strip()
        and bool(getattr(window, "visible", True))
        and not bool(getattr(window, "isMinimized", False))
    ]


def _normalize_title(value: Any) -> str:
    text = re.sub(r"[^0-9a-z]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def _find_window(title: str | None) -> Any | None:
    requested = str(title or "").strip()
    if not requested:
        try:
            import pygetwindow as gw

            return gw.getActiveWindow()
        except Exception:  # noqa: BLE001
            return None

    # Newly launched apps can take a moment to expose their top-level window.
    deadline = time.monotonic() + 2.0
    while True:
        windows = _visible_windows()
        requested_folded = requested.casefold()
        exact = [
            window
            for window in windows
            if str(window.title).strip().casefold() == requested_folded
        ]
        partial = [
            window
            for window in windows
            if requested_folded in str(window.title).strip().casefold()
        ]
        if exact or partial:
            candidates = exact or partial
            return min(
                candidates,
                key=lambda window: (
                    not bool(getattr(window, "isActive", False)),
                    len(str(window.title)),
                ),
            )

        normalized = _normalize_title(requested)
        scored: list[tuple[float, Any]] = []
        for window in windows:
            score = SequenceMatcher(
                None,
                normalized,
                _normalize_title(window.title),
            ).ratio()
            if score >= 0.6:
                scored.append((score, window))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            return scored[0][1]

        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _activate_window(window: Any) -> None:
    hwnd = getattr(window, "_hWnd", None)
    if hwnd:
        try:
            from tools.desktop_context import force_foreground

            if force_foreground(int(hwnd)):
                return
        except Exception:  # noqa: BLE001 - fall back below
            logger.debug("force_foreground failed", exc_info=True)
        try:
            ctypes.windll.user32.ShowWindow(int(hwnd), 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(int(hwnd))
            return
        except Exception:  # noqa: BLE001 - fall back to pygetwindow
            logger.debug("SetForegroundWindow failed", exc_info=True)
    try:
        window.activate()
    except Exception:  # noqa: BLE001 - activation is best effort
        logger.debug("Could not activate target window", exc_info=True)


def _window_rect(window: Any) -> tuple[int, int, int, int] | None:
    try:
        left = int(getattr(window, "left", 0) or 0)
        top = int(getattr(window, "top", 0) or 0)
        width = int(getattr(window, "width", 0) or 0)
        height = int(getattr(window, "height", 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, left + width, top + height


def _rect_intersects(
    display: DisplayBounds,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> bool:
    return display.intersects(left, top, right, bottom)


def _uia_elements(
    window: Any,
    display: DisplayBounds,
    settings: AutomationSettings,
) -> tuple[list[DetectedElement], dict[str, Any]]:
    """Scan a window's UIA tree for verified interactive controls.

    Returns ``(elements, stats)``. Nothing here guesses from pixels: every
    returned rectangle belongs to a real accessibility node.
    """
    stats: dict[str, Any] = {
        "scanned": 0,
        "accepted": 0,
        "offscreen": 0,
        "disabled": 0,
        "rejected": 0,
        "error": "",
    }
    hwnd = getattr(window, "_hWnd", None)
    if not hwnd:
        stats["error"] = "The target window has no native handle."
        return [], stats

    try:
        from pywinauto import Desktop

        wrapper = (
            Desktop(backend="uia")
            .window(handle=int(hwnd))
            .wrapper_object()
        )
    except Exception as exc:  # noqa: BLE001 - UIA may be unavailable
        logger.debug("Could not open UIA wrapper for %r: %s", window.title, exc)
        stats["error"] = f"UIA wrapper failed: {exc}"
        return [], stats

    queue: deque[tuple[Any, int]] = deque([(wrapper, 0)])
    found: list[DetectedElement] = []
    seen_rects: set[tuple[str, int, int, int, int]] = set()

    while queue and stats["scanned"] < settings.max_element_scan:
        control, depth = queue.popleft()
        stats["scanned"] += 1

        try:
            info = control.element_info
        except Exception:  # noqa: BLE001 - a control can disappear
            continue

        control_type = str(getattr(info, "control_type", "") or "").strip()

        # UIA's offscreen flag is the authoritative visibility signal.
        offscreen = False
        try:
            raw = info.element
            offscreen = bool(int(getattr(raw, "CurrentIsOffscreen", 0) or 0))
        except Exception:  # noqa: BLE001 - fall back to the wrapper
            try:
                offscreen = not bool(control.is_visible())
            except Exception:
                offscreen = False

        if offscreen:
            stats["offscreen"] += 1
            # Children of an offscreen subtree are offscreen as well.
            continue

        if depth < settings.max_element_depth:
            try:
                for child in control.children():
                    queue.append((child, depth + 1))
            except Exception:  # noqa: BLE001 - a child can disappear
                pass

        if control_type in _NEVER_INTERACTABLE:
            stats["rejected"] += 1
            continue

        try:
            rect = control.rectangle()
            left, top, right, bottom = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
        except Exception:  # noqa: BLE001
            stats["rejected"] += 1
            continue

        if (
            right - left < settings.min_element_size
            or bottom - top < settings.min_element_size
        ):
            stats["rejected"] += 1
            continue
        if not _rect_intersects(display, left, top, right, bottom):
            stats["rejected"] += 1
            continue

        # Only expose the visible part so the center is always clickable.
        left = max(left, display.left)
        top = max(top, display.top)
        right = min(right, display.right)
        bottom = min(bottom, display.bottom)
        if (
            right - left < settings.min_element_size
            or bottom - top < settings.min_element_size
        ):
            stats["rejected"] += 1
            continue

        try:
            enabled = bool(control.is_enabled())
        except Exception:  # noqa: BLE001
            enabled = True
        if not enabled:
            stats["disabled"] += 1
            continue

        # Pattern probes are only needed when the control type is not already
        # definitive. This keeps the scan fast on large trees.
        invoke = False
        focusable = False
        if control_type not in _DEFINITELY_INTERACTIVE:
            try:
                control.iface_invoke
                invoke = True
            except Exception:  # noqa: BLE001 - no Invoke pattern
                invoke = False
            try:
                focusable = bool(control.is_keyboard_focusable())
            except Exception:  # noqa: BLE001
                focusable = False

        accepted, verified_by = _is_verified_interactable(
            control_type,
            invoke=invoke,
            focusable=focusable,
            width=right - left,
            height=bottom - top,
        )
        if not accepted:
            stats["rejected"] += 1
            continue

        try:
            name = str(getattr(info, "name", "") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        if not name:
            try:
                name = str(control.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                name = ""
        try:
            automation_id = str(getattr(info, "automation_id", "") or "").strip()
        except Exception:  # noqa: BLE001
            automation_id = ""
        if not name:
            name = automation_id or f"Unnamed {control_type}"

        key = (control_type, left, top, right, bottom)
        if key in seen_rects:
            continue
        seen_rects.add(key)

        found.append(
            DetectedElement(
                name=name[:120],
                control_type=control_type,
                automation_id=automation_id[:120],
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                enabled=enabled,
                source="uia",
                verified_by=verified_by,
            )
        )
        stats["accepted"] += 1
        if len(found) >= settings.max_elements:
            break

    return found, stats


def _overlap_ratio(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area = max(1, (first[2] - first[0]) * (first[3] - first[1]))
    return intersection / area


def _containment_ratio(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> float:
    """Intersection over the smaller area, for nested duplicate controls."""
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    smaller = min(
        max(1, (first[2] - first[0]) * (first[3] - first[1])),
        max(1, (second[2] - second[0]) * (second[3] - second[1])),
    )
    return intersection / smaller


def _dedupe_detected(
    elements: Iterable[DetectedElement],
) -> list[DetectedElement]:
    """Remove nested/duplicate hits, keeping the more semantic control.

    Only similarly sized rectangles are compared. A large document pane must
    not suppress the buttons inside it; a Group row and its TreeItem do count
    as the same target.
    """
    ordered = sorted(
        elements,
        key=lambda item: (
            _SEMANTIC_PRIORITY.get(item.control_type, 4),
            (item.right - item.left) * (item.bottom - item.top),
        ),
    )
    kept: list[DetectedElement] = []
    for element in ordered:
        rect = (element.left, element.top, element.right, element.bottom)
        area = max(1, (element.right - element.left) * (element.bottom - element.top))
        duplicate = False
        for existing in kept:
            other = (
                existing.left,
                existing.top,
                existing.right,
                existing.bottom,
            )
            other_area = max(
                1,
                (existing.right - existing.left)
                * (existing.bottom - existing.top),
            )
            size_ratio = max(area, other_area) / max(1, min(area, other_area))
            if size_ratio > 8.0:
                continue
            if _containment_ratio(rect, other) >= 0.60:
                duplicate = True
                break
        if not duplicate:
            kept.append(element)

    kept.sort(key=lambda item: (item.top, item.left))
    return kept


_OCR_LOCK = threading.Lock()
_OCR_ENGINE: Any | None = None


def _ocr_engine() -> Any:
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_text_ok(
    text: str,
    width: int,
    height: int,
    confidence: float,
    settings: AutomationSettings,
) -> bool:
    """Strict filter for OCR fallback labels.

    A plain paragraph line has many words, a long/narrow box, or an
    implausible size. Buttons and labels are short and compact.
    """
    words = [word for word in str(text or "").split() if word]
    if not words or len(words) > settings.ocr_max_words:
        return False
    if confidence < settings.ocr_min_confidence:
        return False
    if len(str(text).strip()) > 60:
        return False
    if width < 16 or width > 520:
        return False
    if height < 10 or height > settings.ocr_max_height:
        return False
    aspect = width / max(1, height)
    return 0.5 <= aspect <= 7.5


def _ocr_elements(
    image: Any,
    settings: AutomationSettings,
    *,
    origin: tuple[int, int] | None = None,
    max_items: int = 60,
) -> tuple[list[DetectedElement], dict[str, Any]]:
    """OCR fallback for windows that expose no usable UIA tree.

    Only short, compact text labels are kept. There is no contour guessing:
    a box must contain recognizable text (1-4 words) and pass the geometry
    filters, otherwise it is dropped.
    """
    stats: dict[str, Any] = {"engine": "rapidocr", "lines": 0, "accepted": 0, "error": ""}
    if not settings.visual_fallback:
        stats["error"] = "OCR fallback is disabled."
        return [], stats

    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001 - optional fallback
        stats["error"] = f"OCR dependencies unavailable: {exc}"
        return [], stats

    origin_left = 0 if origin is None else int(origin[0])
    origin_top = 0 if origin is None else int(origin[1])

    max_width = max(640, int(settings.screenshot_max_width))
    scale = min(1.0, max_width / max(1, image.width))
    working = image
    if scale < 1.0:
        from PIL import Image

        working = image.resize(
            (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            ),
            Image.Resampling.BILINEAR,
        )

    try:
        pixels = np.asarray(working.convert("RGB"))
        # RapidOCR's ndarray path expects OpenCV's BGR order.
        array = pixels[:, :, ::-1].copy()
        with _OCR_LOCK:
            engine = _ocr_engine()
            result, _elapsed = engine(array)
    except Exception as exc:  # noqa: BLE001 - OCR must not break a snapshot
        logger.warning("OCR fallback failed: %s", exc)
        stats["error"] = f"OCR failed: {exc}"
        return [], stats

    candidates: list[tuple[float, str, int, int, int, int]] = []
    for item in result or []:
        try:
            box, text, score = item
            points = [(float(point[0]), float(point[1])) for point in box]
            confidence = float(score)
        except (TypeError, ValueError, IndexError):
            continue
        stats["lines"] += 1

        x0 = min(point[0] for point in points) / scale
        y0 = min(point[1] for point in points) / scale
        x1 = max(point[0] for point in points) / scale
        y1 = max(point[1] for point in points) / scale
        left = origin_left + int(round(x0))
        top = origin_top + int(round(y0))
        right = origin_left + int(round(x1))
        bottom = origin_top + int(round(y1))
        width = right - left
        height = bottom - top
        label = " ".join(str(text or "").split())
        if not _ocr_text_ok(label, width, height, confidence, settings):
            continue
        candidates.append((confidence, label, left, top, right, bottom))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[DetectedElement] = []
    kept_rects: list[tuple[int, int, int, int]] = []
    for confidence, label, left, top, right, bottom in candidates:
        rect = (left, top, right, bottom)
        if any(_containment_ratio(rect, other) >= 0.35 for other in kept_rects):
            continue
        kept_rects.append(rect)
        selected.append(
            DetectedElement(
                name=label[:120],
                control_type="OcrText",
                automation_id="",
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                enabled=True,
                source="ocr",
                verified_by="ocr",
            )
        )
        if len(selected) >= max_items:
            break

    selected.sort(key=lambda item: (item.top, item.left))
    stats["accepted"] = len(selected)
    return selected, stats


def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size=size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.truetype(name, size=size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


class SOMParser:
    """Capture, detect, number, and persist a Set-of-Mark snapshot."""

    def __init__(self, settings: AutomationSettings) -> None:
        self.settings = settings
        self.store = SnapshotStore()
        # Last snapshot, used to skip a full capture/annotation pass when the
        # target window pixels did not change.
        self._cached: SOMSnapshot | None = None
        self._cache_fingerprint: bytes | None = None
        self._cache_rect: tuple[int, int, int, int] | None = None
        self._cache_hwnd: int | None = None
        self._cache_display: int | None = None
        self._cache_foreground: int | None = None
        self._cache_title: str | None = None
        self._saves_since_prune = 0

    def reset(self) -> None:
        """Forget the reuse cache (a new user turn starts from a fresh look)."""
        self._cached = None
        self._cache_fingerprint = None
        self._cache_rect = None
        self._cache_hwnd = None
        self._cache_display = None
        self._cache_foreground = None
        self._cache_title = None
        self.store.clear()

    def _reusable(
        self,
        *,
        image: Any,
        hwnd: int | None,
        display_index: int,
        rect: tuple[int, int, int, int],
        foreground: int,
        title: str,
    ) -> SOMSnapshot | None:
        """Return the previous snapshot when the screen is verifiably unchanged."""
        if self.settings.snapshot_reuse_seconds <= 0:
            return None
        cached = self._cached
        if cached is None or self._cache_fingerprint is None:
            return None
        if self._cache_hwnd != hwnd or self._cache_display != display_index:
            return None
        if self._cache_rect != rect:
            return None
        # A new foreground window usually means a dialog or another app
        # appeared, even when it does not overlap the captured window.
        if self._cache_foreground != foreground:
            return None
        # A changed title means navigation, a save, a modified document, or a
        # different page. Never reuse the old element list in that case.
        if (self._cache_title or "").casefold() != (title or "").casefold():
            return None
        if time.time() - cached.created_at > self.settings.snapshot_reuse_seconds:
            return None
        if not Path(cached.screenshot).is_file():
            return None

        distance = _fingerprint_distance(
            self._cache_fingerprint,
            _screen_fingerprint(image),
        )
        if distance > self.settings.snapshot_reuse_threshold:
            return None

        refreshed = replace(cached, created_at=time.time())
        self.store.put(refreshed)
        self._cached = refreshed
        return refreshed

    def _remember(
        self,
        snapshot: SOMSnapshot,
        *,
        fingerprint: bytes,
        hwnd: int | None,
        display_index: int,
        rect: tuple[int, int, int, int],
        foreground: int,
        title: str,
    ) -> None:
        self._cached = snapshot
        self._cache_fingerprint = fingerprint
        self._cache_hwnd = hwnd
        self._cache_display = display_index
        self._cache_rect = rect
        self._cache_foreground = foreground
        self._cache_title = title

    def _prune_screenshots(self) -> None:
        """Delete the oldest annotated snapshots beyond ``screenshot_keep``."""
        self._saves_since_prune += 1
        if self._saves_since_prune < 20:
            return
        self._saves_since_prune = 0

        directory = self.settings.resolved_screenshot_dir()
        try:
            files = sorted(
                directory.glob("som_*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[self.settings.screenshot_keep :]:
            try:
                stale.unlink()
            except OSError:
                continue

    def snapshot(
        self,
        *,
        display: int | None = None,
        window_title: str | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        monitors = _monitors()
        if not monitors:
            return {"ok": False, "error": "No display is available."}

        window = _find_window(window_title)
        actual_title = str(getattr(window, "title", "") or "").strip()
        if window_title and window is None:
            titles = [str(item.title).strip() for item in _visible_windows()][:20]
            return {
                "ok": False,
                "error": (
                    f"No visible window matches {window_title!r}. "
                    f"Open windows: {titles}"
                ),
                "open_windows": titles,
            }

        if window is not None and activate:
            target = int(getattr(window, "_hWnd", 0) or 0) or None
            # Only activate when the window is not already foreground. If
            # Windows refuses the foreground switch (a background process
            # cannot always steal focus), do not burn half a second waiting.
            if target is None:
                _activate_window(window)
                time.sleep(0.05)
            elif _foreground_handle() != target:
                _activate_window(window)
                deadline = time.monotonic() + 0.15
                while (
                    _foreground_handle() != target
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

        hwnd = int(getattr(window, "_hWnd", 0) or 0) or None
        target_foreground = bool(hwnd) and _foreground_handle() == hwnd

        monitor: DisplayBounds | None = None
        if display is not None:
            try:
                chosen = int(display)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Display must be a 1-based number."}
            if chosen < 1 or chosen > len(monitors):
                return {
                    "ok": False,
                    "error": f"Display {chosen} is not available.",
                    "displays": [item.to_public() for item in monitors],
                }
            monitor = monitors[chosen - 1]
        else:
            rect = _window_rect(window) if window is not None else None
            if rect is not None:
                monitor = _display_for_rect(monitors, *rect)
            if monitor is None:
                monitor = next(
                    (item for item in monitors if item.primary),
                    monitors[0],
                )

        # Capture only the target window when it fits on the selected display.
        # A 4K display grab is several times more expensive to capture,
        # downscale, and encode than the window itself.
        capture_rect = (
            monitor.left,
            monitor.top,
            monitor.right,
            monitor.bottom,
        )
        window_rect = _window_rect(window) if window is not None else None
        if window_rect is not None:
            clipped = _clip_rect(window_rect, monitor)
            if clipped[2] - clipped[0] >= 40 and clipped[3] - clipped[1] >= 40:
                capture_rect = clipped

        image = _capture_region(capture_rect)
        fingerprint = _screen_fingerprint(image)
        foreground = _foreground_handle()

        reused = self._reusable(
            image=image,
            hwnd=hwnd,
            display_index=monitor.index,
            rect=capture_rect,
            foreground=foreground,
            title=actual_title,
        )
        if reused is not None:
            return {
                "ok": True,
                "snapshot": reused,
                "message": (
                    f"Detected {len(reused.elements)} "
                    f"{reused.detection.replace('uia', 'UIA-verified').replace('ocr', 'OCR-filtered')} "
                    f"interactable element(s) in "
                    f"{actual_title or 'the active window'}. The window "
                    "content is unchanged, so the previous snapshot is still "
                    "valid."
                ),
                "files": [reused.screenshot],
                "reused": True,
                "detection": reused.detection,
                "foreground": target_foreground,
                "uia_stats": {"scanned": reused.uia_scanned, "accepted": reused.uia_accepted},
                "ocr_stats": {},
            }

        uia_elements: list[DetectedElement] = []
        uia_stats: dict[str, Any] = {}
        if window is not None:
            uia_elements, uia_stats = _uia_elements(
                window,
                monitor,
                self.settings,
            )
        if uia_stats.get("error"):
            logger.info("UIA scan for %r: %s", actual_title, uia_stats["error"])

        # UIA is authoritative. Only when it exposes almost nothing do we fall
        # back to strict OCR labels for otherwise inaccessible custom windows.
        detection = "uia" if len(uia_elements) >= self.settings.uia_min_elements else "none"
        combined: list[DetectedElement] = list(uia_elements)
        ocr_stats: dict[str, Any] = {}
        if len(uia_elements) < self.settings.uia_min_elements:
            if target_foreground:
                ocr_elements, ocr_stats = _ocr_elements(
                    image,
                    self.settings,
                    origin=(capture_rect[0], capture_rect[1]),
                )
                if ocr_elements:
                    combined.extend(ocr_elements)
                    detection = "uia+ocr" if uia_elements else "ocr"
                elif uia_elements:
                    detection = "uia"
            else:
                # A screen capture of a background/occluded window can show a
                # different app. Never OCR those pixels: that is exactly how
                # false positives get introduced.
                ocr_stats = {
                    "engine": "rapidocr",
                    "lines": 0,
                    "accepted": 0,
                    "error": (
                        "Target window is not foreground; OCR fallback skipped "
                        "to avoid reading another application."
                    ),
                }

        raw_elements = _dedupe_detected(combined)[: self.settings.max_elements]

        elements = tuple(
            UIElement(
                id=index,
                name=item.name,
                control_type=item.control_type,
                automation_id=item.automation_id,
                left=item.left,
                top=item.top,
                right=item.right,
                bottom=item.bottom,
                source=item.source,
                enabled=item.enabled,
                verified_by=item.verified_by,
            )
            for index, item in enumerate(raw_elements, start=1)
        )

        annotated = _annotation_order(elements)[
            : self.settings.max_annotated_elements
        ]
        screenshot, image_width, image_height = self._annotate(
            image,
            annotated,
            monitor,
            origin=(capture_rect[0], capture_rect[1]),
        )
        annotated_ids = tuple(item.id for item in annotated)

        process_id: int | None = None
        process_name = ""
        window_rect_live: tuple[int, int, int, int] | None = None
        if window is not None:
            try:
                from tools.desktop_context import (
                    _process_details,
                    _process_id,
                    _window_rect as _live_window_rect,
                )

                if hwnd:
                    process_id = _process_id(hwnd)
                    process_name = _process_details(process_id).get("name", "")
                    window_rect_live = _live_window_rect(hwnd)
            except Exception:  # noqa: BLE001 - context anchors are best effort
                logger.debug("Could not capture window context", exc_info=True)

        snapshot = SOMSnapshot(
            snapshot_id=f"som_{uuid.uuid4().hex[:16]}",
            elements=elements,
            display=monitor,
            window_title=actual_title,
            screenshot=screenshot,
            image_width=image_width,
            image_height=image_height,
            created_at=time.time(),
            annotated_ids=annotated_ids,
            detection=detection,
            uia_scanned=int(uia_stats.get("scanned") or 0),
            uia_accepted=len(uia_elements),
            window_handle=hwnd,
            process_id=process_id,
            process_name=process_name,
            window_rect=window_rect_live,
        )
        self.store.put(snapshot)
        self._remember(
            snapshot,
            fingerprint=fingerprint,
            hwnd=hwnd,
            display_index=monitor.index,
            rect=capture_rect,
            foreground=foreground,
            title=actual_title,
        )

        source_label = {
            "uia": "UIA-verified",
            "ocr": "OCR-filtered",
            "uia+ocr": "UIA + OCR-filtered",
            "none": "",
        }[detection]
        prefix = f"{source_label} " if source_label else ""
        message = (
            f"Detected {len(elements)} {prefix}interactable element(s) in "
            f"{actual_title or 'the active display'}; "
            f"{len(annotated_ids)} are numbered in the screenshot."
        )
        if not elements:
            message = (
                f"No {prefix}interactable elements were detected in "
                f"{actual_title or 'the active display'}. UIA scan: "
                f"{uia_stats.get('scanned', 0)} nodes, "
                f"{uia_stats.get('accepted', 0)} verified. "
                f"OCR: {ocr_stats.get('accepted', 0)} labels. Bring the target "
                "window to the front or pass its exact window_title."
            )
        if not target_foreground:
            message += (
                " The target window is not foreground, so the screenshot may "
                "show another window; bring it to the front before relying on "
                "the image."
            )

        return {
            "ok": True,
            "snapshot": snapshot,
            "message": message,
            "files": [screenshot],
            "detection": detection,
            "foreground": target_foreground,
            "uia_stats": uia_stats,
            "ocr_stats": ocr_stats,
        }

    def _annotate(
        self,
        image: Any,
        elements: tuple[UIElement, ...],
        display: DisplayBounds,
        *,
        origin: tuple[int, int] | None = None,
    ) -> tuple[str, int, int]:
        from PIL import Image, ImageDraw

        origin_left = display.left if origin is None else int(origin[0])
        origin_top = display.top if origin is None else int(origin[1])

        max_width = max(640, int(self.settings.screenshot_max_width))
        scale = min(1.0, max_width / max(1, image.width))
        if scale < 1.0:
            image = image.resize(
                (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                ),
                Image.Resampling.BILINEAR,
            )

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = _font(max(12, int(image.width / 90)))

        for element in elements:
            left = int((element.left - origin_left) * scale)
            top = int((element.top - origin_top) * scale)
            right = int((element.right - origin_left) * scale)
            bottom = int((element.bottom - origin_top) * scale)
            if right <= 0 or bottom <= 0 or left >= image.width or top >= image.height:
                continue
            left = max(0, min(image.width - 1, left))
            top = max(0, min(image.height - 1, top))
            right = max(left + 1, min(image.width, right))
            bottom = max(top + 1, min(image.height, bottom))
            color = _TYPE_COLORS.get(element.control_type, _DEFAULT_COLOR)
            draw.rectangle(
                [left, top, right, bottom],
                fill=(*color, 38),
                outline=(*color, 235),
                width=2,
            )

        composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        label_draw = ImageDraw.Draw(composed)
        for element in elements:
            left = int((element.left - origin_left) * scale)
            top = int((element.top - origin_top) * scale)
            right = int((element.right - origin_left) * scale)
            bottom = int((element.bottom - origin_top) * scale)
            if right <= 0 or bottom <= 0 or left >= composed.width or top >= composed.height:
                continue
            left = max(0, min(composed.width - 1, left))
            top = max(0, min(composed.height - 1, top))
            right = max(left + 1, min(composed.width, right))
            bottom = max(top + 1, min(composed.height, bottom))
            color = _TYPE_COLORS.get(element.control_type, _DEFAULT_COLOR)
            label = str(element.id)
            bbox = label_draw.textbbox((0, 0), label, font=font)
            label_width = max(18, bbox[2] - bbox[0] + 8)
            label_height = max(16, bbox[3] - bbox[1] + 4)
            label_left = max(0, min(composed.width - label_width, left))
            label_top = max(0, top - label_height)
            label_draw.rectangle(
                [
                    label_left,
                    label_top,
                    label_left + label_width,
                    label_top + label_height,
                ],
                fill=color,
            )
            label_draw.text(
                (label_left + 4, label_top + 1),
                label,
                font=font,
                fill=(12, 12, 12),
            )

        output_dir = self.settings.resolved_screenshot_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = output_dir / f"som_{stamp}_{os.getpid()}_{uuid.uuid4().hex[:6]}.png"
        # compress_level=1 is visually identical for these overlays and about
        # five times faster than optimize=True.
        composed.save(path, format="PNG", compress_level=1)
        self._prune_screenshots()
        return str(path), composed.width, composed.height
