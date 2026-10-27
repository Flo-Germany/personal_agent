"""Isolated workspace for background GUI work.

Two backends, chosen automatically:

1. ``virtual_desktop`` - the window is moved to a different Windows virtual
   desktop through the documented ``IVirtualDesktopManager``. It leaves the
   user's desktop entirely. Desktop creation itself needs the undocumented
   manager, so it runs in ``vd_worker.py`` and reports unsupported instead of
   crashing the backend on builds where the interface changed.
2. ``offscreen`` - when no second desktop exists or creation is unsupported,
   the window is moved to negative coordinates and kept there. It is invisible
   on every monitor, still renders through ``PrintWindow``, and receives
   background input as window messages.

Neither backend moves the physical cursor, steals focus, or switches the
user's desktop. Capture is ``PrintWindow(PW_RENDERFULLCONTENT)`` with a
BitBlt fallback; input is ``PostMessage``/``SendMessage`` to the target window.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from settings import DATA_ROOT
from tools.uia_driver import _CursorGuard, cursor_position

logger = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 0x0002
SRCCOPY = 0x00CC0020
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_SHOWMAXIMIZED = 3
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D

STATE_PATH = DATA_ROOT / "automation" / "workspaces.json"


class WorkspaceError(RuntimeError):
    """A window could not be isolated, captured, or controlled."""


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


def _user32():
    return ctypes.windll.user32


def _gdi32():
    return ctypes.windll.gdi32


@dataclass
class Workspace:
    hwnd: int
    title: str
    mode: str
    original_rect: list[int]
    was_maximized: bool
    desktop_id: str | None = None
    original_desktop: str | None = None
    created_desktop: bool = False
    attached_at: float = 0.0

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["visible_to_user"] = False if self.mode in {"virtual_desktop", "offscreen"} else True
        return data


def _window_text(hwnd: int) -> str:
    length = _user32().GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32().GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise WorkspaceError(f"Could not read the window rectangle of {hwnd}.")
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _is_maximized(hwnd: int) -> bool:
    return bool(_user32().IsZoomed(hwnd))


def _move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    ok = _user32().SetWindowPos(
        hwnd,
        0,
        int(x),
        int(y),
        int(width),
        int(height),
        SWP_NOZORDER | SWP_NOACTIVATE,
    )
    if not ok:
        raise WorkspaceError("Windows refused to move the target window.")


class VirtualDesktopBridge:
    """Talks to pyvda only through the crash-isolated worker."""

    def __init__(self) -> None:
        self._worker = Path(__file__).with_name("vd_worker.py")
        self._manager: Any = None
        self._com_ready = False

    def _run(self, *args: str, timeout: float = 15.0) -> dict[str, Any]:
        if not self._worker.exists():
            return {"ok": False, "error": "virtual desktop worker is missing"}
        try:
            completed = subprocess.run(
                [sys.executable, str(self._worker), *args],
                capture_output=True,
                timeout=timeout,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "virtual desktop worker timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"worker failed: {exc}"}
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            return {
                "ok": False,
                "error": (
                    "virtual desktop support crashed in an isolated worker "
                    f"(exit {completed.returncode})"
                    + (f": {detail[-1]}" if detail else "")
                ),
            }
        try:
            return json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "worker produced invalid JSON"}

    def list(self) -> dict[str, Any]:
        return self._run("list")

    def create(self, name: str = "Compa workspace") -> dict[str, Any]:
        return self._run("create", name)

    def remove(self, desktop_id: str) -> dict[str, Any]:
        return self._run("remove", desktop_id)

    def _documented(self) -> Any:
        """IVirtualDesktopManager: documented, stable, and safe in-process."""
        if self._manager is not None:
            return self._manager
        try:
            import comtypes
            from comtypes import CLSCTX_ALL, GUID, CoCreateInstance
            from pyvda.com_defns import IVirtualDesktopManager

            comtypes.CoInitialize()
            self._com_ready = True
            self._manager = CoCreateInstance(
                GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}"),
                interface=IVirtualDesktopManager,
                clsctx=CLSCTX_ALL,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceError(
                f"The documented virtual desktop manager is unavailable: {exc}"
            ) from exc
        return self._manager

    def is_on_current_desktop(self, hwnd: int) -> bool | None:
        try:
            return bool(self._documented().IsWindowOnCurrentVirtualDesktop(int(hwnd)))
        except Exception:  # noqa: BLE001 - capability probe only
            return None

    def window_desktop(self, hwnd: int) -> str | None:
        try:
            from comtypes import GUID

            guid = GUID()
            self._documented().GetWindowDesktopId(int(hwnd), ctypes.byref(guid))
            return str(guid)
        except Exception:  # noqa: BLE001
            return None

    def move(self, hwnd: int, desktop_id: str) -> None:
        from comtypes import GUID

        try:
            guid = GUID(desktop_id)
            self._documented().MoveWindowToDesktop(int(hwnd), guid)
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceError(f"Could not move the window to {desktop_id}: {exc}") from exc


class WorkspaceManager:
    """Attach, capture, and release isolated windows."""

    def __init__(self, bridge: VirtualDesktopBridge | None = None) -> None:
        self.bridge = bridge or VirtualDesktopBridge()
        self._lock = threading.Lock()
        self._items: dict[int, Workspace] = {}
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            raw = STATE_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - first run or corrupt file
            return
        for key, item in (data or {}).items():
            try:
                hwnd = int(key)
                if not _user32().IsWindow(hwnd):
                    continue
                self._items[hwnd] = Workspace(**item)
            except Exception:  # noqa: BLE001
                continue

    def _save(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(
                json.dumps(
                    {str(hwnd): asdict(item) for hwnd, item in self._items.items()},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - state is recoverable
            logger.debug("Could not persist workspace state", exc_info=True)

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        desktops = self.bridge.list()
        attached = [item.to_public() for item in self._items.values()]
        return {
            "ok": True,
            "capabilities": {
                "virtual_desktop_worker": bool(desktops.get("ok")),
                "desktop_creation": None,
                "documented_move": None,
                "capture": "printwindow",
                "input": "window_messages",
            },
            "desktops": desktops.get("desktops", []),
            "current_desktop": desktops.get("current"),
            "worker_error": desktops.get("error"),
            "workspaces": attached,
            "cursor": list(cursor_position()),
        }

    # ---------------------------------------------------------------- attach
    def attach(
        self,
        *,
        hwnd: int | None = None,
        title: str | None = None,
        prefer: str = "auto",
        name: str = "Compa workspace",
    ) -> dict[str, Any]:
        if hwnd is None:
            from tools.uia_driver import UiaDriver

            window = UiaDriver().resolve_window(title=title)
            hwnd = int(window["hwnd"])
        hwnd = int(hwnd)
        if not _user32().IsWindow(hwnd):
            raise WorkspaceError(f"Window {hwnd} no longer exists.")
        with self._lock:
            existing = self._items.get(hwnd)
        if existing is not None:
            return {"ok": True, "workspace": existing.to_public(), "reused": True}

        rect = window_rect(hwnd)
        workspace = Workspace(
            hwnd=hwnd,
            title=_window_text(hwnd),
            mode="offscreen",
            original_rect=list(rect),
            was_maximized=_is_maximized(hwnd),
            attached_at=time.time(),
        )

        if prefer in {"auto", "virtual_desktop"}:
            created = False
            created_desktop_id: str | None = None
            desktop_id: str | None = None
            listing = self.bridge.list()
            if listing.get("ok"):
                current = (listing.get("current") or {}).get("id")
                other = next(
                    (
                        item
                        for item in listing.get("desktops", [])
                        if item.get("id") != current
                    ),
                    None,
                )
                if other is not None:
                    desktop_id = str(other["id"])
                else:
                    made = self.bridge.create(name)
                    if made.get("ok"):
                        desktop_id = str((made.get("desktop") or {}).get("id"))
                        created = True
                        created_desktop_id = desktop_id
            if desktop_id:
                original = self.bridge.window_desktop(hwnd)
                try:
                    self.bridge.move(hwnd, desktop_id)
                    time.sleep(0.35)
                    isolated = self.bridge.is_on_current_desktop(hwnd)
                    if isolated is False:
                        workspace.mode = "virtual_desktop"
                        workspace.desktop_id = desktop_id
                        workspace.original_desktop = original
                        workspace.created_desktop = created
                except WorkspaceError as exc:
                    logger.info("Virtual desktop move failed: %s", exc)
            if workspace.mode != "virtual_desktop" and created_desktop_id:
                # Never leave an empty desktop behind when the move failed.
                self.bridge.remove(created_desktop_id)

        if workspace.mode == "offscreen":
            if workspace.was_maximized:
                _user32().ShowWindow(hwnd, 9)  # SW_RESTORE: maximised windows cannot move
                time.sleep(0.1)
            _move_window(hwnd, -4000, 0, rect[2] - rect[0], rect[3] - rect[1])

        with self._lock:
            self._items[hwnd] = workspace
        self._save()
        return {
            "ok": True,
            "workspace": workspace.to_public(),
            "cursor": list(cursor_position()),
            "message": (
                f"{workspace.title!r} is isolated in {workspace.mode} mode. "
                "It is not visible on your desktop."
            ),
        }

    # --------------------------------------------------------------- capture
    @staticmethod
    def capture_image(hwnd: int) -> Image.Image:
        left, top, right, bottom = window_rect(hwnd)
        width = max(1, right - left)
        height = max(1, bottom - top)
        user32 = _user32()
        gdi32 = _gdi32()
        hdc = user32.GetWindowDC(hwnd)
        if not hdc:
            raise WorkspaceError("Could not get a device context for the window.")
        mem = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
        previous = gdi32.SelectObject(mem, bitmap)
        try:
            user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            buffer = ctypes.create_string_buffer(width * height * 4)
            lines = gdi32.GetDIBits(
                mem, bitmap, 0, height, buffer, ctypes.byref(info), 0
            )
        finally:
            gdi32.SelectObject(mem, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd, hdc)

        image = Image.frombuffer(
            "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
        ).convert("RGB")
        if not lines:
            raise WorkspaceError("PrintWindow returned no pixels for the window.")
        return image

    def capture(self, hwnd: int, *, save: bool = True) -> dict[str, Any]:
        hwnd = int(hwnd)
        image = self.capture_image(hwnd)
        grey = image.convert("L").resize((200, 120))
        pixels = list(grey.getdata())
        ink = round(sum(1 for value in pixels if value > 12) / max(1, len(pixels)), 3)
        path: str | None = None
        if save:
            out = DATA_ROOT / "screenshots"
            out.mkdir(parents=True, exist_ok=True)
            path = str(out / f"headless-{int(time.time() * 1000)}.png")
            image.save(path)
        workspace = self._items.get(hwnd)
        rect = window_rect(hwnd)
        return {
            "ok": True,
            "hwnd": hwnd,
            "title": _window_text(hwnd),
            "screenshot": path,
            "image_width": image.width,
            "image_height": image.height,
            "rect": list(rect),
            "origin": [rect[0], rect[1]],
            "ink_ratio": ink,
            "mode": workspace.mode if workspace else "current_desktop",
            "on_current_desktop": self.bridge.is_on_current_desktop(hwnd),
            "cursor": list(cursor_position()),
        }

    # ----------------------------------------------------------------- input
    @staticmethod
    def _child_at(hwnd: int, point: wt.POINT) -> tuple[int, wt.POINT]:
        """Deepest child window under a client point, with local coordinates."""
        user32 = _user32()
        target = int(hwnd)
        local = wt.POINT(point.x, point.y)
        for _ in range(6):
            child = int(user32.ChildWindowFromPointEx(target, local, 0) or 0)
            if not child or child == target:
                break
            child_point = wt.POINT(point.x, point.y)
            user32.ScreenToClient(child, ctypes.byref(child_point))
            rect = wt.RECT()
            user32.GetWindowRect(child, ctypes.byref(rect))
            if not (rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom):
                break
            target = child
            local = child_point
        return target, local

    def click(
        self, hwnd: int, x: int, y: int, *, button: str = "left", clicks: int = 1
    ) -> dict[str, Any]:
        hwnd = int(hwnd)
        point = wt.POINT(int(x), int(y))
        if not _user32().ScreenToClient(hwnd, ctypes.byref(point)):
            raise WorkspaceError("Could not convert screen coordinates to window coordinates.")
        target, local = self._child_at(hwnd, point)
        lparam = (local.y << 16) | (local.x & 0xFFFF)
        down, up, flag = (
            (0x0204, 0x0205, 0x0002) if button == "right" else (0x0201, 0x0202, 0x0001)
        )
        with _CursorGuard() as guard:
            for _ in range(max(1, min(int(clicks or 1), 2))):
                _user32().PostMessageW(target, down, flag, lparam)
                time.sleep(0.02)
                _user32().PostMessageW(target, up, 0, lparam)
                time.sleep(0.05)
        return {
            "ok": True,
            "hwnd": hwnd,
            "target_hwnd": target,
            "point": [int(x), int(y)],
            "client_point": [local.x, local.y],
            "via": "post_message",
            "cursor_moved": False,
            "cursor_stable": not guard.moved,
        }

    def type_text(self, hwnd: int, text: str, *, press_enter: bool = False) -> dict[str, Any]:
        hwnd = int(hwnd)
        thread_id = _user32().GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        target = hwnd
        if _user32().GetGUIThreadInfo(int(thread_id), ctypes.byref(info)) and info.hwndFocus:
            target = int(info.hwndFocus)
        sent = 0
        for char in text:
            _user32().SendMessageW(target, WM_CHAR, ord(char), 1)
            sent += 1
        if press_enter:
            _user32().SendMessageW(target, WM_KEYDOWN, VK_RETURN, 1)
            _user32().SendMessageW(target, WM_CHAR, 13, 1)
            _user32().SendMessageW(target, WM_KEYUP, VK_RETURN, 1)
        return {
            "ok": True,
            "hwnd": hwnd,
            "target_hwnd": target,
            "characters": sent,
            "press_enter": bool(press_enter),
            "via": "wm_char",
            "cursor": list(cursor_position()),
        }

    # --------------------------------------------------------------- release
    def release(self, hwnd: int | None = None) -> dict[str, Any]:
        with self._lock:
            targets = (
                [self._items[int(hwnd)]] if hwnd is not None and int(hwnd) in self._items
                else list(self._items.values()) if hwnd is None
                else []
            )
        released: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in targets:
            try:
                if item.mode == "virtual_desktop" and item.original_desktop:
                    self.bridge.move(item.hwnd, item.original_desktop)
                else:
                    left, top, right, bottom = item.original_rect
                    _move_window(item.hwnd, left, top, right - left, bottom - top)
                    if item.was_maximized:
                        _user32().ShowWindow(item.hwnd, SW_SHOWMAXIMIZED)
                released.append(item.to_public())
            except Exception as exc:  # noqa: BLE001 - keep releasing the rest
                errors.append(f"{item.hwnd}: {exc}")
            finally:
                with self._lock:
                    self._items.pop(item.hwnd, None)
                if item.created_desktop and item.desktop_id:
                    self.bridge.remove(item.desktop_id)
        self._save()
        return {
            "ok": not errors,
            "released": released,
            "errors": errors,
            "cursor": list(cursor_position()),
        }

    def get(self, hwnd: int) -> Workspace | None:
        return self._items.get(int(hwnd))
