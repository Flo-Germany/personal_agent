"""Ollama-backed agent with HITL tool approvals and streaming events."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, NamedTuple

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
)

from agent.approvals import (
    ApprovalError,
    ApprovalNotFound,
    PendingAction,
    PendingActionStore,
)
from agent.language import detect_requested_language
from agent.learning import (
    extract_facts,
    infer_deletions,
    should_apply_deletion,
)
from agent.ollama_client import NativeOllamaClient
from agent.prompt import build_system_prompt, format_clock_line
from agent.self_improvement import SelfImprovementService
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings
from tools.executor import ToolExecutor
from tools.schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

CONTEXT_WINDOW_MESSAGES = 10
# Approximate character budget for history messages. System prompt and tool
# schemas already consume part of the context, so this leaves room for the
# answer. Long tool loops keep the current request plus the most recent steps.
CONTEXT_WINDOW_CHARS = 40000
# Largest file body sent to the model. The UI still receives the full result.
_MODEL_READ_CHARS = 12000
# Wait for a quiet moment before extracting facts, so the learning call does
# not compete with a rapid follow-up message.
LEARNING_IDLE_SECONDS = 3.0
# Reasoning text shown in a collapsible UI block and kept with the message.
# Capped so an extreme thinking phase cannot bloat memory or an SSE frame.
_THINKING_STORE_LIMIT = 20000

_PLAN_STEP = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
_ACTION_PROMISE = re.compile(
    r"\b(?:i will|i'll|i am going to|i'm going to|next,?\s+i|"
    r"let me(?!\s+(?:know|also know)\b)|i can now|i will now)\b",
    re.IGNORECASE,
)
_PERSONAL_CUE = re.compile(
    r"\b(?:i|i'm|i am|i've|i have|my|me|mine|we|our|us|"
    r"ich|bin|mein|meine|meinen|mir|mich|wir|unser|uns|"
    r"remember|merke|notiere|forget|vergiss|"
    r"name is|age|live|lives|work|works|study|studies|"
    r"prefer|prefers|like|likes|love|loves|hate|hates|"
    r"favorite|favourite|birthday|geburtstag|heißt|wohnt|"
    r"arbeitet|studiert|mag|liebt|hasst)\b",
    re.IGNORECASE,
)
_SEQUENCE_SPLIT = re.compile(
    r"(?:\n+|;|\bthen\b|\bafter\s+that\b|\bafterwards\b|\bfinally\b|"
    r"\band\s+then\b|\bdann\b|\bdanach\b|\banschließend\b|"
    r"\bschließlich\b)",
    re.IGNORECASE,
)
_LEADING_CONNECTOR = re.compile(
    r"^(?:(?:first|then|next|finally|after\s+that|also|please|"
    r"zuerst|dann|danach|anschließend|schließlich|außerdem|bitte|und|and)"
    r"\b[:,]?\s*)+",
    re.IGNORECASE,
)
_ACTION_VERBS = (
    "create|make|write|add|delete|remove|move|copy|rename|open|start|launch|"
    "run|execute|search|find|read|show|list|zip|unzip|extract|download|"
    "upload|edit|update|set|change|install|stop|kill|save|put|place|close|"
    "empty|organize|sort|backup|restore|send|print|check|verify|compare|"
    "count|calculate|convert|compress|replace|insert|append|merge|split|"
    "share|email|reply|format|analyze|summarise|summarize|explain|describe|"
    "clean|erstelle|erstellen|erzeuge|erzeugen|mache|machen|schreibe|"
    "schreiben|speichere|speichern|lege|legen|füge|fügen|lösche|löschen|"
    "entferne|entfernen|verschiebe|verschieben|kopiere|kopieren|benenne|"
    "benennen|öffne|öffnen|starte|starten|führe|führen|suche|suchen|finde|"
    "finden|lese|lesen|zeige|zeigen|liste|listen|entpacke|entpacken|"
    "extrahiere|extrahieren|lade|laden|aktualisiere|aktualisieren|ändere|"
    "ändern|setze|setzen|installiere|installieren|beende|beenden|stoppe|"
    "stoppen|töte|töten|schließe|schließen|leere|leeren|räume|räumen|"
    "sortiere|sortieren|sichere|sichern|sende|senden|drucke|drucken|prüfe|"
    "prüfen|überprüfe|überprüfen|vergleiche|vergleichen|zähle|zählen|"
    "berechne|berechnen|konvertiere|konvertieren|komprimiere|komprimieren|"
    "ersetze|ersetzen|ergänze|ergänzen|teile|teilen|formatiere|formatieren|"
    "analysiere|analysieren|erkläre|erklären|beschreibe|beschreiben|"
    "bereinige|bereinigen"
)
_ACTION_START = re.compile(rf"^(?:{_ACTION_VERBS})\b", re.IGNORECASE)
_COMMA_ACTION_SPLIT = re.compile(
    rf",\s+(?=(?:{_ACTION_VERBS})\b)",
    re.IGNORECASE,
)
_META_STEP = re.compile(
    r"^(?:perform|do both|do all|without asking|do not ask|please|"
    r"make sure|let me know|mache beides|ohne zu fragen|frage nicht|"
    r"bitte)\b",
    re.IGNORECASE,
)

# Tool schemas are ~6k tokens, which directly slows generation on a local
# model. Base + GUI tools are always sent (one stable prompt prefix for chat and
# GUI work); the rest are added only when the request mentions them. Set
# agent.dynamic_tools to false to always send every schema.
_BASE_TOOL_NAMES = {
    "propose_plan",
    "run_powershell_query",
    "run_powershell_action",
    "start_program",
    "open_file",
    "read_file_content",
    "search_system",
}
_GUI_TOOL_NAMES = {
    "get_open_windows",
    "read_window_content",
    "get_desktop_context",
    "analyze_screen_ui",
    "analyze_screen",
    "move_cursor_to",
    "click_element",
    "keyboard_type_text",
    "keyboard_press_keys",
}
_FILE_TOOL_NAMES = {
    "create_folder",
    "create_file",
    "copy_file",
    "move_file_or_folder",
    "delete_file_or_folder",
    "unzip_file",
}
_FILE_HINT = re.compile(
    r"\b(?:file|files|folder|folders|directory|subfolder|desktop|downloads|"
    r"documents|pdf|docx?|txt|csv|xlsx?|zip|unzip|extract|copy|move|rename|"
    r"delete|remove|create|mkdir|backup|organize|sort|note|notes|memo|"
    r"report|write|script|json|yaml|config|log|ordner|datei|"
    r"dateien|verzeichnis|kopiere|kopieren|verschiebe|verschieben|umbenennen|"
    r"lösche|löschen|erstelle|erstellen|entpacke|entpacken|sichere|speichere|"
    r"speichern|notiz|notizen|bericht|schreibe|schreiben|"
    r"sortiere|organisiere)\b",
    re.IGNORECASE,
)
_VISION_HINT = re.compile(
    r"\b(?:image|images|photo|photos|picture|pictures|screenshot|wallpaper|"
    r"jpg|jpeg|png|bild|bilder|foto|fotos)\b",
    re.IGNORECASE,
)
_DIAGNOSIS_HINT = re.compile(
    r"\b(?:diagnos\w*|health|benchmark|hardware|temperature|thermal|"
    r"smart\w*|overheat\w*|slow|performance|uptime|event log|"
    r"gesundheit|leistung|langsam|temperatur|diagnose)\b",
    re.IGNORECASE,
)
# Requests that benefit from a hidden reasoning phase. Kept deliberately broad
# so that "auto" thinking never sacrifices quality to save latency.
_THINKING_HINT = re.compile(
    r"\b(?:why|how|explain|compare|difference|plan|steps?|debug|analyz\w*|"
    r"analys\w*|design|optimi\w*|trade-?offs?|prove|derive|architect\w*|"
    r"refactor|strategy|estimate|calculate|solve|reason\w*|root cause|"
    r"warum|wieso|erklär\w*|vergleich\w*|unterschied\w*|plan\w*|"
    r"schritt\w*|analysier\w*|entwirf\w*|optimier\w*|beweis\w*|"
    r"berechne\w*|rechne\w*|strategie|löse|ursache)\b",
    re.IGNORECASE,
)
# "auto" must not spend thinking tokens on greetings and acknowledgements.
_SMALL_TALK = re.compile(
    r"^\s*(?:hi|hey|hello|hallo|moin|servus|guten (?:morgen|tag|abend)|"
    r"good (?:morning|evening)|how are you|wie geht(?:'s| es)?(?: dir)?|"
    r"danke|thanks|thank you|ok(?:ay)?|yes|no|ja|nein|bitte|please)\b"
    r"[!.,?\s]*$",
    re.IGNORECASE,
)

# ---- search intent -------------------------------------------------------
# The user's wording decides whether a search is about folders, files, or both,
# even when a small model omits or mislabels the `kind` argument.
_FOLDER_WORD = re.compile(
    r"\b(?:ordner(?:n|s)?|verzeichnis(?:se|sen)?|folder(?:s)?|"
    r"director(?:y|ies)|dirs?)\b",
    re.IGNORECASE,
)
_FILE_WORD = re.compile(
    r"\b(?:datei(?:en)?|files?|dokument(?:e)?|documents?)\b",
    re.IGNORECASE,
)
_EXTENSION_WORD = re.compile(
    r"\b\*?\.(?:pdf|docx?|xlsx?|pptx?|txt|csv|zip|rar|7z|png|jpe?g|gif|"
    r"mp[34]|avi|mkv|md|json|ya?ml|log|exe|lnk|url)\b",
    re.IGNORECASE,
)
_LOCATION_BEFORE_FOLDER = re.compile(
    r"\b(?:in|im|inside|within|unter|from|aus)\b[^.!?\n]{0,30}$",
    re.IGNORECASE,
)
_LOCATION_AFTER_FOLDER = re.compile(
    r"^\s+(?:auf|im|in|on|under|unter)\b",
    re.IGNORECASE,
)
_NAMED_AFTER_FOLDER = re.compile(
    r"^\s+(?:named|called|namens|mit\s+dem\s+namen|mit\s+namen)\b",
    re.IGNORECASE,
)
_FOLDER_QUANTIFIER = re.compile(
    r"\b(?:alle|allen|aller|all|which|welche|welchen|welcher|meine|meinen|"
    r"meiner|my|beide|both|many|viele|list|liste|show|zeige|gib)"
    r"\s+(?:[\w-]+\s+){0,2}$",
    re.IGNORECASE,
)
# Absolute paths and file URLs must not influence intent: a folder request may
# legitimately mention "C:\Users\Me\Dokumente" or an output file "found.txt".
_PATH_TOKEN = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|file:///)[^\s'\"<>|]+",
    re.IGNORECASE,
)
# "on the desktop" / "auf dem Desktop" names the top level, not everything
# nested inside every folder there.
_TOP_LEVEL_PHRASE = re.compile(
    r"\b(?:on|auf)\s+(?:my|the|dem|meinem|meiner)?\s*desktop\b",
    re.IGNORECASE,
)
_RECURSIVE_PHRASE = re.compile(
    r"\b(?:inside|within|recursive|recursively|anywhere|everywhere|"
    r"unterordner|subfolder|subfolders|rekursiv|irgendwo|überall)\b"
    r"|\bin\s+allen\b|\bin\s+all\b",
    re.IGNORECASE,
)


def _search_scope_top_level(message: str) -> bool:
    """True when the wording clearly means only the top level of a folder."""
    text = _PATH_TOKEN.sub(" ", message)
    return bool(_TOP_LEVEL_PHRASE.search(text)) and not _RECURSIVE_PHRASE.search(text)


def _folder_target_count(message: str) -> int:
    """Count folder words that name the target, not just the location."""
    count = 0
    file_targets_present = bool(_FILE_WORD.search(message))
    for match in _FOLDER_WORD.finditer(message):
        before = message[max(0, match.start() - 40) : match.start()]
        after = message[match.end() : match.end() + 20]
        word = match.group(0).casefold()
        plural = word in {"folders", "directories", "dirs"} or word in {
            "ordnern",
            "verzeichnisse",
            "verzeichnissen",
        }
        quantified = bool(_FOLDER_QUANTIFIER.search(before))
        describes_place = bool(_LOCATION_AFTER_FOLDER.match(after))
        names_a_target = bool(_NAMED_AFTER_FOLDER.match(after))

        if not plural and not quantified and file_targets_present:
            # "files in the Downloads folder named report": the folder is the
            # place to look, the files are the target.
            continue
        if plural or quantified or describes_place or names_a_target:
            count += 1
            continue
        if _LOCATION_BEFORE_FOLDER.search(before):
            # "in the Downloads folder" describes where to look, not what to find.
            continue
    return count


def _search_kind_hint(message: str) -> str | None:
    """Infer files/folders/any from how the user phrased the request.

    Returns None when the wording does not clearly name a result type, so the
    model's own choice (or the tool default) is left alone. Absolute paths are
    removed first: a folder request may mention a path like
    ``C:\\Users\\Flo\\Dokumente`` or an output file ``found.txt`` without
    meaning "files".
    """
    text = _PATH_TOKEN.sub(" ", message).strip()
    if not text:
        return None
    folder_targets = _folder_target_count(text)
    file_targets = len(_FILE_WORD.findall(text))
    if folder_targets and file_targets:
        return "any"
    if folder_targets:
        return "folders"
    if file_targets:
        return "files"
    if _EXTENSION_WORD.search(text):
        return "files"
    return None


def _profile_tool_names(message: str) -> set[str]:
    """Pick the tool schemas a user request plausibly needs.

    Base + GUI tools are always included so that chat and GUI work share one
    stable prompt prefix: switching the tool list forces Ollama to re-evaluate
    the whole prompt (several seconds), which would erase the gain from the
    shorter schema list. File, image-search, and diagnosis tools are added only
    when the request mentions them.
    """
    text = str(message or "")
    names = set(_BASE_TOOL_NAMES) | set(_GUI_TOOL_NAMES)
    if _FILE_HINT.search(text):
        names |= _FILE_TOOL_NAMES
    if _VISION_HINT.search(text):
        names.add("semantic_image_search")
    if _DIAGNOSIS_HINT.search(text):
        names.add("system_diagnosis")
    return names


def _compact_element(element: Any) -> dict[str, Any] | None:
    if not isinstance(element, dict):
        return None
    compact: dict[str, Any] = {
        "id": element.get("id"),
        "name": element.get("name"),
        "type": element.get("type") or element.get("control_type"),
    }
    center = element.get("center")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        compact["center"] = [int(center[0]), int(center[1])]
    if element.get("enabled") is False:
        compact["disabled"] = True
    return compact


def _compact_context(context: Any) -> dict[str, Any] | None:
    """Keep only the live-context fields the planner actually needs."""
    if not isinstance(context, dict):
        return None
    active = context.get("active") or {}
    focused = context.get("focused") or {}
    compact: dict[str, Any] = {}
    if active:
        compact["active"] = {
            "title": active.get("title"),
            "process_name": active.get("process_name"),
        }
    if focused:
        compact["focused"] = {
            "name": focused.get("name"),
            "control_type": focused.get("control_type"),
        }
    dialogs = [
        item.get("title")
        for item in context.get("dialogs") or []
        if isinstance(item, dict) and item.get("title")
    ]
    if dialogs:
        compact["dialogs"] = dialogs
    return compact or None


def _compact_som_result(result: dict[str, Any]) -> dict[str, Any]:
    """Drop redundant SoM fields before the result is stored as model history.

    The UI receives the full result; the model only needs IDs, names, centers,
    and a short context. ``coordinate_map`` duplicated every element, and the
    full context/window list added thousands of tokens to every later turn.
    """
    compact: dict[str, Any] = {
        "ok": True,
        "snapshot_id": result.get("snapshot_id"),
        "window_title": result.get("window_title"),
        "display": result.get("display"),
        "process_name": result.get("process_name"),
        "window_foreground": result.get("window_foreground"),
        "detection": result.get("detection"),
        "uia_verified": result.get("uia_verified"),
        "app": result.get("app"),
        "app_windows": result.get("app_windows"),
        "conversation": result.get("conversation"),
        "conversation_warning": result.get("conversation_warning"),
        "preferred_window_title": result.get("preferred_window_title"),
        "recent_actions": result.get("recent_actions"),
        "grounding": result.get("grounding"),
        "suggested_id": result.get("suggested_id"),
        "suggested_element": _compact_element(result.get("suggested_element")),
        "confidence": result.get("confidence"),
        "message": result.get("message"),
        "next_step": result.get("next_step"),
        "elements": [
            item
            for item in (
                _compact_element(element)
                for element in result.get("elements") or []
            )
            if item is not None
        ],
    }
    context = _compact_context(result.get("context"))
    if context:
        compact["context"] = context
    for key in ("reused", "grounding_error", "element_missing", "context_changed"):
        if result.get(key):
            compact[key] = result[key]
    return compact


def _compact_desktop_context(result: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"ok": True}
    for key in ("app", "app_windows", "preferred_window_title", "recent_actions"):
        if result.get(key):
            compact[key] = result[key]
    active = result.get("active") or {}
    if isinstance(active, dict) and active:
        compact["active"] = {
            "title": active.get("title"),
            "process_name": active.get("process_name"),
            "rect": active.get("rect"),
            "minimized": active.get("minimized"),
        }
    focused = result.get("focused") or {}
    if isinstance(focused, dict) and focused:
        compact["focused"] = {
            "name": focused.get("name"),
            "control_type": focused.get("control_type"),
            "text_capable": focused.get("text_capable"),
        }
    windows = []
    for window in result.get("windows") or []:
        if not isinstance(window, dict):
            continue
        windows.append(
            {
                "title": window.get("title"),
                "process_name": window.get("process_name"),
                "rect": window.get("rect"),
                "minimized": window.get("minimized"),
                "active": window.get("active"),
            }
        )
    compact["windows"] = windows
    dialogs = [
        item.get("title")
        for item in result.get("dialogs") or []
        if isinstance(item, dict) and item.get("title")
    ]
    if dialogs:
        compact["dialogs"] = dialogs
    return compact


def _model_facing_tool_result(name: str, result: dict[str, Any]) -> str:
    """Serialize a tool result for conversation history (not for the UI)."""
    try:
        if name == "analyze_screen_ui" and result.get("ok"):
            projected: Any = _compact_som_result(result)
        elif name == "get_desktop_context" and result.get("ok"):
            projected = _compact_desktop_context(result)
        elif (
            name == "read_file_content"
            and result.get("ok")
            and isinstance(result.get("content"), str)
        ):
            projected = _compact_read_file(result)
        elif name == "search_system" and result.get("ok"):
            projected = _compact_search_result(result)
        else:
            projected = result
        return json.dumps(projected, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - never lose a result over projection
        logger.debug("Could not compact tool result %s", name, exc_info=True)
        return json.dumps(result, ensure_ascii=False, default=str)


def _compact_read_file(result: dict[str, Any]) -> dict[str, Any]:
    """Cap a file body before it enters the model context.

    A 55 KB README is roughly 14k tokens. Combined with the system prompt and
    tool schemas it overflows the context window; Ollama then drops the oldest
    tokens, which can remove the answer the model just read, and the agent
    retries the same read forever. The UI still shows the full result.
    """
    projection = dict(result)
    content = projection.get("content")
    if isinstance(content, str) and len(content) > _MODEL_READ_CHARS:
        projection["content"] = (
            content[:_MODEL_READ_CHARS]
            + f"\n\n[Truncated: showing the first {_MODEL_READ_CHARS} of "
            f"{len(content)} characters. Use run_powershell_query with "
            "Get-Content/Select-String to inspect a specific part.]"
        )
        projection["truncated"] = True
    return projection


def _compact_search_result(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the UI-only tree from a search result before it reaches the model.

    The flat ``results`` list already carries every path, type, and timestamp.
    Sending the tree as well would duplicate the same paths (once per ancestor)
    and waste context on nesting the model cannot use.
    """
    return {key: value for key, value in result.items() if key != "tree"}


_FILE_URL_WITHOUT_COLON = re.compile(r"file:///([A-Za-z])/(?=[^\s)>\]]*)")
_SCREEN_REQUEST = re.compile(
    r"(?:\bwhat(?:'s| is)? on (?:my|the) screen\b|"
    r"\blook at (?:my|the) screen\b|"
    r"\banaly[sz]e (?:my|the) screen\b|"
    r"\bwhat (?:do|can) you see (?:on|in) (?:my|the) screen\b|"
    r"\bwhat am i looking at\b|"
    r"\bwhat(?:'s| is) visible\b)",
    re.IGNORECASE,
)
_SCREEN_NUDGE = (
    "The user asked about the screen. Call the analyze_screen tool now with "
    "their question. Do not reply that you cannot see the screen; the tool is "
    "how you see it. Answer from the tool result."
)
_ACTION_FAILURE_NUDGE = (
    "The last run_powershell_action failed. Read its stderr, exit_code, and hint. "
    "Verify the current system state with run_powershell_query and choose a "
    "different approach if appropriate: re-resolve the exact PID or process name, "
    "try Stop-Process -Force, taskkill /PID <pid> /T /F, CloseMainWindow(), or "
    "the exact process name. A window title is not a process name; query "
    "MainWindowTitle or Get-CimInstance Win32_Process. Apps such as Chrome may "
    "have several processes. If Windows denied access, explain that the process "
    "is elevated or protected and that administrator rights are required. Never "
    "claim the change succeeded. If another change is needed, call "
    "run_powershell_action; the user will be asked to approve it."
)

_PATH_ARGUMENT_KEYS = (
    "path",
    "root",
    "source_path",
    "destination_path",
    "archive_path",
    "extract_to",
)

_PLACEHOLDER_HOME = re.compile(
    r"(?i)^([a-z]:[\\/]+users[\\/]+)"
    r"(?:your[-_ ]?(?:user[-_ ]?)?name|your[-_ ]?user|username|<username>|user)"
    r"(?=[\\/]|$)"
)


def _fix_placeholder_path(value: str) -> str:
    """Replace invented home folders like ``C:\\Users\\YourName`` with the real home."""
    match = _PLACEHOLDER_HOME.match(value)
    if match is None:
        return value

    remainder = value[match.end() :].lstrip("\\/")
    home = Path.home()
    return str(home / remainder) if remainder else str(home)


class LLMError(RuntimeError):
    """A user-facing LLM failure with an HTTP status code."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class ConfirmationRequired(RuntimeError):
    """Raised by the legacy non-streaming path when approval is needed."""

    def __init__(self, pending: PendingAction) -> None:
        super().__init__("User approval is required.")
        self.pending = pending


def _parse_arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("function", {}).get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _short_path(value: Any) -> str:
    """Show only the final path component in transient status messages."""
    text = str(value or "").strip()
    if not text:
        return ""

    try:
        name = Path(text).name
    except (OSError, ValueError):
        name = ""

    return name or text


def _short_command(value: Any, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _program_was_named_by_user(user_message: str, program: str) -> bool:
    """Whether the latest user message actually names the chosen program."""
    message = str(user_message or "").casefold()
    program_text = str(program or "").strip().casefold()
    if not message or not program_text:
        return False

    candidates: set[str] = {program_text}

    try:
        path = Path(program_text)
    except (OSError, ValueError):
        path = None

    if path is not None:
        candidates.add(path.name.casefold())
        candidates.add(path.stem.casefold())

    stem = path.stem.casefold() if path is not None else program_text
    aliases = {
        "winword": ("word", "microsoft word"),
        "excel": ("excel", "microsoft excel"),
        "powerpnt": ("powerpoint", "microsoft powerpoint"),
        "outlook": ("outlook", "microsoft outlook"),
        "msedge": ("edge", "microsoft edge"),
        "chrome": ("chrome", "google chrome"),
        "code": ("vs code", "visual studio code"),
        "explorer": ("file explorer",),
        "mspaint": ("paint",),
        "calc": ("calculator",),
    }
    candidates.update(aliases.get(stem, ()))

    for candidate in candidates:
        if candidate and re.search(
            rf"(?<!\w){re.escape(candidate)}(?!\w)",
            message,
        ):
            return True

    return False


def _tool_status_message(name: str, arguments: dict[str, Any]) -> str:
    if name == "propose_plan":
        steps = arguments.get("steps")
        count = len(steps) if isinstance(steps, list) else 0
        if count:
            return f"Planning {count} step{'s' if count != 1 else ''}..."
        return "Planning the steps..."
    if name == "analyze_screen_ui":
        return "Mapping interactive UI elements..."
    if name == "move_cursor_to":
        return "Requesting cursor control..."
    if name == "click_element":
        element = str(arguments.get("element_name") or "element")
        return f"Requesting cursor control to click {element}..."
    if name == "keyboard_type_text":
        return "Requesting keyboard control to type..."
    if name == "keyboard_press_keys":
        keys = arguments.get("keys")
        joined = "+".join(str(key) for key in keys) if isinstance(keys, list) else ""
        return f"Pressing {joined}..." if joined else "Pressing keys..."
    if name == "run_powershell_query":
        return (
            "Running PowerShell query: "
            f"{_short_command(arguments.get('command')) or '...'}"
        )
    if name == "run_powershell_action":
        return (
            "Running PowerShell action: "
            f"{_short_command(arguments.get('command')) or '...'}"
        )
    if name == "start_program":
        return f"Opening {arguments.get('program_name', 'an application')}..."
    if name == "open_file":
        return (
            f"Opening {_short_path(arguments.get('path'))} with "
            f"{arguments.get('program', 'a program')}..."
        )
    if name == "create_folder":
        return f"Creating folder {_short_path(arguments.get('path'))}..."
    if name == "create_file":
        return f"Creating file {_short_path(arguments.get('path'))}..."
    if name == "copy_file":
        return (
            f"Copying {_short_path(arguments.get('source_path'))} to "
            f"{_short_path(arguments.get('destination_path'))}..."
        )
    if name == "move_file_or_folder":
        return (
            f"Moving {_short_path(arguments.get('source_path'))} to "
            f"{_short_path(arguments.get('destination_path'))}..."
        )
    if name == "delete_file_or_folder":
        return (
            f"Moving {_short_path(arguments.get('path'))} to the Recycle Bin..."
        )
    if name == "unzip_file":
        return f"Extracting {_short_path(arguments.get('archive_path'))}..."
    if name == "read_file_content":
        return f"Reading {_short_path(arguments.get('path'))}..."
    if name == "search_system":
        return (
            f"Searching {_short_path(arguments.get('root')) or 'the file system'}..."
        )
    if name == "semantic_image_search":
        return (
            "Searching images for "
            f'"{arguments.get("query", "a visual description")}"...'
        )
    return f"Running {name}..."


def _action_description(name: str, arguments: dict[str, Any]) -> str:
    if name == "run_powershell_action":
        explanation = str(arguments.get("description", "")).strip()
        command = _short_command(arguments.get("command"), 500)
        if explanation:
            return f"PowerShell action: {explanation}\nCommand: {command}"
        return f"PowerShell action:\nCommand: {command}"
    if name == "start_program":
        return f"Start program {arguments.get('program_name', 'an application')}"
    if name == "open_file":
        return (
            f"Open {arguments.get('path', '')} with "
            f"{arguments.get('program', 'a program')}"
        )
    if name == "click_element":
        element = str(arguments.get("element_name") or "the selected element")
        element_id = arguments.get("element_id", "?")
        return (
            f"Requesting cursor control to click #{element_id} '{element}'. "
            "Please take your hands off the mouse and keyboard."
        )
    if name == "move_cursor_to":
        element = str(arguments.get("element_name") or "the selected element")
        element_id = arguments.get("element_id", "?")
        return (
            f"Requesting cursor control to move to #{element_id} '{element}'. "
            "Please take your hands off the mouse and keyboard."
        )
    if name == "keyboard_type_text":
        text = str(arguments.get("text") or "")
        preview = " ".join(text.split())
        if len(preview) > 200:
            preview = preview[:197] + "..."
        return (
            "Requesting keyboard control to type into the focused field. "
            "Please take your hands off the mouse and keyboard.\n"
            f"Text: {preview}"
        )
    if name == "keyboard_press_keys":
        keys = arguments.get("keys")
        joined = "+".join(str(key) for key in keys) if isinstance(keys, list) else ""
        return (
            "Requesting keyboard control to press "
            f"{joined or 'the requested keys'}. "
            "Please take your hands off the mouse and keyboard."
        )
    if name == "create_folder":
        return f"Create folder {arguments.get('path', '')}"
    if name == "create_file":
        return f"Create file {arguments.get('path', '')}"
    if name == "copy_file":
        return (
            f"Copy {arguments.get('source_path', '')} to "
            f"{arguments.get('destination_path', '')}"
        )
    if name == "move_file_or_folder":
        return (
            f"Move {arguments.get('source_path', '')} to "
            f"{arguments.get('destination_path', '')}"
        )
    if name == "delete_file_or_folder":
        return f"Move to Recycle Bin: {arguments.get('path', '')}"
    if name == "unzip_file":
        return (
            f"Extract {arguments.get('archive_path', '')} to "
            f"{arguments.get('extract_to', '')}"
        )
    return f"{name}: {json.dumps(arguments, ensure_ascii=False)}"


class StreamOutcome(NamedTuple):
    """A completion plus the content already emitted to the UI as deltas."""

    completion: Any
    streamed_content: str | None
    # Reasoning models emit a hidden thinking phase before the answer. It is
    # shown in the UI but never sent back to the model.
    thinking: str | None = None
    thinking_ms: float | None = None
    thinking_tokens: int | None = None


class TurnCancelled(Exception):
    """The user interrupted the current agent turn."""


class ChatService:
    """Compose prompts, call Ollama, run tools with HITL, and stream events."""

    def __init__(
        self,
        settings: Settings,
        store: ConversationStore,
        knowledge: KnowledgeBase,
        tools: ToolExecutor | None = None,
        client: Any | None = None,
        pending_store: PendingActionStore | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._knowledge = knowledge
        self._tools = tools or ToolExecutor(
            settings.tools,
            automation=settings.automation,
        )
        self._improvement = SelfImprovementService(
            knowledge,
            enabled=settings.agent.self_improvement_enabled,
            min_tools_for_workflow=(
                settings.agent.self_improvement_workflow_min_tools
            ),
        )
        self._client = client or NativeOllamaClient(
            settings.ollama.base_url,
            timeout=settings.ollama.timeout_seconds,
            num_ctx=settings.ollama.context_window,
            keep_alive=settings.ollama.keep_alive_seconds,
            max_retries=settings.ollama.max_retries,
        )
        warning = settings.ollama.quantization_warning()
        if warning:
            logger.warning("%s", warning)
        self._pending = pending_store or PendingActionStore(
            settings.agent.approval_ttl_seconds
        )
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._turn_plans: dict[str, list[str]] = {}
        # UI-only search cards for the current turn. The model-facing tool result
        # deliberately drops the tree, so history keeps the rendered cards here.
        self._search_cards: dict[str, list[dict[str, Any]]] = {}
        self._search_cards_guard = threading.Lock()
        self._cancel_guard = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._interrupted: set[str] = set()
        self._active_guard = threading.Lock()
        self._active_conversations: set[str] = set()
        self._learning_guard = threading.Lock()
        self._learning_pending: dict[str, list[str]] = {}
        self._learning_timers: dict[str, threading.Timer] = {}
        self._learning_delay = LEARNING_IDLE_SECONDS

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def set_client(self, client: Any) -> None:
        """Hot-swap the inference transport after an engine switch."""
        self._client = client

    # ------------------------------------------------------------- interrupt
    def _cancel_event(self, conversation_id: str) -> threading.Event:
        with self._cancel_guard:
            return self._cancel_events.setdefault(
                conversation_id,
                threading.Event(),
            )

    def interrupt(self, conversation_id: str) -> bool:
        """Ask the running turn for this conversation to stop."""
        with self._cancel_guard:
            event = self._cancel_events.get(conversation_id)
            if event is None:
                return False
            event.set()
            self._interrupted.add(conversation_id)
        return True

    def _raise_if_cancelled(self, conversation_id: str) -> None:
        with self._cancel_guard:
            event = self._cancel_events.get(conversation_id)
            if event is not None and event.is_set():
                raise TurnCancelled()

    def _was_interrupted(self, conversation_id: str) -> bool:
        with self._cancel_guard:
            return conversation_id in self._interrupted

    def _clear_interrupt(self, conversation_id: str) -> None:
        with self._cancel_guard:
            self._interrupted.discard(conversation_id)
            event = self._cancel_events.get(conversation_id)
            if event is not None:
                event.clear()

    # ------------------------------------------------------------- learning
    def _begin_turn(self, conversation_id: str) -> None:
        with self._active_guard:
            self._active_conversations.add(conversation_id)

    def _end_turn(self, conversation_id: str) -> None:
        with self._active_guard:
            self._active_conversations.discard(conversation_id)

    def _is_turn_active(self, conversation_id: str) -> bool:
        with self._active_guard:
            return conversation_id in self._active_conversations

    def schedule_learning(self, conversation_id: str, user_message: str) -> None:
        """Queue fact extraction for a quiet moment after the turn.

        Coalescing rapid follow-ups avoids running an extra LLM request while
        the user is already asking the next question. All messages are kept and
        learned together once the conversation has been idle briefly.
        """
        if not self._settings.memory.learn_from_chats:
            return
        message = user_message.strip()
        if not message or not _PERSONAL_CUE.search(message):
            return

        with self._learning_guard:
            pending = self._learning_pending.setdefault(conversation_id, [])
            pending.append(message)
            if len(pending) > 6:
                del pending[:-6]

            timer = self._learning_timers.pop(conversation_id, None)
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(
                self._learning_delay,
                self._run_scheduled_learning,
                args=(conversation_id,),
            )
            timer.daemon = True
            self._learning_timers[conversation_id] = timer
            timer.start()

    def _run_scheduled_learning(self, conversation_id: str) -> None:
        with self._learning_guard:
            self._learning_timers.pop(conversation_id, None)
            pending = self._learning_pending.get(conversation_id, [])
            if not pending:
                return
            if self._is_turn_active(conversation_id):
                timer = threading.Timer(
                    self._learning_delay,
                    self._run_scheduled_learning,
                    args=(conversation_id,),
                )
                timer.daemon = True
                self._learning_timers[conversation_id] = timer
                timer.start()
                return
            messages = self._learning_pending.pop(conversation_id, [])

        if not messages:
            return
        combined = "\n\n".join(messages)[-8000:]
        self.learn_from_exchange(combined)

    def _store_partial_stream(
        self,
        conversation_id: str,
        content: str,
    ) -> None:
        """Keep the text the user already saw when a turn is interrupted."""
        text = content.strip()
        if not text:
            return
        if self._looks_like_raw_tool_call(text):
            return
        if self._looks_like_malformed_tool_output(text):
            return
        self._store.append(conversation_id, "assistant", text)

    def _finalize_cancelled_turn(self, conversation_id: str) -> None:
        """Keep completed work, drop an incomplete tool group, add a marker."""
        history = self._store.load(conversation_id)

        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.get("role") == "user":
                break
            if message.get("role") == "assistant" and message.get("tool_calls"):
                expected = len(message["tool_calls"])
                results = sum(
                    1
                    for item in history[index + 1 :]
                    if item.get("role") == "tool"
                )
                if results < expected:
                    self._store.truncate(conversation_id, index)
                break

        history = self._store.load(conversation_id)
        if (
            history
            and history[-1].get("role") == "assistant"
            and history[-1].get("content") == "Interrupted by user."
        ):
            self._turn_plans.pop(conversation_id, None)
            return

        self._store.append(conversation_id, "assistant", "Interrupted by user.")
        self._turn_plans.pop(conversation_id, None)

    # ------------------------------------------------------------------ HITL
    def get_pending(self, approval_id: str) -> PendingAction:
        return self._pending.get(approval_id)

    def get_pending_for_conversation(self, conversation_id: str) -> PendingAction | None:
        return self._pending.get_for_conversation(conversation_id)

    def discard_pending(self, conversation_id: str) -> bool:
        return self._pending.discard(conversation_id)

    # --------------------------------------------------------------- streaming
    def start_turn(self, conversation_id: str, message: str) -> Iterator[dict[str, Any]]:
        """Stream a user turn until final, error, or an approval pause."""
        lock = self._lock_for(conversation_id)
        with lock:
            existing = self._pending.get_for_conversation(conversation_id)
            if existing is not None:
                yield existing.to_public()
                return

            # A new user message is the only thing that clears a physical-input
            # automation halt. In-turn approval resumes must keep the halt.
            reset_automation = getattr(self._tools, "reset_automation", None)
            if callable(reset_automation):
                reset_automation()

            self._improvement.begin_turn(conversation_id)
            start_length = len(self._store.load(conversation_id))
            self._store.append(conversation_id, "user", message)
            self._turn_plans.pop(conversation_id, None)
            self.clear_search_cards(conversation_id)
            self._clear_interrupt(conversation_id)
            self._cancel_event(conversation_id)
            self._begin_turn(conversation_id)

            try:
                yield from self._stream_with_improvement(
                    conversation_id,
                    self._run_segment(conversation_id),
                )
            except TurnCancelled:
                self._finalize_cancelled_turn(conversation_id)
                yield {
                    "type": "cancelled",
                    "conversation_id": conversation_id,
                }
            except GeneratorExit:
                if self._was_interrupted(conversation_id):
                    self._finalize_cancelled_turn(conversation_id)
                elif self._pending.get_for_conversation(conversation_id) is None:
                    self._store.truncate(conversation_id, start_length)
                raise
            except Exception as exc:  # noqa: BLE001 - converted into an SSE error
                if self._pending.get_for_conversation(conversation_id) is None:
                    self._store.truncate(conversation_id, start_length)
                yield self._error_event(conversation_id, exc)
            finally:
                self._clear_interrupt(conversation_id)
                self._end_turn(conversation_id)

    def resume(
        self,
        approval_id: str,
        approved: bool,
    ) -> Iterator[dict[str, Any]]:
        """Execute or cancel a pending action, then continue the agent loop."""
        pending = self._pending.consume(approval_id)
        conversation_id = pending.conversation_id
        lock = self._lock_for(conversation_id)

        with lock:
            self._clear_interrupt(conversation_id)
            self._cancel_event(conversation_id)
            self._begin_turn(conversation_id)

            try:
                results: list[tuple[dict[str, Any], dict[str, Any]]] = []

                # Execute before yielding anything so an approved action is not
                # lost if the client disconnects immediately after Approve.
                for call in pending.calls:
                    self._raise_if_cancelled(conversation_id)
                    name = call["function"]["name"]
                    arguments = call["function"]["arguments"]
                    if approved:
                        result = self._execute_tool(
                            name,
                            arguments,
                            conversation_id,
                        )
                    else:
                        result = {
                            "ok": False,
                            "cancelled": True,
                            "error": "Action cancelled by user.",
                        }
                    self._store.append(
                        conversation_id,
                        "tool",
                        _model_facing_tool_result(name, result),
                        tool_call_id=call["id"],
                        name=name,
                    )
                    results.append((call, result))

                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "approval",
                    "message": (
                        "Approved. Executing the action(s)..."
                        if approved
                        else "Action cancelled by user."
                    ),
                }

                # The batch above runs before the first event is yielded so an
                # approved action is never lost if the client disconnects.
                # Emit the per-action start events afterwards, before the
                # results, so the UI can build a complete task list with the
                # exact labels instead of only a batch-level "Approved" row.
                if approved:
                    for call in pending.calls:
                        name = call["function"]["name"]
                        arguments = call["function"]["arguments"]
                        parsed_arguments = arguments
                        if isinstance(parsed_arguments, str):
                            try:
                                parsed_arguments = json.loads(parsed_arguments or "{}")
                            except json.JSONDecodeError:
                                parsed_arguments = {}
                        if not isinstance(parsed_arguments, dict):
                            parsed_arguments = {}
                        yield {
                            "type": "status",
                            "conversation_id": conversation_id,
                            "phase": "tool_started",
                            "tool": name,
                            "arguments": parsed_arguments,
                            "message": _tool_status_message(name, parsed_arguments),
                        }

                for call, result in results:
                    yield self._tool_result_event(
                        conversation_id,
                        call["function"]["name"],
                        result,
                    )

                failed_powershell_action = any(
                    call["function"]["name"] == "run_powershell_action"
                    and not result.get("ok")
                    and not result.get("cancelled")
                    for call, result in results
                )
                executed_signatures = (
                    {
                        self._call_signature(
                            call["function"]["name"],
                            call["function"]["arguments"],
                        )
                        for call in pending.calls
                    }
                    if approved
                    else set()
                )
                yield from self._stream_with_improvement(
                    conversation_id,
                    self._run_segment(
                        conversation_id,
                        recover_failed_action=failed_powershell_action,
                        auto_approve=approved,
                        executed_signatures=executed_signatures,
                    ),
                )
            except TurnCancelled:
                self._finalize_cancelled_turn(conversation_id)
                yield {
                    "type": "cancelled",
                    "conversation_id": conversation_id,
                }
            except GeneratorExit:
                if self._was_interrupted(conversation_id):
                    self._finalize_cancelled_turn(conversation_id)
                raise
            except Exception as exc:  # noqa: BLE001
                yield self._error_event(conversation_id, exc)
            finally:
                self._clear_interrupt(conversation_id)
                self._end_turn(conversation_id)

    # ------------------------------------------------------- display choice
    def resume_display(
        self,
        approval_id: str,
        display: int | None,
    ) -> Iterator[dict[str, Any]]:
        """Continue a paused screen analysis with the display the user chose.

        ``None`` cancels the choice; the model is told that no display was
        selected and can explain or ask again. Validation happens eagerly so
        an invalid number leaves the pending choice intact.
        """
        pending = self._pending.get(approval_id)
        if pending.kind != "display":
            raise ApprovalError(
                "This request is not waiting for a display choice."
            )

        if display is not None:
            try:
                chosen = int(display)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "The display must be a 1-based display number."
                ) from exc

            available = {
                int(item["index"])
                for item in pending.displays
                if isinstance(item, dict) and "index" in item
            }
            if chosen not in available:
                numbers = ", ".join(str(index) for index in sorted(available))
                raise ValueError(
                    f"Display {chosen} is not available. Choose one of: "
                    f"{numbers}."
                )
            display = chosen

        return self._resume_display_stream(approval_id, display)

    def _resume_display_stream(
        self,
        approval_id: str,
        display: int | None,
    ) -> Iterator[dict[str, Any]]:
        pending = self._pending.consume(approval_id)
        conversation_id = pending.conversation_id
        lock = self._lock_for(conversation_id)

        with lock:
            self._clear_interrupt(conversation_id)
            self._cancel_event(conversation_id)
            self._begin_turn(conversation_id)

            try:
                call = dict(pending.calls[0])
                function = dict(call.get("function") or {})
                arguments = dict(_parse_arguments(call))

                if display is not None:
                    arguments["display"] = display

                function["arguments"] = json.dumps(
                    arguments,
                    ensure_ascii=False,
                )
                call["function"] = function
                self._store.append(
                    conversation_id,
                    "assistant",
                    pending.assistant_message,
                    tool_calls=[call],
                )

                if display is None:
                    result: dict[str, Any] = {
                        "ok": False,
                        "cancelled": True,
                        "error": (
                            "The user cancelled the display choice, so the "
                            "screen was not captured. Do not guess a display."
                        ),
                    }
                else:
                    # Run before yielding so the chosen analysis is not lost
                    # if the client disconnects immediately after selecting.
                    tool_name = str(
                        call.get("function", {}).get("name")
                        or "analyze_screen"
                    )
                    result = self._execute_tool(
                        tool_name,
                        arguments,
                        conversation_id,
                    )

                self._store.append(
                    conversation_id,
                    "tool",
                    _model_facing_tool_result("analyze_screen", result),
                    tool_call_id=call["id"],
                    name="analyze_screen",
                )

                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "tool_started",
                    "tool": "analyze_screen",
                    "arguments": arguments,
                    "message": (
                        f"Analyzing display {display}..."
                        if display is not None
                        else "Screen analysis cancelled."
                    ),
                }
                yield self._tool_result_event(
                    conversation_id,
                    "analyze_screen",
                    result,
                )

                yield from self._stream_with_improvement(
                    conversation_id,
                    self._run_segment(
                        conversation_id,
                        screen_already_analyzed=True,
                    ),
                )
            except TurnCancelled:
                self._finalize_cancelled_turn(conversation_id)
                yield {
                    "type": "cancelled",
                    "conversation_id": conversation_id,
                }
            except GeneratorExit:
                if self._was_interrupted(conversation_id):
                    self._finalize_cancelled_turn(conversation_id)
                raise
            except Exception as exc:  # noqa: BLE001
                yield self._error_event(conversation_id, exc)
            finally:
                self._clear_interrupt(conversation_id)
                self._end_turn(conversation_id)

    def _tool_schemas(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the tool schemas relevant to the current request."""
        if not self._settings.agent.dynamic_tools:
            return TOOL_SCHEMAS

        message = ""
        for item in reversed(history):
            if item.get("role") == "user":
                message = str(item.get("content") or "")
                break

        names = _profile_tool_names(message)
        selected = [
            schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in names
        ]
        return selected or TOOL_SCHEMAS

    def _screen_context(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Return the chat's stable system/tool prefix for the vision request.

        Sending the exact same prefix lets Ollama reuse its prompt cache and
        keep the runner at the chat context size. When the model template needs
        the system prompt folded into the user turn (Hermes), there is no stable
        prefix to share, so the vision request stays minimal.
        """
        if self._system_prompt_in_user_message():
            return None
        if self._settings.vision.screen_model != self._settings.ollama.model:
            return None
        history = self._store.load(conversation_id)
        return {
            "system_prompt": self._system_prompt(history),
            "tools": self._tool_schemas(history),
        }

    def _stream_with_improvement(
        self,
        conversation_id: str,
        events: Iterator[dict[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        """Pass events through while recording lessons at the turn boundary."""
        for event in events:
            kind = event.get("type")
            if kind == "final":
                self._improvement.finish_turn(
                    conversation_id,
                    self._last_user_message(conversation_id),
                    str(event.get("reply") or ""),
                    success=True,
                )
            elif kind in {"error", "cancelled"}:
                self._improvement.finish_turn(
                    conversation_id,
                    self._last_user_message(conversation_id),
                    "",
                    success=False,
                )
            yield event

    @staticmethod
    def _repeated_read_result(name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "repeated": True,
            "tool": name,
            "error": (
                "This exact read-only call already ran twice with the same "
                "arguments. Do not call it again; answer with the evidence you "
                "already have."
            ),
        }

    @staticmethod
    def _note_read_call(
        state: dict[str, Any],
        signature: tuple[str, str],
    ) -> bool:
        """Track consecutive identical read-only calls.

        The third identical call in a row is treated as a loop. A legitimate
        read -> edit -> verify workflow is not affected because any other tool
        call resets the counter.
        """
        if state.get("signature") == signature:
            state["count"] = int(state.get("count", 0)) + 1
        else:
            state["signature"] = signature
            state["count"] = 1
        return int(state["count"]) >= 3

    def _summarize_without_tools(
        self,
        conversation_id: str,
        *,
        message: str,
        nudge_text: str,
    ) -> Iterator[dict[str, Any]]:
        """Force a final answer from the evidence already collected."""
        yield {
            "type": "status",
            "conversation_id": conversation_id,
            "phase": "thinking",
            "message": message,
        }
        try:
            outcome = yield from self._stream_completion(
                conversation_id,
                self._store.load(conversation_id),
                nudge=True,
                nudge_text=nudge_text,
                include_tools=False,
            )
        except LLMError as exc:
            logger.warning("Tool-free summary failed: %s", exc)
            raise LLMError(
                "The agent could not finish the request.",
                status_code=502,
            ) from exc

        choices = getattr(outcome.completion, "choices", None)
        summary = ""
        if choices:
            summary = getattr(choices[0].message, "content", "") or ""
        reply = self._normalize_reply(summary).strip()
        if not reply or self._looks_like_raw_tool_call(reply):
            raise LLMError(
                "The agent could not finish the request.",
                status_code=502,
            )
        thinking_fields = self._thinking_fields(
            [outcome.thinking] if outcome.thinking else [],
            outcome.thinking_ms,
            outcome.thinking_tokens,
        )
        self._store.append(
            conversation_id,
            "assistant",
            reply,
            **thinking_fields,
        )
        self._turn_plans.pop(conversation_id, None)
        yield {
            "type": "final",
            "conversation_id": conversation_id,
            "reply": reply,
            "streamed": (
                outcome.streamed_content is not None
                and self._normalize_reply(outcome.streamed_content).strip()
                == reply
            ),
            **thinking_fields,
        }

    def _execute_tool(
        self,
        name: str,
        arguments: str | dict[str, Any],
        conversation_id: str,
    ) -> dict[str, Any]:
        if name == "search_system":
            arguments = self._align_search_kind(conversation_id, arguments)
        if name != "analyze_screen":
            result = self._tools.execute(name, arguments)
        else:
            result = self._tools.execute(
                name,
                arguments,
                screen_context=self._screen_context(conversation_id),
            )

        if isinstance(arguments, str):
            try:
                parsed_arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                parsed_arguments = {}
        elif isinstance(arguments, dict):
            parsed_arguments = arguments
        else:
            parsed_arguments = {}

        self._improvement.record_tool_result(
            conversation_id,
            name,
            parsed_arguments if isinstance(parsed_arguments, dict) else {},
            result,
        )
        if name == "search_system":
            self._remember_search_card(conversation_id, result)
        if name in {"analyze_screen", "analyze_screen_ui"}:
            release = getattr(self._tools, "release_vision", None)
            if callable(release):
                try:
                    release()
                except Exception:  # noqa: BLE001 - never fail a tool result
                    logger.debug("Releasing the vision engine failed", exc_info=True)
        return result

    def _align_search_kind(
        self,
        conversation_id: str,
        arguments: str | dict[str, Any],
    ) -> str | dict[str, Any]:
        """Align search arguments with an unambiguous user wording.

        Models occasionally omit ``kind``, label a folder request as files, or
        recurse into the whole Desktop when the user asked for the folders *on*
        the Desktop. The user's own words are the ground truth, so those cases
        are corrected before the tool runs. Ambiguous wording is left alone.
        """
        message = self._last_user_message(conversation_id)
        hint = _search_kind_hint(message)
        top_level = _search_scope_top_level(message)
        if hint is None and not top_level:
            return arguments

        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return arguments
        else:
            parsed = arguments
        if not isinstance(parsed, dict):
            return arguments

        normalized = dict(parsed)
        changed = False

        if hint is not None:
            if hint == "folders" and (
                normalized.get("extension")
                or normalized.get("min_size")
                or normalized.get("max_size")
            ):
                # A byte or extension filter can only describe files; forcing
                # folders would make the tool reject the call.
                hint = "files"

            current = str(normalized.get("kind") or "").strip().lower()
            if current != hint:
                normalized["kind"] = hint
                changed = True
                logger.info(
                    "search_system kind %r adjusted to %r from the user's wording",
                    current or None,
                    hint,
                )

        if top_level and normalized.get("recursive") is not False:
            # "Ordner auf dem Desktop" means the icons, not every folder below.
            normalized["recursive"] = False
            normalized.pop("max_depth", None)
            changed = True
            logger.info("search_system limited to the top level by the user's wording")

        return normalized if changed else arguments

    def _remember_search_card(
        self,
        conversation_id: str,
        result: dict[str, Any],
    ) -> None:
        """Keep the rendered search tree for the history endpoint.

        Only the current turn is kept: the card is a view of the last search
        the user asked about, and the full path list already lives in the tool
        message for the model.
        """
        if not result.get("ok") or not result.get("results"):
            return
        card = {
            key: result.get(key)
            for key in (
                "results",
                "tree",
                "summary",
                "query",
                "roots",
                "kind",
                "truncated",
                "limited",
                "duration_seconds",
                "message",
            )
        }
        with self._search_cards_guard:
            cards = self._search_cards.setdefault(conversation_id, [])
            cards.append(card)
            del cards[:-3]

    def search_cards(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return the search cards of the current turn for /chat/history."""
        with self._search_cards_guard:
            return list(self._search_cards.get(conversation_id, []))

    def clear_search_cards(self, conversation_id: str) -> None:
        with self._search_cards_guard:
            self._search_cards.pop(conversation_id, None)

    def _record_plan(
        self,
        conversation_id: str,
        result: dict[str, Any],
    ) -> None:
        steps = result.get("steps")
        if not isinstance(steps, list):
            return
        cleaned = [" ".join(str(step).split()) for step in steps if str(step).strip()]
        if cleaned:
            self._turn_plans[conversation_id] = cleaned[:8]

    def _run_segment(
        self,
        conversation_id: str,
        *,
        recover_failed_action: bool = False,
        auto_approve: bool = False,
        executed_signatures: set[tuple[str, str]] | None = None,
        screen_already_analyzed: bool = False,
    ) -> Iterator[dict[str, Any]]:
        max_tool_rounds = self._settings.agent.max_iterations
        max_auto_approved = self._settings.agent.max_auto_approved_actions
        screen_forced = screen_already_analyzed
        recovery_attempted = False
        executed = set(executed_signatures or ())
        auto_approved_actions = 0
        round_index = 0
        force_summary = False
        read_repeat: dict[str, Any] = {"signature": None, "count": 0}
        # Reasoning across the whole turn, attached to the visible answer.
        turn_thinking: list[str] = []
        turn_thinking_ms = 0.0
        turn_thinking_tokens = 0

        def collect_thinking(result: StreamOutcome) -> dict[str, Any]:
            """Accumulate one round's reasoning and return the stored fields."""
            nonlocal turn_thinking_ms, turn_thinking_tokens
            if result.thinking:
                turn_thinking.append(result.thinking)
            if result.thinking_ms is not None:
                turn_thinking_ms += result.thinking_ms
            if result.thinking_tokens:
                turn_thinking_tokens += result.thinking_tokens
            return self._thinking_fields(
                turn_thinking,
                turn_thinking_ms or None,
                turn_thinking_tokens or None,
            )

        while True:
            self._raise_if_cancelled(conversation_id)
            if force_summary:
                yield from self._summarize_without_tools(
                    conversation_id,
                    message=(
                        "The same read was repeated; summarizing the evidence "
                        "collected so far..."
                    ),
                    nudge_text=(
                        "You repeated the same read-only tool call with "
                        "identical arguments. Do not call any more tools. "
                        "Answer using the results you already have, and state "
                        "clearly what could not be determined. Never claim an "
                        "action succeeded unless a tool result confirmed it."
                    ),
                )
                return
            yield {
                "type": "status",
                "conversation_id": conversation_id,
                "phase": "thinking",
                "message": "Thinking...",
            }

            completion = None
            message = None
            tool_calls: list[Any] = []
            content = ""
            streamed_content: str | None = None

            # Hermes occasionally emits an empty message on complex prompts.
            # Retrying once is safe because no tool has run yet.
            for attempt in range(2):
                outcome = yield from self._stream_completion(
                    conversation_id,
                    self._store.load(conversation_id),
                    nudge=attempt > 0,
                    tool_choice="required" if attempt > 0 else "auto",
                )
                completion = outcome.completion
                streamed_content = outcome.streamed_content
                choices = getattr(completion, "choices", None)
                if not choices:
                    raise LLMError("Ollama returned an empty response.")

                message = choices[0].message
                tool_calls = getattr(message, "tool_calls", None) or []
                content = getattr(message, "content", "") or ""

                if tool_calls or content.strip():
                    break

                if attempt == 0:
                    yield {
                        "type": "status",
                        "conversation_id": conversation_id,
                        "phase": "thinking",
                        "message": (
                            "The model returned an empty response; retrying with "
                            "a continuation prompt..."
                        ),
                    }
                else:
                    raise LLMError("Ollama returned an empty response.")

            thinking_fields = collect_thinking(outcome)

            # Some Hermes/Ollama combinations emit the tool call as JSON text
            # instead of a structured tool_calls item. Execute it, but never
            # forward the raw JSON to the user.
            text_calls: list[dict[str, Any]] = (
                [] if tool_calls else self._extract_text_tool_calls(content)
            )
            if text_calls:
                # The streamed text was raw tool-call JSON; remove the bubble.
                if streamed_content is not None:
                    yield {
                        "type": "assistant_reset",
                        "conversation_id": conversation_id,
                    }
                    streamed_content = None
                content = ""

            if any(
                getattr(getattr(call, "function", None), "name", "")
                == "analyze_screen"
                for call in tool_calls
            ) or any(
                call.get("function", {}).get("name") == "analyze_screen"
                for call in text_calls
            ):
                screen_forced = True

            content = self._normalize_reply(content)

            if (
                not screen_forced
                and not tool_calls
                and not text_calls
                and self._last_user_requested_screen(conversation_id)
            ):
                screen_forced = True
                if streamed_content is not None:
                    # The model answered without looking; replace that text.
                    yield {
                        "type": "assistant_reset",
                        "conversation_id": conversation_id,
                    }
                    streamed_content = None
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "thinking",
                    "message": "Looking at the screen...",
                }
                try:
                    outcome = yield from self._stream_completion(
                        conversation_id,
                        self._store.load(conversation_id),
                        nudge=True,
                        nudge_text=_SCREEN_NUDGE,
                        tool_choice="required",
                    )
                    forced = outcome.completion
                    forced_choices = getattr(forced, "choices", None)
                    forced_calls: list[Any] = []
                    if forced_choices:
                        forced_message = forced_choices[0].message
                        forced_calls = getattr(forced_message, "tool_calls", None) or []
                    if forced_calls:
                        tool_calls = forced_calls
                        content = getattr(forced_message, "content", "") or ""
                        text_calls = []
                        streamed_content = outcome.streamed_content
                    elif outcome.streamed_content is not None:
                        # A forced answer that did not use the screen is stale.
                        yield {
                            "type": "assistant_reset",
                            "conversation_id": conversation_id,
                        }
                except LLMError as exc:
                    logger.warning("Forced screen analysis failed: %s", exc)

            # Hermes sometimes narrates the next action ("Next, I will...")
            # or emits malformed JSON instead of calling the tool. Since
            # modifying tools are still gated by HITL, ask it to choose a tool.
            needs_forced_tool = (
                len(content.strip()) < 500
                and (
                    self._looks_like_action_promise(content)
                    or self._looks_like_malformed_tool_output(content)
                )
            )
            if (
                not tool_calls
                and not text_calls
                and content.strip()
                and needs_forced_tool
                and self._last_user_requested_action(conversation_id)
            ):
                for _forced_attempt in range(2):
                    if streamed_content is not None:
                        # Drop the narration that the retry will replace.
                        yield {
                            "type": "assistant_reset",
                            "conversation_id": conversation_id,
                        }
                        streamed_content = None
                    yield {
                        "type": "status",
                        "conversation_id": conversation_id,
                        "phase": "thinking",
                        "message": (
                            "The model described the next action; asking it to "
                            "execute the tool..."
                        ),
                    }
                    try:
                        outcome = yield from self._stream_completion(
                            conversation_id,
                            self._store.load(conversation_id),
                            nudge=True,
                            tool_choice="required",
                        )
                        forced = outcome.completion
                        forced_choices = getattr(forced, "choices", None)
                        if not forced_choices:
                            streamed_content = outcome.streamed_content
                            break
                        forced_message = forced_choices[0].message
                        forced_calls = getattr(forced_message, "tool_calls", None) or []
                        forced_content = getattr(forced_message, "content", "") or ""
                        if forced_calls:
                            tool_calls = forced_calls
                            content = forced_content
                            text_calls = []
                            streamed_content = outcome.streamed_content
                            break
                        forced_text_calls = self._extract_text_tool_calls(
                            forced_content
                        )
                        if forced_text_calls:
                            if outcome.streamed_content is not None:
                                yield {
                                    "type": "assistant_reset",
                                    "conversation_id": conversation_id,
                                }
                            text_calls = forced_text_calls
                            content = ""
                            streamed_content = None
                            break
                        if forced_content.strip():
                            content = forced_content
                        streamed_content = outcome.streamed_content
                        if (
                            len(content.strip()) >= 500
                            or not self._looks_like_action_promise(content)
                        ):
                            break
                    except LLMError as exc:
                        logger.warning("Forced tool continuation failed: %s", exc)
                        break

            if (
                recover_failed_action
                and not recovery_attempted
                and not tool_calls
                and not text_calls
                and content.strip()
            ):
                recovery_attempted = True
                if streamed_content is not None:
                    # The conclusion is being replaced by a recovery attempt.
                    yield {
                        "type": "assistant_reset",
                        "conversation_id": conversation_id,
                    }
                    streamed_content = None
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "thinking",
                    "message": (
                        "The action failed; verifying the system and trying "
                        "another approach..."
                    ),
                }
                try:
                    outcome = yield from self._stream_completion(
                        conversation_id,
                        self._store.load(conversation_id),
                        nudge=True,
                        nudge_text=_ACTION_FAILURE_NUDGE,
                        tool_choice="required",
                    )
                    recovered = outcome.completion
                    recovered_choices = getattr(recovered, "choices", None)
                    if recovered_choices:
                        recovered_message = recovered_choices[0].message
                        recovered_calls = (
                            getattr(recovered_message, "tool_calls", None) or []
                        )
                        recovered_content = (
                            getattr(recovered_message, "content", "") or ""
                        )
                        if recovered_calls:
                            tool_calls = recovered_calls
                            content = recovered_content
                            text_calls = []
                            streamed_content = outcome.streamed_content
                        else:
                            recovered_text_calls = self._extract_text_tool_calls(
                                recovered_content
                            )
                            if recovered_text_calls:
                                if outcome.streamed_content is not None:
                                    yield {
                                        "type": "assistant_reset",
                                        "conversation_id": conversation_id,
                                    }
                                text_calls = recovered_text_calls
                                content = ""
                                streamed_content = None
                            elif recovered_content.strip():
                                content = recovered_content
                                streamed_content = outcome.streamed_content
                except LLMError as exc:
                    logger.warning("Failed-action recovery attempt failed: %s", exc)

            self._raise_if_cancelled(conversation_id)
            if not tool_calls and not text_calls:
                if not content.strip():
                    raise LLMError("Ollama returned an empty response.")
                if self._looks_like_raw_tool_call(
                    content
                ) or self._looks_like_malformed_tool_output(content):
                    if streamed_content is not None:
                        yield {
                            "type": "assistant_reset",
                            "conversation_id": conversation_id,
                        }
                        streamed_content = None
                    raise LLMError(
                        "The model returned a malformed response. Please try again."
                    )
                reply = content.strip()
                self._store.append(
                    conversation_id,
                    "assistant",
                    reply,
                    **thinking_fields,
                )
                self._turn_plans.pop(conversation_id, None)
                yield {
                    "type": "final",
                    "conversation_id": conversation_id,
                    "reply": reply,
                    "streamed": (
                        streamed_content is not None
                        and self._normalize_reply(streamed_content).strip() == reply
                    ),
                    **thinking_fields,
                }
                return

            if max_tool_rounds > 0 and round_index >= max_tool_rounds:
                # Do not fail the whole turn just because the model wanted
                # another tool. Summarize the evidence collected so far without
                # offering any tools, so the user still gets a final answer.
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "thinking",
                    "message": (
                        "Tool limit reached; summarizing the results collected "
                        "so far..."
                    ),
                }
                try:
                    outcome = yield from self._stream_completion(
                        conversation_id,
                        self._store.load(conversation_id),
                        nudge=True,
                        nudge_text=(
                            "You have reached the tool-call limit. Do not call "
                            "any more tools. Summarize the findings and results "
                            "you already have, state clearly what could not be "
                            "checked or completed, and give the user a useful "
                            "final answer. Do not claim an action succeeded "
                            "unless its tool result confirmed it."
                        ),
                        include_tools=False,
                    )
                except LLMError as exc:
                    logger.warning("Tool-budget summary failed: %s", exc)
                    raise LLMError(
                        "The agent exceeded the maximum number of tool steps.",
                        status_code=502,
                    ) from exc

                completion = outcome.completion
                choices = getattr(completion, "choices", None)
                summary = ""
                if choices:
                    summary = (
                        getattr(choices[0].message, "content", "") or ""
                    )
                thinking_fields = collect_thinking(outcome)
                reply = self._normalize_reply(summary).strip()
                if reply and not self._looks_like_raw_tool_call(reply):
                    self._store.append(
                        conversation_id,
                        "assistant",
                        reply,
                        **thinking_fields,
                    )
                    self._turn_plans.pop(conversation_id, None)
                    yield {
                        "type": "final",
                        "conversation_id": conversation_id,
                        "reply": reply,
                        "streamed": (
                            outcome.streamed_content is not None
                            and self._normalize_reply(
                                outcome.streamed_content
                            ).strip()
                            == reply
                        ),
                        **thinking_fields,
                    }
                    return

                raise LLMError(
                    "The agent exceeded the maximum number of tool steps.",
                    status_code=502,
                )

            serialized_calls = (
                [self._serialize_tool_call(call) for call in tool_calls]
                if tool_calls
                else text_calls
            )
            serialized_calls = [
                self._normalize_serialized_call(conversation_id, call)
                for call in serialized_calls
            ]

            confirmation_calls = [
                call
                for call in serialized_calls
                if self._tools.requires_confirmation(call["function"]["name"])
            ]
            read_only_calls = [
                call
                for call in serialized_calls
                if not self._tools.requires_confirmation(call["function"]["name"])
            ]

            user_message = self._last_user_message(conversation_id)

            # A screen analysis is paused before any tool runs while more than
            # one display is connected. Capturing first would either guess a
            # monitor or leave an unresolved tool call in the history.
            screen_call = next(
                (
                    call
                    for call in read_only_calls
                    if call["function"]["name"]
                    in {"analyze_screen", "analyze_screen_ui"}
                ),
                None,
            )

            if screen_call is not None:
                screen_arguments = _parse_arguments(screen_call)
                # A named window determines its own display; only the generic
                # screen question needs the multi-display chooser.
                if not screen_arguments.get("window_title"):
                    displays = self._tools.display_choices()
                    if len(displays) > 1:
                        pending = self._pending.create(
                            conversation_id,
                            user_message,
                            [screen_call],
                            [],
                            assistant_message=content.strip(),
                            kind="display",
                            displays=displays,
                            question=str(
                                screen_arguments.get("user_question")
                                or screen_arguments.get("question")
                                or ""
                            ),
                        )
                        yield pending.to_public()
                        return

            self._store.append(
                conversation_id,
                "assistant",
                content,
                tool_calls=serialized_calls,
            )

            invalid_open_calls = [
                call
                for call in confirmation_calls
                if call["function"]["name"] == "open_file"
                and not _program_was_named_by_user(
                    user_message,
                    str(_parse_arguments(call).get("program", "")),
                )
            ]

            if invalid_open_calls:
                for call in invalid_open_calls:
                    self._store.append(
                        conversation_id,
                        "tool",
                        json.dumps(
                            {
                                "ok": False,
                                "needs_program": True,
                                "error": (
                                    "The user did not name an editor or program. "
                                    "Ask which application to use; do not choose "
                                    "one based on the file extension."
                                ),
                            }
                        ),
                        tool_call_id=call["id"],
                        name="open_file",
                    )

                first_path = str(
                    _parse_arguments(invalid_open_calls[0]).get("path", "")
                )
                file_name = _short_path(first_path) or "this file"
                reply = (
                    f"Which editor or program should I use to open {file_name}?"
                )
                self._store.append(
                    conversation_id,
                    "assistant",
                    reply,
                    **thinking_fields,
                )
                self._turn_plans.pop(conversation_id, None)
                yield {
                    "type": "final",
                    "conversation_id": conversation_id,
                    "reply": reply,
                    "streamed": False,
                    **thinking_fields,
                }
                return

            if auto_approve:
                # The user approved this request once. Execute every call in
                # the model's order, including mixed read-only and modifying
                # calls, and keep going until the final answer.
                for call in serialized_calls:
                    self._raise_if_cancelled(conversation_id)
                    name = call["function"]["name"]
                    arguments = call["function"]["arguments"]
                    parsed = _parse_arguments(call)
                    requires_confirmation = self._tools.requires_confirmation(name)
                    duplicate = False

                    if requires_confirmation:
                        signature = self._call_signature(name, arguments)
                        # Keyboard input is often legitimately repeated (Tab
                        # between fields, the same text in two fields). Only
                        # deduplicate actions whose exact repetition is a loop.
                        dedupe = name not in {
                            "keyboard_press_keys",
                            "keyboard_type_text",
                        }
                        if dedupe and signature in executed:
                            duplicate = True
                            result = {
                                "ok": False,
                                "error": (
                                    "This action already ran in the current "
                                    "request and was not repeated."
                                ),
                            }
                        else:
                            if dedupe:
                                executed.add(signature)
                            auto_approved_actions += 1
                            if (
                                max_auto_approved > 0
                                and auto_approved_actions > max_auto_approved
                            ):
                                raise LLMError(
                                    "The agent exceeded the configured limit "
                                    "of automatic actions for one request.",
                                    status_code=502,
                                )
                            yield {
                                "type": "status",
                                "conversation_id": conversation_id,
                                "phase": "tool_started",
                                "tool": name,
                                "arguments": parsed,
                                "message": _tool_status_message(name, parsed),
                            }
                            result = self._execute_tool(
                                name,
                                arguments,
                                conversation_id,
                            )
                    else:
                        if self._note_read_call(
                            read_repeat,
                            self._call_signature(name, arguments),
                        ):
                            force_summary = True
                            result = self._repeated_read_result(name)
                        else:
                            yield {
                                "type": "status",
                                "conversation_id": conversation_id,
                                "phase": "tool_started",
                                "tool": name,
                                "arguments": parsed,
                                "message": _tool_status_message(name, parsed),
                            }
                            result = self._execute_tool(
                                name,
                                arguments,
                                conversation_id,
                            )

                    if name == "propose_plan":
                        self._record_plan(conversation_id, result)

                    self._store.append(
                        conversation_id,
                        "tool",
                        _model_facing_tool_result(name, result),
                        tool_call_id=call["id"],
                        name=name,
                    )
                    yield self._tool_result_event(
                        conversation_id,
                        name,
                        result,
                    )
                    if (
                        not duplicate
                        and name == "run_powershell_action"
                        and not result.get("ok")
                    ):
                        recover_failed_action = True
                round_index += 1
                continue

            for call in read_only_calls:
                self._raise_if_cancelled(conversation_id)
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                parsed = _parse_arguments(call)
                if self._note_read_call(
                    read_repeat,
                    self._call_signature(name, arguments),
                ):
                    force_summary = True
                    result = self._repeated_read_result(name)
                else:
                    yield {
                        "type": "status",
                        "conversation_id": conversation_id,
                        "phase": "tool_started",
                        "tool": name,
                        "arguments": parsed,
                        "message": _tool_status_message(name, parsed),
                    }
                    result = self._execute_tool(name, arguments, conversation_id)
                if name == "propose_plan":
                    self._record_plan(conversation_id, result)
                self._store.append(
                    conversation_id,
                    "tool",
                    _model_facing_tool_result(name, result),
                    tool_call_id=call["id"],
                    name=name,
                )
                yield self._tool_result_event(conversation_id, name, result)

            self._raise_if_cancelled(conversation_id)
            if confirmation_calls:
                actions = [
                    {
                        "tool": call["function"]["name"],
                        "arguments": _parse_arguments(call),
                        "description": _action_description(
                            call["function"]["name"],
                            _parse_arguments(call),
                        ),
                    }
                    for call in confirmation_calls
                ]
                model_plan = self._normalize_plan_steps(
                    self._turn_plans.get(conversation_id)
                    or self._extract_plan_steps(content)
                )
                request_plan = self._plan_from_request(user_message)
                if (
                    len(request_plan) >= 2
                    and len(request_plan) >= len(model_plan)
                ):
                    # The user's explicit wording is usually more complete and
                    # specific than the model's short plan.
                    plan = request_plan
                else:
                    plan = model_plan or request_plan

                pending = self._pending.create(
                    conversation_id,
                    user_message,
                    confirmation_calls,
                    actions,
                    assistant_message=content.strip(),
                    plan=plan,
                )
                yield pending.to_public()
                return

            round_index += 1

    def generate_reply(self, conversation_id: str, message: str) -> str:
        """Legacy non-streaming helper.

        Simple tool-free chats work as before. If a modifying tool is required,
        this raises ``ConfirmationRequired`` so the caller can return 409.
        """
        events = list(self.start_turn(conversation_id, message))

        for event in events:
            if event["type"] in {"action_required", "display_required"}:
                pending = self._pending.get(event["approval_id"])
                raise ConfirmationRequired(pending)

        error = next((event for event in events if event["type"] == "error"), None)
        if error is not None:
            raise LLMError(
                error.get("message", "The agent failed."),
                status_code=error.get("status", 502),
            )

        final = next((event for event in events if event["type"] == "final"), None)
        if final is None:
            raise LLMError("The agent did not produce a reply.")
        return final["reply"]

    # ---------------------------------------------------------------- learning
    def learn_from_exchange(self, user_message: str) -> list[tuple[str, str]]:
        """Extract durable facts from the user's message and store them."""
        if not self._settings.memory.learn_from_chats:
            return []

        if not _PERSONAL_CUE.search(user_message):
            # Commands, tool requests, and general questions rarely contain
            # durable personal facts. Skipping that LLM call keeps the model
            # free for rapid follow-ups without losing first-person facts.
            return []

        try:
            knowledge_text = self._knowledge.read()
            learned = extract_facts(
                self._client,
                self._settings.ollama.model,
                knowledge_text,
                user_message,
            )
        except Exception as exc:  # noqa: BLE001 - background learning is best effort
            logger.warning("Knowledge extraction failed: %s", exc)
            return []

        deletions = list(
            dict.fromkeys(
                [
                    *(
                        fact
                        for fact in learned.deletions
                        if should_apply_deletion(fact, user_message)
                    ),
                    *infer_deletions(knowledge_text, user_message),
                ]
            )
        )
        added = self._knowledge.add_facts(learned.facts, deletions)
        if added:
            logger.info("Learned %d new fact(s) about the user", len(added))
        return added

    # ------------------------------------------------------------------ LLM
    def warm_up(self) -> bool:
        """Load the model and cache the stable prompt/tool prefix.

        This runs in a background thread at startup. The dummy user message is
        intentionally short: Ollama stores the common system+tools prefix even
        though the final user tokens differ from the first real message.
        """
        if self._settings.ollama.keep_alive_seconds <= 0:
            return False

        started = time.monotonic()
        try:
            messages = self._build_messages(
                [{"role": "user", "content": "Reply with OK."}]
            )
            self._client.chat.completions.create(
                model=self._settings.ollama.model,
                messages=messages,
                tools=self._tool_schemas(
                    [{"role": "user", "content": "Reply with OK."}]
                ),
                tool_choice="auto",
                temperature=0.0,
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 - startup must not fail
            logger.warning("Model warm-up failed: %s", exc)
            return False

        logger.info(
            "Model warm-up finished in %.2f seconds", time.monotonic() - started
        )
        return True

    def _completion_messages(
        self,
        history: list[dict[str, Any]],
        *,
        nudge: bool,
        nudge_text: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        model = self._settings.ollama.model
        messages = self._build_messages(history)

        if nudge:
            messages.append(
                {
                    "role": (
                        "user"
                        if self._system_prompt_in_user_message()
                        else "system"
                    ),
                    "content": nudge_text
                    or (
                        "Continue the user's request. Choose the next single tool "
                        "call, or provide the final answer if the plan is complete. "
                        "Do not repeat an action that already succeeded."
                    ),
                }
            )

        return model, messages

    def _request_error(self, exc: Exception, model: str) -> LLMError:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 404:
                return LLMError(
                    f"Ollama model '{model}' is not installed. "
                    f"Run `ollama pull {model}`.",
                    status_code=503,
                )
            return LLMError(
                f"Ollama returned an error (HTTP {exc.code}).",
                status_code=502,
            )
        if isinstance(exc, (TimeoutError, urllib.error.URLError)):
            if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(
                exc, TimeoutError
            ):
                return LLMError(
                    "Ollama took too long to respond. Try a shorter message.",
                    status_code=504,
                )
            return LLMError(
                "I can't reach Ollama at "
                f"{self._settings.ollama.base_url}. Make sure Ollama is running "
                "(`ollama serve`).",
                status_code=503,
            )
        if isinstance(exc, APITimeoutError):
            return LLMError(
                "Ollama took too long to respond. Try a shorter message.",
                status_code=504,
            )
        if isinstance(exc, APIConnectionError):
            return LLMError(
                "I can't reach Ollama at "
                f"{self._settings.ollama.base_url}. Make sure Ollama is running "
                "(`ollama serve`).",
                status_code=503,
            )
        if isinstance(exc, NotFoundError):
            return LLMError(
                f"Ollama model '{model}' is not installed. "
                f"Run `ollama pull {model}`.",
                status_code=503,
            )
        if isinstance(exc, APIStatusError):
            return LLMError(
                f"Ollama returned an error (HTTP {exc.status_code}).",
                status_code=502,
            )
        if isinstance(exc, json.JSONDecodeError):
            return LLMError(
                "The model returned malformed streaming data.",
                status_code=502,
            )
        return LLMError("The LLM request failed.", status_code=502)

    def _metrics_event(
        self,
        conversation_id: str,
        data: dict[str, Any],
        *,
        live: bool,
        completion_tokens: int | None = None,
        tokens_per_second: float | None = None,
        first_token_ms: float | None = None,
        thinking_ms: float | None = None,
        thinking_tokens: int | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": "metrics",
            "conversation_id": conversation_id,
            "live": live,
            "model": self._settings.ollama.model,
        }
        if first_token_ms is not None:
            event["first_token_ms"] = round(first_token_ms, 1)
        if thinking_ms is not None:
            event["thinking_ms"] = round(thinking_ms, 1)
            # Ollama has no separate thinking token count, so this is the number
            # of streamed thinking chunks; close enough for a live readout and
            # always labeled as an estimate in the UI.
            if thinking_tokens is not None:
                event["thinking_tokens"] = thinking_tokens
        if completion_tokens is not None:
            event["completion_tokens"] = completion_tokens
        if tokens_per_second is not None:
            event["tokens_per_second"] = round(tokens_per_second, 1)

        prompt_tokens = data.get("prompt_eval_count")
        eval_count = data.get("eval_count")
        prompt_ns = data.get("prompt_eval_duration")
        eval_ns = data.get("eval_duration")
        load_ns = data.get("load_duration")
        total_ns = data.get("total_duration")

        if isinstance(prompt_tokens, int):
            event["prompt_tokens"] = prompt_tokens
        if isinstance(eval_count, int) and not live:
            event["completion_tokens"] = eval_count
            if isinstance(eval_ns, (int, float)) and eval_ns > 0:
                event["tokens_per_second"] = round(
                    eval_count / (float(eval_ns) / 1_000_000_000),
                    1,
                )
        if (
            isinstance(prompt_tokens, int)
            and isinstance(prompt_ns, (int, float))
            and prompt_ns > 0
        ):
            event["prompt_tokens_per_second"] = round(
                prompt_tokens / (float(prompt_ns) / 1_000_000_000),
                1,
            )
        if isinstance(prompt_ns, (int, float)):
            event["prompt_eval_ms"] = round(float(prompt_ns) / 1_000_000, 1)
        if isinstance(eval_ns, (int, float)):
            event["eval_ms"] = round(float(eval_ns) / 1_000_000, 1)
        if isinstance(load_ns, (int, float)):
            event["load_ms"] = round(float(load_ns) / 1_000_000, 1)
        if isinstance(total_ns, (int, float)):
            event["total_ms"] = round(float(total_ns) / 1_000_000, 1)
        return event

    def _thinking_mode(self, history: list[dict[str, Any]]) -> bool | str | None:
        """Return the value for Ollama's ``think`` field, or None to omit it.

        ``off`` disables it, ``low``/``medium``/``high`` request a budget, and
        ``auto`` thinks on turns that need a plan while skipping short,
        conversational turns so the first visible token arrives sooner.
        Non-thinking models never receive the field at all.
        """
        setting = self._settings.ollama.thinking
        if setting == "off":
            return False

        supports = getattr(self._client, "supports_thinking", None)
        if not callable(supports):
            return None
        try:
            if not supports(self._settings.ollama.model):
                return None
        except Exception:  # noqa: BLE001 - a probe must never fail the turn
            logger.debug("Thinking capability probe failed", exc_info=True)
            return None

        if setting in {"low", "medium", "high"}:
            return setting
        return True if self._should_think(history) else False

    def _should_think(self, history: list[dict[str, Any]]) -> bool:
        """Decide whether an auto-thinking turn is worth the latency.

        Short greetings, acknowledgements, and one-line lookups go straight to
        the answer. Anything that asks for a plan, compares options, reasons
        about numbers, or is long enough to be a real task gets thinking.
        """
        request = self._last_user_message_in(history).strip()
        if not request:
            return False
        if _SMALL_TALK.match(request):
            return False
        if len(request) >= self._settings.ollama.thinking_min_chars:
            return True
        if _THINKING_HINT.search(request):
            return True
        return request.count("?") + request.count("\n") >= 2

    @staticmethod
    def _last_user_message_in(history: list[dict[str, Any]]) -> str:
        for message in reversed(history):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    def _stream_completion(
        self,
        conversation_id: str,
        history: list[dict[str, Any]],
        *,
        nudge: bool = False,
        nudge_text: str | None = None,
        tool_choice: str = "auto",
        include_tools: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Stream content deltas/metrics, then return an OpenAI-shaped completion."""
        model, messages = self._completion_messages(
            history,
            nudge=nudge,
            nudge_text=nudge_text,
        )

        stream_chat = getattr(self._client, "stream_chat", None)
        thinking_mode = self._thinking_mode(history)
        if stream_chat is None:
            completion = self._create_completion(
                history,
                nudge=nudge,
                nudge_text=nudge_text,
                tool_choice=tool_choice,
                include_tools=include_tools,
                think=thinking_mode,
            )
            self._raise_if_cancelled(conversation_id)
            choices = getattr(completion, "choices", None) or []
            message = choices[0].message if choices else None
            content = (getattr(message, "content", "") or "") if message else ""
            thinking = (getattr(message, "thinking", "") or "") if message else ""
            if content.strip():
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "generating",
                    "message": "Generating answer...",
                }
                yield {
                    "type": "assistant_start",
                    "conversation_id": conversation_id,
                }
                yield {
                    "type": "assistant_delta",
                    "conversation_id": conversation_id,
                    "content": content,
                }
                yield {
                    "type": "assistant_end",
                    "conversation_id": conversation_id,
                    "content": content,
                }
            return StreamOutcome(
                completion,
                content or None,
                thinking=thinking or None,
            )

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[Any] = []
        started = False
        token_count = 0
        thinking_tokens = 0
        first_token_at: float | None = None
        first_thinking_at: float | None = None
        last_thinking_at: float | None = None
        thinking_announced = False
        last_live_at = time.monotonic()
        metrics_data: dict[str, Any] = {}
        request_started = time.monotonic()
        self._raise_if_cancelled(conversation_id)
        chunks = stream_chat(
            model=model,
            messages=messages,
            tools=self._tool_schemas(history) if include_tools else None,
            tool_choice=tool_choice,
            temperature=self._settings.ollama.temperature,
            think=thinking_mode,
        )

        try:
            for chunk in chunks:
                self._raise_if_cancelled(conversation_id)
                message = chunk.get("message") or {}
                thinking_delta = message.get("thinking") or ""
                delta = message.get("content") or ""

                if thinking_delta:
                    now = time.monotonic()
                    if first_thinking_at is None:
                        first_thinking_at = now
                    last_thinking_at = now
                    thinking_tokens += 1
                    thinking_parts.append(thinking_delta)
                    if not thinking_announced:
                        thinking_announced = True
                        yield {
                            "type": "status",
                            "conversation_id": conversation_id,
                            "phase": "thinking",
                            "message": "Thinking...",
                        }
                    yield {
                        "type": "assistant_thinking",
                        "conversation_id": conversation_id,
                        "content": thinking_delta,
                    }

                if delta:
                    now = time.monotonic()
                    if not started:
                        started = True
                        first_token_at = now
                        last_live_at = now
                        yield {
                            "type": "status",
                            "conversation_id": conversation_id,
                            "phase": "generating",
                            "message": "Generating answer...",
                        }
                        yield {
                            "type": "assistant_start",
                            "conversation_id": conversation_id,
                        }

                    content_parts.append(delta)
                    token_count += 1
                    yield {
                        "type": "assistant_delta",
                        "conversation_id": conversation_id,
                        "content": delta,
                    }

                    elapsed = now - (first_token_at or now)
                    if elapsed >= 0.5 and now - last_live_at >= 0.5:
                        last_live_at = now
                        yield self._metrics_event(
                            conversation_id,
                            metrics_data,
                            live=True,
                            completion_tokens=token_count,
                            tokens_per_second=token_count / max(elapsed, 0.001),
                        )

                for raw_call in message.get("tool_calls") or []:
                    function = raw_call.get("function") or {}
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                    tool_calls.append(
                        SimpleNamespace(
                            id=raw_call.get("id") or f"call_{uuid.uuid4().hex}",
                            type="function",
                            function=SimpleNamespace(
                                name=function.get("name", ""),
                                arguments=arguments,
                            ),
                        )
                    )

                if chunk.get("error"):
                    raise LLMError(
                        f"Ollama error: {chunk['error']}",
                        status_code=502,
                    )

                if chunk.get("done"):
                    metrics_data = chunk
        except TurnCancelled:
            self._store_partial_stream(
                conversation_id,
                "".join(content_parts),
            )
            raise
        except GeneratorExit:
            self._store_partial_stream(
                conversation_id,
                "".join(content_parts),
            )
            raise
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated into an LLMError
            raise self._request_error(exc, model) from exc
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - never mask the real error
                    logger.debug("Closing the Ollama stream failed", exc_info=True)

        full_content = "".join(content_parts)
        full_thinking = "".join(thinking_parts).strip()
        thinking_end = first_token_at or last_thinking_at
        thinking_ms = (
            (thinking_end - first_thinking_at) * 1000
            if first_thinking_at is not None and thinking_end is not None
            else None
        )
        if started:
            yield {
                "type": "assistant_end",
                "conversation_id": conversation_id,
                "content": full_content,
            }
        if metrics_data:
            yield self._metrics_event(
                conversation_id,
                metrics_data,
                live=False,
                first_token_ms=(
                    (first_token_at - request_started) * 1000
                    if first_token_at is not None
                    else None
                ),
                thinking_ms=thinking_ms,
                thinking_tokens=thinking_tokens or None,
            )

        return StreamOutcome(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=full_content,
                            tool_calls=tool_calls or None,
                        )
                    )
                ]
            ),
            full_content or None,
            thinking=full_thinking or None,
            thinking_ms=thinking_ms,
            thinking_tokens=thinking_tokens or None,
        )

    def _create_completion(
        self,
        history: list[dict[str, Any]],
        *,
        nudge: bool = False,
        nudge_text: str | None = None,
        tool_choice: str = "auto",
        include_tools: bool = True,
        think: bool | str | None = None,
    ) -> Any:
        model, messages = self._completion_messages(
            history,
            nudge=nudge,
            nudge_text=nudge_text,
        )

        try:
            return self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=self._tool_schemas(history) if include_tools else None,
                tool_choice=tool_choice,
                temperature=self._settings.ollama.temperature,
                think=think,
            )
        except Exception as exc:  # noqa: BLE001 - translated for the API
            raise self._request_error(exc, model) from exc

    @staticmethod
    def _normalize_reply(text: str) -> str:
        """Repair file URLs the model sometimes writes as file:///C/... .

        The frontend expects a drive colon; without it the link cannot be
        opened. Already-correct URLs are left untouched.
        """
        return _FILE_URL_WITHOUT_COLON.sub(
            lambda match: f"file:///{match.group(1).upper()}:/",
            text,
        )

    @staticmethod
    def _extract_plan_steps(text: str) -> list[str]:
        """Extract a short numbered/bulleted plan from the model's narration."""
        steps: list[str] = []
        for line in text.splitlines():
            match = _PLAN_STEP.match(line)
            if not match:
                continue
            step = " ".join(match.group(1).split())
            if step and len(step) <= 240:
                steps.append(step)
            if len(steps) >= 8:
                break
        return steps

    @staticmethod
    def _clean_plan_clause(part: str) -> str:
        sentence = re.split(r"(?<=[.!?])\s+", part.strip())[0]
        cleaned = _LEADING_CONNECTOR.sub("", sentence.strip(" \t\r\n.,;:-"))
        cleaned = cleaned.strip(" \t\r\n.,;:-")
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
        return cleaned

    @classmethod
    def _split_plan_clauses(cls, text: str) -> list[str]:
        """Split a request into individual action clauses.

        Sequence words are handled first. Commas and ``and``/``und`` are only
        treated as separators when the following clause starts with an action
        verb, so paths and prose containing commas stay intact.
        """
        clauses: list[str] = []

        for segment in _SEQUENCE_SPLIT.split(text):
            comma_parts = _COMMA_ACTION_SPLIT.split(segment)
            candidates: list[str] = []
            for comma_part in comma_parts:
                candidates.extend(
                    re.split(
                        r"\s+(?:and|und)\s+",
                        comma_part,
                        flags=re.IGNORECASE,
                    )
                )

            cleaned = [cls._clean_plan_clause(part) for part in candidates]
            cleaned = [part for part in cleaned if part]
            if not cleaned:
                continue

            if len(cleaned) > 1 and not all(
                _ACTION_START.match(part) and not _META_STEP.match(part)
                for part in cleaned
            ):
                fallback = cls._clean_plan_clause(segment)
                if fallback:
                    clauses.append(fallback)
                continue

            clauses.extend(cleaned)

        return clauses

    @classmethod
    def _normalize_plan_steps(cls, steps: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for step in steps:
            for clause in cls._split_plan_clauses(str(step)):
                if len(clause) < 3 or _META_STEP.match(clause):
                    continue
                key = clause.casefold()
                if key in seen:
                    continue
                seen.add(key)
                normalized.append(clause[:240])
                if len(normalized) >= 8:
                    return normalized

        return normalized

    @classmethod
    def _plan_from_request(cls, message: str) -> list[str]:
        """Turn an explicit multi-step request into a short display plan.

        Handles "first X, then Y", German "X, dann Y", comma lists, and plain
        conjunctions such as "create X and write Y" / "erstelle X und
        erstelle Y". A conjunction or comma is only treated as a separator when
        every resulting clause starts with an action verb.
        """
        text = " ".join(message.split())
        if not text:
            return []

        numbered = cls._extract_plan_steps(message)
        if len(numbered) > 1:
            return cls._normalize_plan_steps(numbered)

        steps = [
            clause
            for clause in cls._split_plan_clauses(text)
            if len(clause) >= 3 and not _META_STEP.match(clause)
        ]
        if len(steps) <= 1:
            return []
        return cls._normalize_plan_steps(steps)[:8]

    @staticmethod
    def _model_facing_diagnosis(content: str) -> str:
        """Remove UI-only artifact paths from a diagnosis tool result."""
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content
        if not isinstance(parsed, dict):
            return content
        for key in ("charts", "files", "report_dir", "report_file"):
            parsed.pop(key, None)
        return json.dumps(parsed, ensure_ascii=False, default=str)

    @staticmethod
    def _call_signature(name: str, arguments: Any) -> tuple[str, str]:
        """Normalize a tool call for duplicate detection within one turn."""
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                return name, arguments
        else:
            parsed = arguments
        return name, json.dumps(parsed, sort_keys=True, default=str)

    @staticmethod
    def _serialize_tool_call(call: Any) -> dict[str, Any]:
        function = getattr(call, "function", None)
        arguments = getattr(function, "arguments", "{}")

        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)

        return {
            "id": getattr(call, "id", None) or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": getattr(function, "name", ""),
                "arguments": arguments,
            },
        }

    @staticmethod
    def _extract_text_tool_calls(content: str) -> list[dict[str, Any]]:
        """Parse a tool call that Hermes emitted as JSON text."""
        text = content.strip()
        if not text:
            return []

        wrapped = re.search(
            r"<tool_call>\s*(.*?)\s*</tool_call>",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if wrapped:
            text = wrapped.group(1).strip()

        if text.startswith("```"):
            text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []

        items = data if isinstance(data, list) else [data]
        calls: list[dict[str, Any]] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                function = {}
            name = item.get("name") or function.get("name")
            arguments = item.get("arguments", function.get("arguments"))
            if not isinstance(name, str) or not name.strip() or arguments is None:
                continue
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append(
                {
                    "id": item.get("id") or f"call_text_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )

        return calls

    @staticmethod
    def _looks_like_raw_tool_call(content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        return (
            text.startswith("{") or "<tool_call" in text.lower()
        ) and ('"name"' in text or '"function"' in text) and (
            '"arguments"' in text or '"parameters"' in text
        )

    @staticmethod
    def _looks_like_malformed_tool_output(content: str) -> bool:
        text = content.strip()
        if not text or text[0] not in "[{<":
            return False
        if text.lower().startswith("<tool_call"):
            return True
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return True
        return False

    @staticmethod
    def _tool_result_event(
        conversation_id: str,
        name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        summary = result.get("message") or result.get("error") or "Tool completed."
        return {
            "type": "tool_result",
            "conversation_id": conversation_id,
            "tool": name,
            "ok": bool(result.get("ok")),
            "summary": summary,
            "result": result,
        }

    @staticmethod
    def _thinking_fields(
        parts: list[str],
        thinking_ms: float | None,
        thinking_tokens: int | None,
    ) -> dict[str, Any]:
        """Bundle the turn's reasoning for the store, the ``final`` event, and
        the UI. The text is capped so one long reasoning phase cannot bloat the
        in-memory conversation or the SSE frame."""
        text = "\n\n".join(part.strip() for part in parts if part and part.strip())
        if len(text) > _THINKING_STORE_LIMIT:
            text = text[:_THINKING_STORE_LIMIT].rstrip() + "\n[...]"

        fields: dict[str, Any] = {}
        if text:
            fields["thinking"] = text
        if thinking_ms is not None:
            fields["thinking_ms"] = round(thinking_ms, 1)
        if thinking_tokens:
            fields["thinking_tokens"] = thinking_tokens
        return fields

    def _last_user_message(self, conversation_id: str) -> str:
        for message in reversed(self._store.load(conversation_id)):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    def _last_user_requested_screen(self, conversation_id: str) -> bool:
        return bool(_SCREEN_REQUEST.search(self._last_user_message(conversation_id)))

    def _normalize_serialized_call(
        self,
        conversation_id: str,
        call: dict[str, Any],
    ) -> dict[str, Any]:
        name = call.get("function", {}).get("name", "")
        raw_arguments = call.get("function", {}).get("arguments", "{}")

        if isinstance(raw_arguments, dict):
            parsed = dict(raw_arguments)
            original = json.dumps(raw_arguments, ensure_ascii=False)
        elif isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                return call
            original = raw_arguments
        else:
            return call

        if not isinstance(parsed, dict):
            return call

        normalized = original
        changed = False

        for key in _PATH_ARGUMENT_KEYS:
            value = parsed.get(key)
            if isinstance(value, str):
                fixed = _fix_placeholder_path(value)
                if fixed != value:
                    parsed[key] = fixed
                    changed = True

        if name == "search_system":
            normalized = self._normalize_search_arguments(conversation_id, parsed)
            changed = changed or normalized != original
        elif changed:
            normalized = json.dumps(parsed, ensure_ascii=False)

        if not changed:
            return call

        return {
            **call,
            "function": {**call.get("function", {}), "arguments": normalized},
        }

    def _normalize_search_arguments(
        self,
        conversation_id: str,
        arguments: str | dict[str, Any],
    ) -> str:
        """Correct common search misuses without overriding explicit constraints."""
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return arguments
        else:
            parsed = dict(arguments)

        if not isinstance(parsed, dict):
            return arguments if isinstance(arguments, str) else json.dumps(arguments)

        user = self._last_user_message(conversation_id).lower()

        explicit_all_drives = bool(
            re.search(
                r"\b(?:all drives|every drive|entire computer|whole computer|"
                r"alle laufwerke|allen laufwerken|ganzen computer)\b",
                user,
            )
        )
        if not explicit_all_drives:
            parsed["all_drives"] = False

        alias = None
        if re.search(r"\b(?:downloads?|downloadordner|heruntergeladen)\b", user):
            alias = "downloads"
        elif re.search(r"\b(?:dokumente|dokumenten|documents?)\b", user):
            alias = "documents"
        elif re.search(r"\b(?:desktop|schreibtisch)\b", user):
            alias = "desktop"
        elif re.search(r"\b(?:pictures?|bilder)\b", user):
            alias = "pictures"
        elif re.search(r"\b(?:music|musik)\b", user):
            alias = "music"
        elif re.search(r"\b(?:videos?)\b", user):
            alias = "videos"

        root = str(parsed.get("root") or "").strip()
        root_exists = False
        if root:
            try:
                root_exists = Path(root).expanduser().exists()
            except (OSError, ValueError):
                root_exists = False

        if alias and (
            not root
            or re.fullmatch(r"[A-Za-z]:\\?", root)
            or re.fullmatch(r"[A-Za-z]:/?", root)
            or not root_exists
        ):
            parsed["root"] = alias

        root_name = Path(str(parsed.get("root") or "")).name.casefold()
        name_value = str(parsed.get("name_contains") or "").strip().casefold()
        ext_value = str(parsed.get("extension") or "").strip().lstrip(".").casefold()
        generic_names = {
            "downloads",
            "download",
            "documents",
            "dokumente",
            "desktop",
            "schreibtisch",
            "pictures",
            "bilder",
            "music",
            "musik",
            "videos",
            "video",
        }
        if name_value and (
            name_value == root_name
            or name_value in generic_names
            or (
                ext_value
                and name_value in {ext_value, f".{ext_value}"}
            )
        ):
            parsed.pop("name_contains", None)

        wants_recent = bool(
            re.search(
                r"\b(?:last|latest|newest|recent|letzten|letzte|neuesten|neueste)\b",
                user,
            )
        )
        wants_oldest = bool(
            re.search(r"\b(?:oldest|earliest|ältesten|aeltesten)\b", user)
        )

        if wants_recent or wants_oldest:
            numbers = re.findall(r"\b(\d{1,3})\b", user)
            limit = int(numbers[0]) if numbers else 5
            parsed["max_results"] = max(1, min(limit, 200))
            parsed["sort_by"] = "modified_asc" if wants_oldest else "modified_desc"

            # "Last files in Downloads/Documents" normally means direct
            # children, not files nested inside extracted archives.
            if alias in {"downloads", "documents"} and not re.search(
                r"\b(?:recursive|subfolders?|subfolders|unterordner|rekursiv)\b",
                user,
            ):
                parsed["recursive"] = False

            if not re.search(r"\b(?:hidden|versteckt)\b", user):
                parsed["include_hidden"] = False

            if not re.search(
                r"\b(?:pdf|txt|docx?|xlsx?|pptx?|csv|json|zip|png|jpe?g)\b",
                user,
            ):
                parsed.pop("extension", None)

            if not re.search(
                r"\b(?:size|kb|mb|gb|bytes?|groesse|größe|klein|gross|groß)\b",
                user,
            ):
                parsed.pop("min_size", None)
                parsed.pop("max_size", None)

            if not re.search(
                r"\b(?:date|datum|modified|geändert|geaendert|20\d{2}|month|monat)\b",
                user,
            ):
                parsed.pop("modified_after", None)
                parsed.pop("modified_before", None)

            name_value = str(parsed.get("name_contains") or "").strip().lower()
            if not re.search(r"\b(?:named|called|namens)\b", user):
                parsed.pop("name_contains", None)
            elif not name_value or name_value not in user:
                parsed.pop("name_contains", None)

        return json.dumps(parsed, ensure_ascii=False)

    @staticmethod
    def _looks_like_action_promise(text: str) -> bool:
        return bool(_ACTION_PROMISE.search(text))

    def _last_user_requested_action(self, conversation_id: str) -> bool:
        last = self._last_user_message(conversation_id).lower()
        return any(
            word in last
            for word in (
                "create",
                "move",
                "delete",
                "copy",
                "unzip",
                "extract",
                "organize",
                "rename",
                "open",
                "launch",
                "folder",
                "file",
            )
        )

    def _error_event(self, conversation_id: str, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, LLMError):
            return {
                "type": "error",
                "conversation_id": conversation_id,
                "message": str(exc),
                "status": exc.status_code,
            }
        if isinstance(exc, ApprovalError):
            return {
                "type": "error",
                "conversation_id": conversation_id,
                "message": str(exc),
                "status": 404 if isinstance(exc, ApprovalNotFound) else 409,
            }

        logger.exception("Agent turn failed")
        return {
            "type": "error",
            "conversation_id": conversation_id,
            "message": "The agent failed while processing the request.",
            "status": 500,
        }

    def _system_prompt_in_user_message(self) -> bool:
        """Whether the model's template needs the prompt folded into a user turn.

        ``hermes3`` and related Hermes builds render only the tool preamble when
        tools are supplied and omit the system message in that branch. Since the
        agent always sends tools, a system-role prompt would be silently lost.
        """
        mode = self._settings.ollama.system_prompt_mode
        if mode == "user":
            return True
        if mode == "system":
            return False
        return "hermes" in self._settings.ollama.model.casefold()

    @staticmethod
    def _fold_system_prompt(
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> None:
        """Prepend the system prompt to the latest user message in place.

        The latest turn is used deliberately: when a long conversation is
        truncated to fit the context window, Ollama drops the oldest tokens
        first. Keeping the prompt on the newest user turn means the identity
        and rules survive truncation.
        """
        for message in reversed(messages):
            if message["role"] != "user":
                continue
            message["content"] = (
                "SYSTEM PROMPT (highest priority; governs this whole "
                "conversation and every reply; do not quote it back):\n\n"
                f"{system_prompt}\n\n"
                "END OF SYSTEM PROMPT\n\n"
                "Now answer the user's message below.\n\n"
                f"{message['content']}"
            )
            return

    def _system_prompt(
        self,
        history: list[dict[str, Any]],
        requested_language: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Build the system prompt exactly as the chat request will send it."""
        if requested_language is None:
            requested_language = detect_requested_language(history)
        return build_system_prompt(
            self._settings,
            self._knowledge.read(),
            requested_language,
            now=now,
        )

    def _build_messages(
        self,
        history: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        requested_language = detect_requested_language(history)
        system_prompt = self._system_prompt(history, requested_language, now=now)
        fold_system = self._system_prompt_in_user_message()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        window = self._context_window(history)
        logger.debug(
            "Sending %d of %d stored messages to the model (system excluded)",
            len(window),
            len(history),
        )

        for item in window:
            content = item.get("content") or ""
            if item["role"] == "tool" and item.get("name") == "system_diagnosis":
                # The UI needs chart/report paths, but the model does not: the
                # visuals are already rendered natively in the chat. Hide the
                # artifact paths so the answer does not paste local file links.
                content = self._model_facing_diagnosis(content)
            message: dict[str, Any] = {
                "role": item["role"],
                "content": content,
            }
            if item.get("tool_calls"):
                message["tool_calls"] = item["tool_calls"]
            if item.get("tool_call_id"):
                message["tool_call_id"] = item["tool_call_id"]
            if item.get("name"):
                message["name"] = item["name"]
            messages.append(message)

        if requested_language:
            for message in reversed(messages):
                if message["role"] == "user":
                    message["content"] = (
                        f"{message['content']}\n\n"
                        f"[Reply entirely in {requested_language}.]"
                    )
                    break

        # The clock is volatile, so it belongs in the newest user turn, not in
        # the system prompt. This keeps the system prompt and tool schemas
        # byte-identical across requests so Ollama can reuse its prompt cache.
        clock = format_clock_line(now)
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"] = f"{clock}\n\n{message['content']}"
                break

        if fold_system:
            self._fold_system_prompt(messages, system_prompt)

        return messages

    @staticmethod
    def _context_window(
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return the latest messages that fit the sliding context window.

        The window is capped at CONTEXT_WINDOW_MESSAGES and an approximate
        character budget. The current user request is always kept, even when a
        long tool loop pushes it out of the recent-message window. The selected
        slice starts at a complete tool-call boundary so the model never sees an
        orphaned tool result or a tool call whose result was cut off.
        """
        if not history:
            return []

        if len(history) <= CONTEXT_WINDOW_MESSAGES:
            window = list(history)
        else:
            start = len(history) - CONTEXT_WINDOW_MESSAGES
            # A user message inside the recent slice is a natural turn
            # boundary; start there instead of with the previous answer.
            conversation_start = next(
                (
                    index
                    for index in range(start, len(history))
                    if history[index].get("role") == "user"
                ),
                None,
            )
            if conversation_start is not None:
                start = conversation_start
                window = list(history[start:])
            else:
                start = ChatService._align_window_start(history, start)
                window = list(history[start:])
                last_user = next(
                    (
                        index
                        for index in range(len(history) - 1, -1, -1)
                        if history[index].get("role") == "user"
                    ),
                    None,
                )
                if last_user is not None and last_user < start:
                    window = [history[last_user]] + window

        window = ChatService._trim_incomplete_tool_prefix(window)
        return ChatService._fit_context_budget(window)

    @staticmethod
    def _align_window_start(
        history: list[dict[str, Any]],
        start: int,
    ) -> int:
        """Move a window start back so it includes the tool call for a result."""
        while 0 < start < len(history) and history[start].get("role") == "tool":
            call_id = history[start].get("tool_call_id")
            for index in range(start - 1, -1, -1):
                message = history[index]
                if message.get("role") != "assistant":
                    continue
                call_ids = {
                    call.get("id")
                    for call in message.get("tool_calls") or []
                    if isinstance(call, dict)
                }
                if call_id in call_ids:
                    start = index
                    break
            else:
                # No matching call: skip the orphaned result.
                start += 1
                continue
            if start == len(history):
                break
        return start

    @staticmethod
    def _fit_context_budget(
        window: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep the current request and trim old steps to fit the char budget."""
        if not window:
            return []

        total = sum(
            len(str(message.get("content") or "")) for message in window
        )
        if total <= CONTEXT_WINDOW_CHARS:
            return window

        has_user_head = window[0].get("role") == "user"
        head = [window[0]] if has_user_head else []
        rest = ChatService._trim_incomplete_tool_prefix(
            window[1:] if has_user_head else list(window)
        )
        used = len(str(head[0].get("content") or "")) if head else 0
        kept: list[dict[str, Any]] = []

        for message in reversed(rest):
            cost = len(str(message.get("content") or "")) + 80
            if kept and used + cost > CONTEXT_WINDOW_CHARS:
                break
            kept.append(message)
            used += cost

        trimmed = ChatService._trim_incomplete_tool_prefix(
            list(reversed(kept))
        )
        return head + trimmed

    @staticmethod
    def _trim_incomplete_tool_prefix(
        window: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop leading tool messages and tool calls without all their results."""
        while window:
            first = window[0]
            role = first.get("role")

            if role == "tool":
                window.pop(0)
                continue

            if role == "assistant" and first.get("tool_calls"):
                call_ids = {
                    call.get("id")
                    for call in first["tool_calls"]
                    if call.get("id")
                }
                result_ids = {
                    message.get("tool_call_id")
                    for message in window[1:]
                    if message.get("role") == "tool"
                }
                if call_ids and not call_ids.issubset(result_ids):
                    window.pop(0)
                    continue

            break

        return window
