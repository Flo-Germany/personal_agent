"""Smooth, visible pointer control with a pre-move highlight.

Every action is wrapped by the :class:`InputFailsafe`. The guard is checked
between interpolation steps, so a physical mouse movement stops the glide
within a few milliseconds instead of after the full animation.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import string
import threading
import time
from typing import Any, Iterable

from settings import AutomationSettings
from tools.input_failsafe import AutomationAbortedException, InputFailsafe

logger = logging.getLogger(__name__)

# Characters that pyautogui can type safely on common layouts without
# Shift/AltGr ambiguity. Everything else uses the clipboard paste path.
_SAFE_TYPEWRITE = set(
    string.ascii_letters + string.digits + " .,;-_/\\'"
)


def _needs_clipboard(text: str) -> bool:
    return (not text.isascii()) or any(
        character not in _SAFE_TYPEWRITE for character in text
    )


_ALLOWED_KEYS = {
    "enter",
    "return",
    "tab",
    "esc",
    "escape",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "up",
    "down",
    "left",
    "right",
    "ctrl",
    "control",
    "alt",
    "shift",
    "win",
    "cmd",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
} | {chr(code) for code in range(ord("a"), ord("z") + 1)} | {
    str(number) for number in range(10)
}


class OverlayHighlighter:
    """Brief red ring around the target, drawn with a transparent Tk overlay.

    The overlay lives on one background thread and is click-through, so showing
    it never blocks the cursor glide and never intercepts the click that
    follows. Earlier versions created a Tk root and joined the thread for the
    full highlight duration, which added about half a second to every action.
    """

    # Extended window styles that let mouse input fall through the overlay.
    _GWL_EXSTYLE = -20
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_NOACTIVATE = 0x08000000
    _WS_EX_TOOLWINDOW = 0x00000080

    def __init__(self, settings: AutomationSettings) -> None:
        self.settings = settings
        self._requests: queue.Queue[tuple[int, int, float] | None] = queue.Queue(
            maxsize=1
        )
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._broken = False
        self._hide_requested = threading.Event()
        self._visible = threading.Event()

    def show(self, x: int, y: int, duration: float | None = None) -> None:
        seconds = (
            self.settings.highlight_seconds
            if duration is None
            else max(0.0, float(duration))
        )
        if seconds <= 0 or self._broken:
            return

        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="agent-cursor-highlight",
                    daemon=True,
                )
                self._worker.start()

        self._hide_requested.clear()
        request = (int(x), int(y), seconds)
        try:
            self._requests.put_nowait(request)
        except queue.Full:
            # A newer target supersedes a ring that has not been drawn yet.
            try:
                self._requests.get_nowait()
            except queue.Empty:
                pass
            try:
                self._requests.put_nowait(request)
            except queue.Full:
                pass

    def hide(self, timeout: float = 0.3) -> None:
        """Withdraw the ring before a synthetic click.

        The ring is click-through on Windows, but hiding it first makes the
        click deterministic even if the OS refuses the extended style.
        """
        if self._worker is None or not self._worker.is_alive():
            return
        self._hide_requested.set()
        # Drop a ring that was queued but not drawn yet.
        try:
            self._requests.get_nowait()
        except queue.Empty:
            pass
        deadline = time.monotonic() + max(0.0, timeout)
        while self._visible.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            size = 74
            window = tk.Toplevel(root)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            try:
                window.attributes("-alpha", 0.55)
            except tk.TclError:
                pass

            background = "black"
            try:
                background = "#010203"
                window.attributes("-transparentcolor", background)
            except tk.TclError:
                background = "black"

            canvas = tk.Canvas(
                window,
                width=size,
                height=size,
                bg=background,
                highlightthickness=0,
            )
            canvas.pack()
            canvas.create_oval(
                4,
                4,
                size - 4,
                size - 4,
                outline="#ff3b30",
                width=3,
            )
            center = size // 2
            canvas.create_oval(
                center - 4,
                center - 4,
                center + 4,
                center + 4,
                fill="#ff3b30",
                outline="",
            )
            window.withdraw()
            # Create the native window once before the first request.
            window.geometry(f"{size}x{size}-10000-10000")
            window.deiconify()
            window.update_idletasks()
            window.withdraw()
        except Exception:  # noqa: BLE001 - a failed highlight must not fail an action
            logger.debug("Cursor highlight overlay failed to initialise", exc_info=True)
            self._broken = True
            return

        while True:
            request = self._requests.get()
            if request is None:
                break
            x, y, duration = request
            try:
                window.geometry(f"{size}x{size}{x - size // 2:+d}{y - size // 2:+d}")
                window.deiconify()
                window.update_idletasks()
                # Tk re-applies its own window styles on map, so apply the
                # click-through bits after every deiconify.
                self._make_click_through(window)
                self._visible.set()
                deadline = time.monotonic() + duration
                while (
                    time.monotonic() < deadline
                    and not self._hide_requested.is_set()
                ):
                    root.update()
                    time.sleep(0.01)
                window.withdraw()
                self._visible.clear()
                root.update()
            except Exception:  # noqa: BLE001 - keep the worker alive
                logger.debug("Cursor highlight draw failed", exc_info=True)
                self._visible.clear()

        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def _make_click_through(cls, window: Any) -> None:
        if os.name != "nt":
            return
        try:
            window.update_idletasks()
            child = int(window.winfo_id())
            user32 = ctypes.windll.user32
            get_style = user32.GetWindowLongW
            get_style.restype = ctypes.c_long
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
            set_style = user32.SetWindowLongW
            set_style.restype = ctypes.c_long
            set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            get_ancestor = user32.GetAncestor
            get_ancestor.restype = ctypes.c_void_p
            get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]

            # winfo_id() is Tk's child window; the visible top-level wrapper is
            # its GA_ROOT ancestor. Both need the transparent hit-test bits.
            top = int(get_ancestor(child, 2) or 0) or child
            bits = (
                cls._WS_EX_LAYERED
                | cls._WS_EX_TRANSPARENT
                | cls._WS_EX_NOACTIVATE
                | cls._WS_EX_TOOLWINDOW
            )
            for hwnd in {child, top}:
                style = int(get_style(hwnd, cls._GWL_EXSTYLE))
                set_style(hwnd, cls._GWL_EXSTYLE, style | bits)
        except Exception:  # noqa: BLE001 - best effort, highlights still work
            logger.debug("Could not make the highlight click-through", exc_info=True)


class SmoothPointer:
    """Visible cursor movement, clicking, and typing guarded by the killswitch."""

    def __init__(
        self,
        settings: AutomationSettings,
        guard: InputFailsafe,
        highlighter: OverlayHighlighter | None = None,
    ) -> None:
        self.settings = settings
        self.guard = guard
        self.highlighter = highlighter or OverlayHighlighter(settings)
        self._pyautogui: Any | None = None

    def _load(self) -> Any:
        if self._pyautogui is None:
            import pyautogui

            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.02
            self._pyautogui = pyautogui
        return self._pyautogui

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        """Return (left, top, right, bottom) of the virtual desktop."""
        if os.name == "nt":
            try:
                user32 = ctypes.windll.user32
                left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
                top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
                width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
                height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
                if width > 0 and height > 0:
                    return left, top, left + width, top + height
            except Exception:  # noqa: BLE001
                logger.debug("Could not read virtual screen bounds", exc_info=True)
        page = self._load()
        width, height = page.size()
        return 0, 0, int(width), int(height)

    def _validate_point(self, x: int, y: int) -> tuple[int, int]:
        left, top, right, bottom = self.virtual_bounds()
        if not (left <= x < right and top <= y < bottom):
            raise ValueError(
                f"Target ({x}, {y}) is outside the virtual desktop "
                f"({left}, {top}) - ({right}, {bottom})."
            )
        return int(x), int(y)

    def move_to(
        self,
        x: int,
        y: int,
        *,
        duration: float | None = None,
        highlight: bool = True,
    ) -> None:
        page = self._load()
        x, y = self._validate_point(int(x), int(y))
        self.guard.check()

        try:
            start_x, start_y = page.position()
        except Exception:  # noqa: BLE001
            start_x, start_y = x, y

        if duration is None:
            # Short hops do not need most of the configured budget; long
            # glides still stay smooth and visible.
            distance = math.hypot(x - start_x, y - start_y)
            seconds = min(
                float(self.settings.move_duration_seconds),
                0.12 + distance / 3000.0,
            )
        else:
            seconds = float(duration)
        seconds = max(0.12, min(5.0, seconds))

        if highlight:
            # The ring is click-through and drawn on a background thread, so it
            # can overlap the glide instead of delaying it.
            self.highlighter.show(x, y, self.settings.highlight_seconds)
            self.guard.check()

        steps = max(8, min(90, int(seconds * 45)))
        for index in range(1, steps + 1):
            self.guard.check()
            fraction = index / steps
            next_x = int(round(start_x + (x - start_x) * fraction))
            next_y = int(round(start_y + (y - start_y) * fraction))
            try:
                with self.guard.synthetic():
                    page.moveTo(next_x, next_y, duration=0, _pause=False)
            except Exception as exc:  # pyautogui.FailSafeException and OS errors
                raise AutomationAbortedException(
                    f"Cursor movement stopped: {exc}"
                ) from exc
            time.sleep(seconds / steps)

        self.guard.check()
        try:
            with self.guard.synthetic():
                page.moveTo(x, y, duration=0, _pause=False)
        except Exception as exc:  # noqa: BLE001
            raise AutomationAbortedException(
                f"Cursor movement stopped: {exc}"
            ) from exc

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        duration: float | None = None,
        highlight: bool = True,
    ) -> None:
        page = self._load()
        button = str(button or "left").strip().lower()
        if button not in {"left", "right", "middle"}:
            raise ValueError("button must be left, right, or middle")
        try:
            count = max(1, min(2, int(clicks)))
        except (TypeError, ValueError):
            count = 1

        self.move_to(x, y, duration=duration, highlight=highlight)
        self.guard.check()
        # Never let the highlight overlay sit between the pointer and the
        # target when the synthetic click is delivered.
        self.highlighter.hide()
        self.guard.check()
        try:
            with self.guard.synthetic():
                page.click(
                    x=int(x),
                    y=int(y),
                    clicks=count,
                    interval=0.12,
                    button=button,
                    _pause=False,
                )
        except Exception as exc:  # noqa: BLE001
            raise AutomationAbortedException(f"Click stopped: {exc}") from exc
        self.guard.check()

    def type_text(
        self,
        text: str,
        *,
        interval: float | None = None,
        press_enter: bool = False,
    ) -> None:
        page = self._load()
        value = str(text)
        delay = (
            self.settings.type_interval_seconds
            if interval is None
            else max(0.0, float(interval))
        )

        self.guard.check()
        # Long strings are pasted even when every character is layout-safe:
        # typing 200 characters costs several seconds and risks dropped keys.
        if _needs_clipboard(value) or len(value) >= 48:
            if not self._paste_unicode(page, value):
                if _needs_clipboard(value):
                    raise ValueError(
                        "Typing non-ASCII text requires the pyperclip package."
                    )
                self._type_ascii(page, value, delay)
        else:
            self._type_ascii(page, value, delay)

        if press_enter:
            self.guard.check()
            with self.guard.synthetic():
                page.press("enter", _pause=False)
            self.guard.check()

    def _type_ascii(self, page: Any, text: str, delay: float) -> None:
        lines = text.split("\n")
        for line_index, line in enumerate(lines):
            for character in line:
                self.guard.check()
                with self.guard.synthetic():
                    page.write(character, interval=0, _pause=False)
                if delay:
                    time.sleep(delay)
            if line_index < len(lines) - 1:
                self.guard.check()
                with self.guard.synthetic():
                    page.press("enter", _pause=False)
                if delay:
                    time.sleep(delay)

    def _paste_unicode(self, page: Any, text: str) -> bool:
        try:
            import pyperclip
        except Exception as exc:  # noqa: BLE001
            logger.debug("pyperclip is unavailable: %s", exc)
            return False

        previous: str | None = None
        try:
            previous = pyperclip.paste()
        except Exception:  # noqa: BLE001
            previous = None

        try:
            pyperclip.copy(text)
        except Exception:  # noqa: BLE001 - clipboard may be locked
            logger.debug("Could not copy text for paste", exc_info=True)
            return False

        self.guard.check()
        try:
            with self.guard.synthetic():
                page.hotkey("ctrl", "v", _pause=False)
            time.sleep(0.25)
            self.guard.check()
        finally:
            if previous is not None:
                try:
                    pyperclip.copy(previous)
                except Exception:  # noqa: BLE001
                    pass
        return True

    def press_keys(
        self,
        keys: Iterable[str],
        *,
        interval: float | None = None,
    ) -> None:
        page = self._load()
        cleaned = [
            str(key).strip().lower()
            for key in keys
            if str(key).strip()
        ]
        if not cleaned:
            raise ValueError("At least one key is required.")
        if len(cleaned) > 5:
            raise ValueError("At most five keys can be pressed together.")
        invalid = [key for key in cleaned if key not in _ALLOWED_KEYS]
        if invalid:
            raise ValueError(
                f"Unsupported key(s): {', '.join(invalid)}. "
                "Use letters, digits, arrows, Enter, Tab, Esc, F1-F12, "
                "Ctrl, Alt, Shift, or Win."
            )

        delay = (
            self.settings.type_interval_seconds
            if interval is None
            else max(0.0, float(interval))
        )
        self.guard.check()
        with self.guard.synthetic():
            if len(cleaned) == 1:
                page.press(cleaned[0], _pause=False)
            else:
                page.hotkey(*cleaned, _pause=False)
        if delay:
            time.sleep(delay)
        self.guard.check()
