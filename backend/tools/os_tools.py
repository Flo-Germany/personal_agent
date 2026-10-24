"""Full-filesystem Windows tools with HITL safety flags.

Modifying operations must be confirmed by the user before execution. Read-only
operations run immediately. Destructive operations use the Recycle Bin, and
critical Windows locations are hard-denied even after approval.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import stat as stat_module
import string
import subprocess
import time
import zipfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from send2trash import send2trash

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only application
    winreg = None

logger = logging.getLogger(__name__)

# Tools that change the system and therefore require explicit approval.
TOOL_REQUIRES_CONFIRMATION = {
    "start_program": True,
    "open_file": True,
    "create_folder": True,
    "create_file": True,
    "copy_file": True,
    "unzip_file": True,
    "move_file_or_folder": True,
    "delete_file_or_folder": True,
    "read_file_content": False,
    "search_system": False,
    "semantic_image_search": False,
    "get_open_windows": False,
    "read_window_content": False,
    "analyze_screen": False,
}
_PROGRAM_NAMES = {"calculator", "calc", "notepad", "explorer", "paint", "mspaint"}

_MAX_SEARCH_RESULTS = 200
_MAX_SCANNED_FILES = 100_000
_MAX_SEARCH_SECONDS = 60.0
_MAX_READ_BYTES = 5_000_000
_MAX_WRITE_BYTES = 1_000_000
_MAX_UNZIP_MEMBERS = 2000
_MAX_UNZIP_BYTES = 500_000_000

_SKIP_DIR_NAMES = {
    "$Recycle.Bin",
    "System Volume Information",
    "WinSxS",
}

# High-confidence credential formats. Redacting these keeps an accidental read
# of a config or secret file from pasting a usable key into the chat history.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def _redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pattern in _SECRET_PATTERNS:
        text, replacements = pattern.subn("[REDACTED]", text)
        count += replacements
    return text, count

# Aliases used by the model and by localized Windows installs. The registry
# values point at the real known folders, including OneDrive redirection.
_KNOWN_FOLDER_ALIASES = {
    "documents": "Personal",
    "dokumente": "Personal",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "download": "{374DE290-123F-4565-9164-39C4925E467B}",
    "desktop": "Desktop",
    "schreibtisch": "Desktop",
    "pictures": "My Pictures",
    "bilder": "My Pictures",
    "music": "My Music",
    "musik": "My Music",
    "videos": "My Video",
    "video": "My Video",
}


@lru_cache(maxsize=None)
def _known_folder(alias: str) -> Path | None:
    """Resolve a known Windows folder from the per-user Shell Folders key."""
    value_name = _KNOWN_FOLDER_ALIASES.get(alias)
    if not value_name or winreg is None:
        return None

    key_path = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None

    try:
        path = Path(os.path.expandvars(str(value)))
    except (OSError, ValueError):
        return None
    return path if path.exists() else None


def _map_known_folder(path: Path) -> Path:
    """Map Documents/Downloads/... under the profile to the real known folder."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return path

    parts = relative.parts
    if not parts:
        return path

    known = _known_folder(parts[0].lower())
    if known is None:
        return path
    return known.joinpath(*parts[1:])


class ToolError(RuntimeError):
    """A user-facing tool failure that is safe to show to the model."""


def requires_confirmation(tool_name: str) -> bool:
    """Unknown tools are treated as modifying (fail closed)."""
    return TOOL_REQUIRES_CONFIRMATION.get(tool_name, True)


def _windows_root() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows"))


def _system32() -> Path:
    return _windows_root() / "System32"


def _program_path(key: str) -> Path:
    system32 = _system32()

    if key in {"calculator", "calc"}:
        return system32 / "calc.exe"
    if key == "notepad":
        return system32 / "notepad.exe"
    if key == "explorer":
        return _windows_root() / "explorer.exe"

    candidates = [system32 / "mspaint.exe"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Microsoft" / "WindowsApps" / "mspaint.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


_PROGRAM_SUFFIXES = {".exe", ".com", ".lnk"}
_COMMON_PROGRAM_KEYS = {
    "chrome": ("chrome", "google chrome", "googlechrome"),
    "msedge": ("edge", "microsoft edge", "msedge"),
    "code": ("code", "vs code", "visual studio code"),
    "firefox": ("firefox", "mozilla firefox"),
}


def _common_program_path(key: str) -> Path | None:
    env = os.environ
    program_files = Path(env.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local_app_data = Path(env.get("LOCALAPPDATA", ""))

    candidates: list[Path] = []

    if key in _COMMON_PROGRAM_KEYS["chrome"]:
        candidates.extend(
            [
                program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
                program_files_x86
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe",
            ]
        )
    elif key in _COMMON_PROGRAM_KEYS["msedge"]:
        candidates.extend(
            [
                program_files_x86
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe",
                program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
        for base in (
            program_files_x86 / "Microsoft" / "EdgeCore",
            program_files / "Microsoft" / "EdgeCore",
        ):
            if not base.is_dir():
                continue
            try:
                versions = sorted(
                    (entry for entry in base.iterdir() if entry.is_dir()),
                    reverse=True,
                )
            except OSError:
                continue
            for version in versions:
                candidate = version / "msedge.exe"
                if candidate.is_file():
                    candidates.append(candidate)
                    break
    elif key in _COMMON_PROGRAM_KEYS["code"]:
        candidates.append(
            local_app_data / "Programs" / "Microsoft VS Code" / "Code.exe"
        )
    elif key in _COMMON_PROGRAM_KEYS["firefox"]:
        candidates.extend(
            [
                program_files / "Mozilla Firefox" / "firefox.exe",
                program_files_x86 / "Mozilla Firefox" / "firefox.exe",
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    return None


def _registry_program_path(name: str) -> Path | None:
    if winreg is None:
        return None

    keys = [name]
    if not name.casefold().endswith(".exe"):
        keys.insert(0, f"{name}.exe")

    base = r"Software\Microsoft\Windows\CurrentVersion\App Paths"

    for key_name in keys:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, f"{base}\\{key_name}") as key:
                    value, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue

            try:
                candidate = Path(os.path.expandvars(str(value))).expanduser()
            except (OSError, ValueError):
                continue

            if candidate.is_file():
                return candidate.resolve()

    return None


def _shortcut_target(shortcut: Path) -> Path | None:
    try:
        import win32com.client
    except Exception:  # noqa: BLE001 - optional COM dependency
        return None

    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        target = Path(shell.CreateShortcut(str(shortcut)).TargetPath)
    except Exception:  # noqa: BLE001
        return None

    return target.resolve() if target.is_file() else None


def _start_menu_shortcut(name: str) -> Path | None:
    env = os.environ
    roots = [
        Path(env.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(env.get("PROGRAMDATA", ""))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs",
    ]
    requested = Path(name).stem.casefold()

    for root in roots:
        if not root.is_dir():
            continue

        try:
            shortcuts = root.rglob("*.lnk")
        except OSError:
            continue

        for shortcut in shortcuts:
            if shortcut.stem.casefold() == requested:
                target = _shortcut_target(shortcut)
                return target or shortcut.resolve()

    return None


def _resolve_program(program: str) -> Path:
    """Resolve an alias, executable name, Start Menu app, or absolute path."""
    raw = str(program or "").strip().strip('"')
    if not raw:
        raise ToolError("A program name or executable path is required.")

    key = raw.casefold()
    alias_key = key[:-4] if key.endswith(".exe") else key

    if alias_key in _PROGRAM_NAMES:
        candidate = _program_path(alias_key)
        if candidate.is_file():
            return candidate.resolve()

    candidate = Path(os.path.expandvars(raw)).expanduser()

    if candidate.is_file():
        if candidate.suffix.casefold() not in _PROGRAM_SUFFIXES:
            raise ToolError(
                f"{candidate} is not an executable program. Choose an .exe, "
                ".com, or Start Menu shortcut."
            )
        return candidate.resolve()

    if candidate.is_absolute() or any(part in raw for part in ("\\", "/")):
        raise ToolError(f"Program not found at {candidate}.")

    common = _common_program_path(alias_key)
    if common is not None:
        return common

    found = shutil.which(raw)
    if found:
        executable = Path(found).resolve()
        if executable.suffix.casefold() in _PROGRAM_SUFFIXES:
            return executable

    registry = _registry_program_path(raw)
    if registry is not None:
        return registry

    shortcut = _start_menu_shortcut(raw)
    if shortcut is not None:
        return shortcut

    raise ToolError(
        f"Could not find the program {raw!r}. Provide an absolute path to an "
        "installed .exe, or use a Start Menu application name."
    )


def start_program(program_name: str) -> dict[str, Any]:
    """Launch any installed program or executable after user approval."""
    path = _resolve_program(program_name)

    try:
        if path.suffix.casefold() == ".lnk":
            os.startfile(str(path))
        else:
            subprocess.Popen([str(path)], shell=False)
    except OSError as exc:
        raise ToolError(f"Could not start {path.name}: {exc}") from exc

    return {
        "ok": True,
        "message": f"Started {path.name}.",
        "program": str(path),
    }


def open_file(path: str, program: str) -> dict[str, Any]:
    """Open one existing file in an explicitly chosen program."""
    target = _absolute_path(path)
    if not target.exists():
        raise ToolError(f"File not found: {target}")
    if not target.is_file():
        raise ToolError(f"Not a file: {target}")

    raw_program = str(program or "").strip()
    if not raw_program:
        raise ToolError(
            "Choose the editor or program to open this file with. Ask the user "
            "which application they want; do not guess."
        )

    application = _resolve_program(raw_program)
    if application.suffix.casefold() == ".lnk":
        resolved = _shortcut_target(application)
        if resolved is None:
            raise ToolError(
                f"Could not resolve the shortcut {application.name} to an "
                "executable. Choose the application's .exe path instead."
            )
        application = resolved

    if application.suffix.casefold() not in {".exe", ".com"}:
        raise ToolError(
            f"{application} is not a direct executable. Choose the application's "
            ".exe path instead."
        )

    try:
        subprocess.Popen(
            [str(application), str(target)],
            shell=False,
        )
    except OSError as exc:
        raise ToolError(
            f"Could not open {target.name} with {application.name}: {exc}"
        ) from exc

    return {
        "ok": True,
        "message": f"Opened {target.name} with {application.name}.",
        "path": str(target),
        "program": str(application),
    }


def _protected_tree_roots() -> list[Path]:
    roots: list[Path] = [
        _windows_root(),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
        Path(r"C:\Boot"),
        Path(r"C:\Recovery"),
        Path(r"C:\System Volume Information"),
        Path(r"C:\$Recycle.Bin"),
        Path(r"C:\Windows.old"),
    ]
    return [root for root in roots if root.exists()]


def _protected_self_only_roots() -> list[Path]:
    roots: list[Path] = [Path.home(), Path.home().parent]
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if drive.exists():
            roots.append(drive)
    return roots


def _is_protected(path: Path) -> tuple[bool, Path | None]:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()

    for root in _protected_tree_roots():
        root = root.resolve(strict=False)
        if resolved == root or root in resolved.parents:
            return True, root

    for root in _protected_self_only_roots():
        root = root.resolve(strict=False)
        if resolved == root:
            return True, root

    return False, None


def _guard_modification(path: Path, action: str) -> None:
    protected, root = _is_protected(path)
    if protected:
        raise ToolError(
            f"Refusing to {action} a protected system location: {path} "
            f"(protected root: {root})."
        )


def _absolute_path(value: str | Path) -> Path:
    text = str(value).strip()
    if not text:
        raise ToolError("A non-empty path is required.")

    try:
        expanded = os.path.expandvars(os.path.expanduser(text))
        path = Path(expanded)
        if not path.is_absolute():
            path = Path.home() / path
        return _map_known_folder(Path(os.path.abspath(path)))
    except (OSError, ValueError) as exc:
        raise ToolError(f"Invalid path: {value}") from exc


def _now_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(
        timestamp if timestamp is not None else time.time()
    ).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError(f"Invalid date/time value: {value}") from exc
    return parsed.timestamp()


def _is_hidden(path: Path, stats: os.stat_result) -> bool:
    if path.name.startswith("."):
        return True
    hidden_flag = getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0)
    return bool(getattr(stats, "st_file_attributes", 0) & hidden_flag)


def create_folder(path: str) -> dict[str, Any]:
    """Create a folder anywhere outside protected system trees."""
    target = _absolute_path(path)
    _guard_modification(target, "create")

    if target.exists() and not target.is_dir():
        raise ToolError(f"A file already exists at {target}.")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"Could not create folder {target}: {exc}") from exc

    return {
        "ok": True,
        "message": f"Created folder {target}.",
        "path": str(target),
    }


def create_file(
    path: str,
    content: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a UTF-8 text file without silently overwriting existing data."""
    target = _absolute_path(path)
    _guard_modification(target, "create")

    if target.exists():
        if target.is_dir():
            raise ToolError(f"A folder already exists at {target}.")
        if not overwrite:
            raise ToolError(
                f"File already exists at {target}; set overwrite=true to replace it."
            )

    text = str(content)
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_WRITE_BYTES:
        raise ToolError(
            f"Content is too large ({len(encoded)} bytes; limit {_MAX_WRITE_BYTES})."
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Could not create file {target}: {exc}") from exc

    return {
        "ok": True,
        "message": f"Created file {target} ({len(encoded)} bytes).",
        "path": str(target),
        "bytes": len(encoded),
    }


def copy_file(source_path: str, destination_path: str) -> dict[str, Any]:
    """Copy a file. The destination must not already exist."""
    source = _absolute_path(source_path)
    destination = _absolute_path(destination_path)

    if not source.is_file():
        raise ToolError(f"Source file does not exist: {source}")
    if destination.exists() or destination.is_symlink():
        raise ToolError(f"Destination already exists: {destination}")

    _guard_modification(destination, "copy to")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except (OSError, shutil.Error) as exc:
        raise ToolError(f"Could not copy file: {exc}") from exc

    return {
        "ok": True,
        "message": f"Copied {source.name} to {destination}.",
        "source": str(source),
        "destination": str(destination),
    }


def move_file_or_folder(source_path: str, destination_path: str) -> dict[str, Any]:
    """Move a file or folder. Existing destinations are never overwritten."""
    source = _absolute_path(source_path)
    destination = _absolute_path(destination_path)

    if not source.exists() and not source.is_symlink():
        raise ToolError(f"Source does not exist: {source}")
    if destination.exists() or destination.is_symlink():
        raise ToolError(f"Destination already exists: {destination}")
    if source == destination:
        raise ToolError("Source and destination are the same path.")

    _guard_modification(source, "move")
    _guard_modification(destination, "move to")

    if source.is_dir():
        try:
            source_resolved = source.resolve(strict=False)
            destination_resolved = destination.resolve(strict=False)
            if (
                destination_resolved == source_resolved
                or source_resolved in destination_resolved.parents
            ):
                raise ToolError("Cannot move a folder into itself.")
        except OSError as exc:
            raise ToolError(f"Could not validate move: {exc}") from exc

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    except (OSError, shutil.Error) as exc:
        raise ToolError(f"Could not move: {exc}") from exc

    return {
        "ok": True,
        "message": f"Moved {source} to {destination}.",
        "source": str(source),
        "destination": str(destination),
    }


def delete_file_or_folder(path: str) -> dict[str, Any]:
    """Move a file or folder to the Windows Recycle Bin."""
    target = _absolute_path(path)

    if not target.exists() and not target.is_symlink():
        raise ToolError(f"Path does not exist: {target}")

    _guard_modification(target, "delete")

    try:
        send2trash(str(target))
    except Exception as exc:  # noqa: BLE001 - send2trash raises platform errors
        raise ToolError(f"Could not move to the Recycle Bin: {exc}") from exc

    return {
        "ok": True,
        "message": f"Moved to the Recycle Bin: {target}.",
        "path": str(target),
    }


def read_file_content(
    path: str,
    *,
    max_bytes: int = 1_000_000,
    redact_secrets: bool = True,
) -> dict[str, Any]:
    """Read a text file, or describe a binary/image file without dumping bytes."""
    target = _absolute_path(path)

    if not target.is_file():
        raise ToolError(f"Not a file: {target}")

    max_bytes = max(1, min(int(max_bytes), _MAX_READ_BYTES))
    try:
        stats = target.stat()
    except OSError as exc:
        raise ToolError(f"Could not stat file: {exc}") from exc

    if stats.st_size > max_bytes:
        raise ToolError(
            f"File is too large to read ({stats.st_size} bytes; limit {max_bytes})."
        )

    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"Could not read file: {exc}") from exc

    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
    is_image = target.suffix.lower() in image_extensions
    is_binary = is_image or b"\x00" in raw
    content = None if is_binary else raw.decode("utf-8", errors="replace")

    redacted = 0
    if content is not None and redact_secrets:
        content, redacted = _redact_secrets(content)

    message = (
        f"Read {target.name}."
        if not is_binary
        else f"{target.name} is a binary/image file; content was not decoded."
    )
    if redacted:
        message += f" Redacted {redacted} secret value(s)."

    result = {
        "ok": True,
        "message": message,
        "path": str(target),
        "size": stats.st_size,
        "modified": _now_iso(stats.st_mtime),
        "is_image": is_image,
        "binary": is_binary,
        "content": content,
    }
    if redacted:
        result["redacted"] = redacted
    return result


def unzip_file(
    archive_path: str,
    extract_to: str,
    *,
    max_bytes: int = 200_000_000,
    max_members: int = _MAX_UNZIP_MEMBERS,
) -> dict[str, Any]:
    """Safely extract a .zip archive to any non-protected folder."""
    archive = _absolute_path(archive_path)
    target_root = _absolute_path(extract_to)

    if not archive.is_file():
        raise ToolError(f"Archive does not exist: {archive}")
    if archive.suffix.lower() != ".zip":
        raise ToolError("Only .zip archives are supported.")
    if not zipfile.is_zipfile(archive):
        raise ToolError(f"Not a valid zip archive: {archive}")

    max_bytes = max(1, min(int(max_bytes), _MAX_UNZIP_BYTES))
    _guard_modification(target_root, "extract to")

    try:
        target_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > max_members:
                raise ToolError(
                    f"Archive has too many entries ({len(infos)} > {max_members})."
                )
            if sum(info.file_size for info in infos) > max_bytes:
                raise ToolError("Archive is larger than the extraction limit.")

            written = 0
            extracted = 0

            for info in infos:
                target = (target_root / info.filename).resolve(strict=False)
                try:
                    target.relative_to(target_root.resolve(strict=False))
                except ValueError as exc:
                    raise ToolError(
                        f"Archive entry escapes the extraction folder: {info.filename}"
                    ) from exc

                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ToolError("Extracted data exceeded the size limit.")
                        output.write(chunk)
                extracted += 1

    except ToolError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ToolError(f"Could not extract archive: {exc}") from exc

    return {
        "ok": True,
        "message": f"Extracted {extracted} file(s) to {target_root}.",
        "extracted": extracted,
        "path": str(target_root),
    }


def search_system(
    root: str = ".",
    *,
    name_contains: str | None = None,
    extension: str | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    include_hidden: bool = False,
    all_drives: bool = False,
    recursive: bool = True,
    sort_by: str = "modified_desc",
    max_results: int = 50,
    max_scanned: int = 20_000,
    max_seconds: float = 30.0,
) -> dict[str, Any]:
    """Search files by name, extension, size, and modification time.

    Results default to newest-first so questions like "the last 5 files" can be
    answered by requesting ``max_results=5`` with ``sort_by="modified_desc"``.
    """
    max_results = max(1, min(int(max_results), _MAX_SEARCH_RESULTS))
    max_scanned = max(1, min(int(max_scanned), _MAX_SCANNED_FILES))
    max_seconds = max(0.1, min(float(max_seconds), _MAX_SEARCH_SECONDS))

    sort_key = (sort_by or "modified_desc").strip().lower()
    if sort_key not in {
        "modified_desc",
        "modified_asc",
        "name",
        "size_desc",
        "size_asc",
        "none",
    }:
        sort_key = "modified_desc"

    needle = (name_contains or "").strip().casefold()
    use_glob = "*" in needle or "?" in needle

    ext = (extension or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    after_text = (modified_after or "").strip()
    before_text = (modified_before or "").strip()
    after_ts = _parse_datetime(after_text) if after_text else None
    before_ts = _parse_datetime(before_text) if before_text else None

    def optional_size(value: int | str | None, label: str) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{label} must be an integer.") from exc
        if parsed < 0:
            raise ToolError(f"{label} must not be negative.")
        return parsed

    min_size_value = optional_size(min_size, "min_size")
    max_size_value = optional_size(max_size, "max_size")

    if all_drives:
        roots: list[Path] = []
        requested = _absolute_path(root) if str(root).strip() else None
        if requested is not None and requested.is_dir():
            roots.append(requested)
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists() and drive not in roots:
                roots.append(drive)
    else:
        requested = _absolute_path(root)
        if not requested.is_dir():
            raise ToolError(f"Search root is not a folder: {requested}")
        roots = [requested]

    started = time.monotonic()
    scanned = 0
    matches: list[dict[str, Any]] = []
    truncated = False
    limited = False

    for search_root in roots:
        if truncated:
            break

        for dirpath, dirnames, filenames in os.walk(search_root, followlinks=False):
            if time.monotonic() - started >= max_seconds:
                truncated = True
                break

            if not recursive:
                dirnames[:] = []
            else:
                kept_dirs: list[str] = []
                for name in dirnames:
                    if name in _SKIP_DIR_NAMES:
                        continue
                    child = Path(dirpath) / name
                    try:
                        is_junction = getattr(child, "is_junction", lambda: False)()
                        if child.is_symlink() or is_junction:
                            continue
                        if not include_hidden and _is_hidden(child, child.stat()):
                            continue
                    except OSError:
                        continue
                    kept_dirs.append(name)
                dirnames[:] = kept_dirs

            for name in filenames:
                if time.monotonic() - started >= max_seconds:
                    truncated = True
                    break

                scanned += 1
                if scanned > max_scanned:
                    truncated = True
                    break

                path = Path(dirpath) / name
                try:
                    stats = path.stat()
                except OSError:
                    continue

                if not include_hidden and _is_hidden(path, stats):
                    continue
                if needle:
                    if use_glob:
                        if not fnmatch.fnmatch(name.casefold(), needle):
                            continue
                    elif needle not in name.casefold():
                        continue
                if ext and path.suffix.lower() != ext:
                    continue
                if min_size_value is not None and stats.st_size < min_size_value:
                    continue
                if max_size_value is not None and stats.st_size > max_size_value:
                    continue
                if after_ts is not None and stats.st_mtime < after_ts:
                    continue
                if before_ts is not None and stats.st_mtime > before_ts:
                    continue

                matches.append(
                    {
                        "path": str(path),
                        "name": name,
                        "extension": path.suffix.lower(),
                        "size": stats.st_size,
                        "modified": _now_iso(stats.st_mtime),
                        "_mtime": stats.st_mtime,
                    }
                )

                # Without sorting we can stop as soon as the result cap is met.
                if sort_key == "none" and len(matches) >= max_results:
                    limited = True
                    break

            if truncated or (sort_key == "none" and limited):
                break

    if sort_key == "modified_desc":
        matches.sort(key=lambda item: item["_mtime"], reverse=True)
    elif sort_key == "modified_asc":
        matches.sort(key=lambda item: item["_mtime"])
    elif sort_key == "name":
        matches.sort(key=lambda item: item["name"].casefold())
    elif sort_key == "size_desc":
        matches.sort(key=lambda item: item["size"], reverse=True)
    elif sort_key == "size_asc":
        matches.sort(key=lambda item: item["size"])

    if len(matches) > max_results:
        limited = True
    results = matches[:max_results]
    for item in results:
        item.pop("_mtime", None)

    if not results:
        if truncated:
            message = (
                "No matching files were found before the search caps were hit. "
                "Narrow the root or remove filters and try again."
            )
        else:
            message = "No matching files were found."
    else:
        message = f"Found {len(results)} file(s)."
        if limited:
            message += (
                f" Showing {len(results)} of {len(matches)} matches"
                f" ({sort_key.replace('_', ' ')})."
            )
        if truncated:
            message += " Search caps were reached."

    return {
        "ok": True,
        "message": message,
        "results": results,
        "roots": [str(root) for root in roots],
        "scanned": scanned,
        "truncated": truncated,
        "limited": limited,
        "sort_by": sort_key,
        "recursive": recursive,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
