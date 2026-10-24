"""Read-only Windows window awareness and UI Automation tools."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 20_000
_MAX_CONTROLS = 4_000
_MAX_WINDOW_RESULTS = 100


class WindowToolError(RuntimeError):
    """A window could not be listed, found, or read."""


def _visible_windows() -> list[Any]:
    try:
        import pygetwindow as gw
    except Exception as exc:  # noqa: BLE001 - optional desktop dependency
        raise WindowToolError(
            f"Window inspection is unavailable: {exc}"
        ) from exc

    try:
        windows = gw.getAllWindows()
    except Exception as exc:  # noqa: BLE001 - desktop session issues
        raise WindowToolError(
            f"Could not enumerate windows: {exc}"
        ) from exc

    return [
        window
        for window in windows
        if str(getattr(window, "title", "") or "").strip()
        and bool(getattr(window, "visible", True))
        and not bool(getattr(window, "isMinimized", False))
    ]


def get_open_windows() -> dict[str, Any]:
    """Return visible top-level window titles and which one is active."""
    windows = _visible_windows()

    entries: list[dict[str, Any]] = []
    active_title: str | None = None

    for window in windows:
        try:
            title = str(window.title).strip()
            is_active = bool(getattr(window, "isActive", False))
            entry = {
                "title": title,
                "active": is_active,
                "left": int(getattr(window, "left", 0) or 0),
                "top": int(getattr(window, "top", 0) or 0),
                "width": int(getattr(window, "width", 0) or 0),
                "height": int(getattr(window, "height", 0) or 0),
            }
        except Exception:  # noqa: BLE001 - a window can disappear mid-read
            continue

        if is_active:
            active_title = title
        entries.append(entry)

    entries.sort(key=lambda item: (not item["active"], item["title"].casefold()))
    entries = entries[:_MAX_WINDOW_RESULTS]

    return {
        "ok": True,
        "count": len(entries),
        "active_window": active_title,
        "windows": entries,
        "message": (
            f"Found {len(entries)} visible window(s)."
            + (f" Active: {active_title}." if active_title else "")
        ),
    }


def _normalize_title(value: Any) -> str:
    text = re.sub(r"[^0-9a-z]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def _matching_windows(window_title: str) -> list[Any]:
    requested = window_title.strip().casefold()
    normalized_requested = _normalize_title(window_title)

    try:
        import pygetwindow as gw
    except Exception as exc:  # noqa: BLE001
        raise WindowToolError(f"Window inspection is unavailable: {exc}") from exc

    try:
        windows = gw.getAllWindows()
    except Exception as exc:  # noqa: BLE001
        raise WindowToolError(f"Could not enumerate windows: {exc}") from exc

    exact: list[Any] = []
    partial: list[Any] = []
    fuzzy: list[tuple[float, Any]] = []

    for window in windows:
        title = str(getattr(window, "title", "") or "").strip()
        if not title or not getattr(window, "visible", True):
            continue

        lowered = title.casefold()
        if lowered == requested:
            exact.append(window)
        elif requested in lowered:
            partial.append(window)
        else:
            normalized_title = _normalize_title(title)
            if not normalized_requested or not normalized_title:
                continue
            score = SequenceMatcher(
                None,
                normalized_requested,
                normalized_title,
            ).ratio()
            if score >= 0.6:
                fuzzy.append((score, window))

    if exact:
        return exact
    if partial:
        return partial

    fuzzy.sort(key=lambda item: item[0], reverse=True)
    return [window for _score, window in fuzzy]


def _window_text(window: Any) -> tuple[str, int]:
    """Walk a pywinauto UI Automation tree and collect readable text."""
    chunks: list[str] = []
    seen: set[str] = set()
    total = 0

    def add(value: Any) -> None:
        nonlocal total
        text = str(value or "").strip()
        if not text or text in seen:
            return
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS]
        seen.add(text)
        chunks.append(text)
        total += len(text)

    queue = [window]
    scanned = 0

    while queue and scanned < _MAX_CONTROLS and total < _MAX_TEXT_CHARS:
        control = queue.pop(0)
        scanned += 1

        try:
            if not control.is_visible():
                continue
        except Exception:  # noqa: BLE001 - some wrappers lack visibility info
            pass

        try:
            add(control.window_text())
        except Exception:  # noqa: BLE001
            pass

        try:
            add(control.iface_value.CurrentValue)
        except Exception:  # noqa: BLE001
            pass

        # UIA TextPattern exposes document/browser text even when the root
        # window itself has no value.
        try:
            add(control.iface_text.DocumentRange.GetText(-1))
        except Exception:  # noqa: BLE001
            pass

        try:
            queue.extend(control.children())
        except Exception:  # noqa: BLE001
            pass

    return "\n".join(chunks)[:_MAX_TEXT_CHARS], scanned


def _extract_window_text(hwnd: int, window_title: str) -> tuple[str, int, str]:
    from pywinauto import Desktop

    last_error: Exception | None = None

    for backend in ("uia", "win32"):
        try:
            desktop = Desktop(backend=backend)
            wrapper = desktop.window(handle=hwnd).wrapper_object()
            text, scanned = _window_text(wrapper)
            if text.strip():
                return text, scanned, backend
        except Exception as exc:  # noqa: BLE001 - try the next backend
            last_error = exc
            logger.debug(
                "Could not read %r with %s: %s",
                window_title,
                backend,
                exc,
            )

    if last_error is not None:
        raise WindowToolError(
            f"Could not read UI Automation content from {window_title!r}: "
            f"{last_error}"
        )

    return "", 0, "uia"


def read_window_content(window_title: str) -> dict[str, Any]:
    """Extract readable text from an open native window using UI Automation."""
    requested = str(window_title or "").strip()
    if not requested:
        raise WindowToolError("A non-empty window title is required.")

    matches = _matching_windows(requested)
    if not matches:
        try:
            titles = [
                str(window.title).strip()
                for window in _visible_windows()
                if str(getattr(window, "title", "") or "").strip()
            ][:20]
        except WindowToolError:
            titles = []

        suffix = f" Open windows: {titles}" if titles else ""
        raise WindowToolError(
            f"No visible window matches {requested!r}.{suffix}"
        )

    best = min(
        matches,
        key=lambda window: (
            str(window.title).strip().casefold() != requested.casefold(),
            len(str(window.title)),
            not bool(getattr(window, "isActive", False)),
        ),
    )
    actual_title = str(best.title).strip()
    hwnd = getattr(best, "_hWnd", None)

    if not hwnd:
        raise WindowToolError(
            f"Could not obtain a window handle for {actual_title!r}."
        )

    text, scanned, backend = _extract_window_text(int(hwnd), actual_title)

    if text.strip():
        message = f"Read {len(text)} characters from {actual_title!r}."
    else:
        message = (
            f"No readable UI Automation text was found in {actual_title!r}. "
            "The application may not expose its content; use analyze_screen "
            "to inspect the visible screen instead."
        )

    return {
        "ok": True,
        "window_title": actual_title,
        "text": text,
        "char_count": len(text),
        "controls_scanned": scanned,
        "backend": backend,
        "message": message,
    }
