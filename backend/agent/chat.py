"""Ollama-backed agent with HITL tool approvals and streaming events."""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import uuid
from pathlib import Path
from typing import Any, Iterator

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
    OpenAIError,
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
from agent.prompt import build_system_prompt
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings
from tools.executor import ToolExecutor
from tools.schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

CONTEXT_WINDOW_MESSAGES = 10

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
        self._tools = tools or ToolExecutor(settings.tools)
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

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

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

            start_length = len(self._store.load(conversation_id))
            self._store.append(conversation_id, "user", message)

            try:
                yield from self._run_segment(conversation_id)
            except GeneratorExit:
                if self._pending.get_for_conversation(conversation_id) is None:
                    self._store.truncate(conversation_id, start_length)
                raise
            except Exception as exc:  # noqa: BLE001 - converted into an SSE error
                if self._pending.get_for_conversation(conversation_id) is None:
                    self._store.truncate(conversation_id, start_length)
                yield self._error_event(conversation_id, exc)

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
            results: list[tuple[dict[str, Any], dict[str, Any]]] = []

            # Execute before yielding anything so an approved action is not lost
            # if the client disconnects immediately after clicking Approve.
            for call in pending.calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                if approved:
                    result = self._tools.execute(name, arguments)
                else:
                    result = {
                        "ok": False,
                        "cancelled": True,
                        "error": "Action cancelled by user.",
                    }
                self._store.append(
                    conversation_id,
                    "tool",
                    json.dumps(result, ensure_ascii=False, default=str),
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

            for call, result in results:
                yield self._tool_result_event(
                    conversation_id,
                    call["function"]["name"],
                    result,
                )

            try:
                failed_powershell_action = any(
                    call["function"]["name"] == "run_powershell_action"
                    and not result.get("ok")
                    and not result.get("cancelled")
                    for call, result in results
                )
                yield from self._run_segment(
                    conversation_id,
                    recover_failed_action=failed_powershell_action,
                )
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001
                yield self._error_event(conversation_id, exc)

    def _run_segment(
        self,
        conversation_id: str,
        *,
        recover_failed_action: bool = False,
    ) -> Iterator[dict[str, Any]]:
        max_tool_rounds = self._settings.agent.max_iterations
        screen_forced = False
        recovery_attempted = False

        for round_index in range(max_tool_rounds + 1):
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

            # Hermes occasionally emits an empty message on complex prompts.
            # Retrying once is safe because no tool has run yet.
            for attempt in range(2):
                completion = self._create_completion(
                    self._store.load(conversation_id),
                    nudge=attempt > 0,
                    tool_choice="required" if attempt > 0 else "auto",
                )
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

            # Some Hermes/Ollama combinations emit the tool call as JSON text
            # instead of a structured tool_calls item. Execute it, but never
            # forward the raw JSON to the user.
            text_calls: list[dict[str, Any]] = (
                [] if tool_calls else self._extract_text_tool_calls(content)
            )
            if text_calls:
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
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "thinking",
                    "message": "Looking at the screen...",
                }
                try:
                    forced = self._create_completion(
                        self._store.load(conversation_id),
                        nudge=True,
                        nudge_text=_SCREEN_NUDGE,
                        tool_choice="required",
                    )
                    forced_choices = getattr(forced, "choices", None)
                    if forced_choices:
                        forced_message = forced_choices[0].message
                        forced_calls = getattr(forced_message, "tool_calls", None) or []
                        if forced_calls:
                            tool_calls = forced_calls
                            content = (
                                getattr(forced_message, "content", "") or ""
                            )
                            text_calls = []
                except LLMError as exc:
                    logger.warning("Forced screen analysis failed: %s", exc)

            # Hermes sometimes narrates the next action ("Next, I will...")
            # or emits malformed JSON instead of calling the tool. Since
            # modifying tools are still gated by HITL, ask it to choose a tool.
            needs_forced_tool = (
                self._looks_like_action_promise(content)
                or self._looks_like_malformed_tool_output(content)
            )
            if (
                not tool_calls
                and not text_calls
                and content.strip()
                and needs_forced_tool
                and self._last_user_requested_action(conversation_id)
            ):
                for _forced_attempt in range(2):
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
                        forced = self._create_completion(
                            self._store.load(conversation_id),
                            nudge=True,
                            tool_choice="required",
                        )
                        forced_choices = getattr(forced, "choices", None)
                        if not forced_choices:
                            break
                        forced_message = forced_choices[0].message
                        forced_calls = getattr(forced_message, "tool_calls", None) or []
                        forced_content = getattr(forced_message, "content", "") or ""
                        if forced_calls:
                            tool_calls = forced_calls
                            content = forced_content
                            text_calls = []
                            break
                        forced_text_calls = self._extract_text_tool_calls(
                            forced_content
                        )
                        if forced_text_calls:
                            text_calls = forced_text_calls
                            content = ""
                            break
                        if forced_content.strip():
                            content = forced_content
                        if not self._looks_like_action_promise(content):
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
                    recovered = self._create_completion(
                        self._store.load(conversation_id),
                        nudge=True,
                        nudge_text=_ACTION_FAILURE_NUDGE,
                        tool_choice="required",
                    )
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
                        else:
                            recovered_text_calls = self._extract_text_tool_calls(
                                recovered_content
                            )
                            if recovered_text_calls:
                                text_calls = recovered_text_calls
                                content = ""
                            elif recovered_content.strip():
                                content = recovered_content
                except LLMError as exc:
                    logger.warning("Failed-action recovery attempt failed: %s", exc)

            if not tool_calls and not text_calls:
                if not content.strip():
                    raise LLMError("Ollama returned an empty response.")
                if self._looks_like_raw_tool_call(
                    content
                ) or self._looks_like_malformed_tool_output(content):
                    raise LLMError(
                        "The model returned a malformed response. Please try again."
                    )
                reply = content.strip()
                self._store.append(conversation_id, "assistant", reply)
                yield {
                    "type": "final",
                    "conversation_id": conversation_id,
                    "reply": reply,
                }
                return

            if round_index >= max_tool_rounds:
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
            self._store.append(
                conversation_id,
                "assistant",
                content,
                tool_calls=serialized_calls,
            )

            if content.strip():
                yield {
                    "type": "assistant",
                    "conversation_id": conversation_id,
                    "content": content.strip(),
                }

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
                self._store.append(conversation_id, "assistant", reply)
                yield {
                    "type": "final",
                    "conversation_id": conversation_id,
                    "reply": reply,
                }
                return

            for call in read_only_calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                parsed = _parse_arguments(call)
                yield {
                    "type": "status",
                    "conversation_id": conversation_id,
                    "phase": "tool_started",
                    "tool": name,
                    "arguments": parsed,
                    "message": _tool_status_message(name, parsed),
                }
                result = self._tools.execute(name, arguments)
                self._store.append(
                    conversation_id,
                    "tool",
                    json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=call["id"],
                    name=name,
                )
                yield self._tool_result_event(conversation_id, name, result)

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
                pending = self._pending.create(
                    conversation_id,
                    self._last_user_message(conversation_id),
                    confirmation_calls,
                    actions,
                    assistant_message=content.strip(),
                )
                yield pending.to_public()
                return

        raise LLMError(
            "The agent exceeded the maximum number of tool steps.",
            status_code=502,
        )

    def generate_reply(self, conversation_id: str, message: str) -> str:
        """Legacy non-streaming helper.

        Simple tool-free chats work as before. If a modifying tool is required,
        this raises ``ConfirmationRequired`` so the caller can return 409.
        """
        events = list(self.start_turn(conversation_id, message))

        for event in events:
            if event["type"] == "action_required":
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
    def _create_completion(
        self,
        history: list[dict[str, Any]],
        *,
        nudge: bool = False,
        nudge_text: str | None = None,
        tool_choice: str = "auto",
    ) -> Any:
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

        try:
            return self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice=tool_choice,
                temperature=self._settings.ollama.temperature,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LLMError(
                    f"Ollama model '{model}' is not installed. "
                    f"Run `ollama pull {model}`.",
                    status_code=503,
                ) from exc
            raise LLMError(
                f"Ollama returned an error (HTTP {exc.code}).",
                status_code=502,
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(
                exc, TimeoutError
            ):
                raise LLMError(
                    "Ollama took too long to respond. Try a shorter message.",
                    status_code=504,
                ) from exc
            raise LLMError(
                "I can't reach Ollama at "
                f"{self._settings.ollama.base_url}. Make sure Ollama is running "
                "(`ollama serve`).",
                status_code=503,
            ) from exc
        except APITimeoutError as exc:
            raise LLMError(
                "Ollama took too long to respond. Try a shorter message.",
                status_code=504,
            ) from exc
        except APIConnectionError as exc:
            raise LLMError(
                "I can't reach Ollama at "
                f"{self._settings.ollama.base_url}. Make sure Ollama is running "
                "(`ollama serve`).",
                status_code=503,
            ) from exc
        except NotFoundError as exc:
            raise LLMError(
                f"Ollama model '{model}' is not installed. "
                f"Run `ollama pull {model}`.",
                status_code=503,
            ) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"Ollama returned an error (HTTP {exc.status_code}).",
                status_code=502,
            ) from exc
        except OpenAIError as exc:
            raise LLMError("The LLM request failed.", status_code=502) from exc

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
        return bool(
            re.search(
                r"\b(?:i will|i'll|i am going to|i'm going to|next,?\s+i|"
                r"let me|i can now|i will now)\b",
                text,
                re.IGNORECASE,
            )
        )

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

    def _build_messages(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requested_language = detect_requested_language(history)
        system_prompt = build_system_prompt(
            self._settings,
            self._knowledge.read(),
            requested_language,
        )
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
            message: dict[str, Any] = {
                "role": item["role"],
                "content": item.get("content") or "",
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

        if fold_system:
            self._fold_system_prompt(messages, system_prompt)

        return messages

    @staticmethod
    def _context_window(
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return the latest messages that fit the sliding context window.

        The window is capped at CONTEXT_WINDOW_MESSAGES. It is then advanced to
        the next user message so the model never receives an orphaned tool
        result or a tool call whose result was cut off. If a long tool loop
        leaves no user message in the window, the current turn is kept intact.
        """
        if len(history) <= CONTEXT_WINDOW_MESSAGES:
            window = list(history)
        else:
            window = list(history[-CONTEXT_WINDOW_MESSAGES:])
            first_user = next(
                (
                    index
                    for index, message in enumerate(window)
                    if message.get("role") == "user"
                ),
                None,
            )
            if first_user is not None:
                window = window[first_user:]
            else:
                last_user = next(
                    (
                        index
                        for index in range(len(history) - 1, -1, -1)
                        if history[index].get("role") == "user"
                    ),
                    None,
                )
                if last_user is not None:
                    window = list(history[last_user:])

        return ChatService._trim_incomplete_tool_prefix(window)

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
