"""Set-of-Mark UI parsing for desktop automation.

The primary detector is Windows UI Automation via ``pywinauto``: it is fast,
gives real control names and exact rectangles, and covers native apps such as
Outlook, Explorer, dialogs, and VS Code. A light OpenCV contour fallback adds
numbered regions for icon-only or custom-rendered controls when UIA exposes
almost nothing.

Every snapshot produces:
  * an annotated screenshot with a unique number over each element,
  * a numbered element list with control type and absolute screen rectangle,
  * a compact coordinate map (ID -> center x/y),
  * a short-lived ``snapshot_id`` used by click/move tools.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
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

_INTERACTABLE = {
    "Button",
    "Edit",
    "ComboBox",
    "CheckBox",
    "RadioButton",
    "Hyperlink",
    "MenuItem",
    "TabItem",
    "ListItem",
    "TreeItem",
    "Slider",
    "Spinner",
    "SplitButton",
    "DataItem",
    "Document",
    "RichEdit",
}

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
}
_DEFAULT_COLOR = (251, 191, 36)


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
    import mss
    from PIL import Image

    with mss.MSS() as capture:
        shot = capture.grab(
            {
                "left": monitor.left,
                "top": monitor.top,
                "width": monitor.width,
                "height": monitor.height,
            }
        )
        return Image.frombytes("RGB", shot.size, shot.rgb)


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
) -> list[tuple[str, str, str, int, int, int, int, bool]]:
    hwnd = getattr(window, "_hWnd", None)
    if not hwnd:
        return []

    try:
        from pywinauto import Desktop

        wrapper = (
            Desktop(backend="uia")
            .window(handle=int(hwnd))
            .wrapper_object()
        )
    except Exception as exc:  # noqa: BLE001 - UIA may be unavailable
        logger.debug("Could not open UIA wrapper for %r: %s", window.title, exc)
        return []

    queue: deque[tuple[Any, int]] = deque([(wrapper, 0)])
    scanned = 0
    found: list[tuple[str, str, str, int, int, int, int, bool]] = []
    seen_rects: set[tuple[str, int, int, int, int]] = set()

    while (
        queue
        and scanned < settings.max_element_scan
        and len(found) < settings.max_elements
    ):
        control, depth = queue.popleft()
        scanned += 1

        try:
            visible = bool(control.is_visible())
        except Exception:  # noqa: BLE001 - some wrappers lack visibility
            visible = True

        if visible and depth < settings.max_element_depth:
            try:
                for child in control.children():
                    queue.append((child, depth + 1))
            except Exception:  # noqa: BLE001 - a child can disappear
                pass

        try:
            info = control.element_info
            control_type = str(getattr(info, "control_type", "") or "")
        except Exception:  # noqa: BLE001
            continue

        if control_type not in _INTERACTABLE:
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
            continue

        if (
            right - left < settings.min_element_size
            or bottom - top < settings.min_element_size
        ):
            continue
        if not _rect_intersects(display, left, top, right, bottom):
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
            continue

        try:
            enabled = bool(control.is_enabled())
        except Exception:  # noqa: BLE001
            enabled = True

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
            (
                name[:120],
                control_type,
                automation_id[:120],
                left,
                top,
                right,
                bottom,
                enabled,
            )
        )

    return found


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


def _visual_elements(
    image: Any,
    display: DisplayBounds,
    existing: Iterable[tuple[int, int, int, int]],
    settings: AutomationSettings,
    *,
    max_items: int = 15,
) -> list[tuple[str, str, str, int, int, int, int, bool]]:
    """Conservative contour fallback for custom-rendered controls."""
    if not settings.visual_fallback:
        return []

    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001 - optional fallback
        return []

    try:
        pixels = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 45, 135)
        edges = cv2.dilate(
            edges,
            np.ones((3, 3), dtype=np.uint8),
            iterations=1,
        )
        contours, _hierarchy = cv2.findContours(
            edges,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Visual element fallback failed", exc_info=True)
        return []

    image_area = max(1, image.width * image.height)
    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < max(18, settings.min_element_size * 2):
            continue
        if height < max(14, settings.min_element_size * 2):
            continue
        if width > image.width * 0.55 or height > image.height * 0.65:
            continue
        if (width * height) < image_area * 0.00015:
            continue
        aspect = width / max(1, height)
        if aspect < 0.25 or aspect > 6.0:
            continue
        candidates.append((x, y, width, height))

    candidates.sort(key=lambda item: item[2] * item[3], reverse=True)
    local_existing = [
        (
            left - display.left,
            top - display.top,
            right - display.left,
            bottom - display.top,
        )
        for left, top, right, bottom in existing
    ]

    selected: list[tuple[int, int, int, int]] = []
    for x, y, width, height in candidates:
        rect = (x, y, x + width, y + height)
        if any(_overlap_ratio(rect, other) > 0.55 for other in local_existing):
            continue
        if any(_overlap_ratio(rect, other) > 0.55 for other in selected):
            continue
        selected.append(rect)
        if len(selected) >= max_items:
            break

    selected.sort(key=lambda item: (item[1], item[0]))
    return [
        (
            "visual element",
            "Visual",
            "",
            display.left + x,
            display.top + y,
            display.left + x + width,
            display.top + y + height,
            True,
        )
        for x, y, width, height in selected
    ]


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
            _activate_window(window)
            time.sleep(0.25)

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

        image = _capture_display(monitor)

        raw_elements: list[
            tuple[str, str, str, int, int, int, int, bool]
        ] = []
        if window is not None:
            raw_elements = _uia_elements(window, monitor, self.settings)

        if len(raw_elements) < 3:
            raw_elements.extend(
                _visual_elements(
                    image,
                    monitor,
                    [
                        (item[3], item[4], item[5], item[6])
                        for item in raw_elements
                    ],
                    self.settings,
                )
            )

        raw_elements.sort(key=lambda item: (item[4], item[3]))
        if len(raw_elements) > self.settings.max_elements:
            raw_elements = raw_elements[: self.settings.max_elements]

        elements = tuple(
            UIElement(
                id=index,
                name=item[0],
                control_type=item[1],
                automation_id=item[2],
                left=item[3],
                top=item[4],
                right=item[5],
                bottom=item[6],
                source="uia" if item[1] != "Visual" else "visual",
                enabled=item[7],
            )
            for index, item in enumerate(raw_elements, start=1)
        )

        screenshot, image_width, image_height = self._annotate(
            image,
            elements,
            monitor,
        )

        window_handle: int | None = None
        process_id: int | None = None
        process_name = ""
        window_rect: tuple[int, int, int, int] | None = None
        if window is not None:
            try:
                from tools.desktop_context import (
                    _process_details,
                    _process_id,
                    _window_rect as _live_window_rect,
                )

                hwnd = int(getattr(window, "_hWnd", 0) or 0) or None
                if hwnd:
                    window_handle = hwnd
                    process_id = _process_id(hwnd)
                    process_name = _process_details(process_id).get("name", "")
                    window_rect = _live_window_rect(hwnd)
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
            window_handle=window_handle,
            process_id=process_id,
            process_name=process_name,
            window_rect=window_rect,
        )
        self.store.put(snapshot)

        message = (
            f"Detected {len(elements)} interactable element(s) in "
            f"{actual_title or 'the active display'}."
        )
        if not elements:
            message = (
                f"No interactable elements were detected in "
                f"{actual_title or 'the active display'}. Bring the target "
                "window to the front or pass its exact window_title."
            )

        return {
            "ok": True,
            "snapshot": snapshot,
            "message": message,
            "files": [screenshot],
        }

    def _annotate(
        self,
        image: Any,
        elements: tuple[UIElement, ...],
        display: DisplayBounds,
    ) -> tuple[str, int, int]:
        from PIL import Image, ImageDraw

        max_width = max(640, int(self.settings.screenshot_max_width))
        scale = min(1.0, max_width / max(1, image.width))
        if scale < 1.0:
            image = image.resize(
                (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = _font(max(12, int(image.width / 90)))

        for element in elements:
            left = int((element.left - display.left) * scale)
            top = int((element.top - display.top) * scale)
            right = int((element.right - display.left) * scale)
            bottom = int((element.bottom - display.top) * scale)
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
            left = int((element.left - display.left) * scale)
            top = int((element.top - display.top) * scale)
            right = int((element.right - display.left) * scale)
            bottom = int((element.bottom - display.top) * scale)
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
        composed.save(path, format="PNG", optimize=True)
        return str(path), composed.width, composed.height
