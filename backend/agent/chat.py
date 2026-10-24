"""Ollama-backed agent with HITL tool approvals and streaming events."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
    OpenAI,
    OpenAIError,
)

from agent.approvals import (
    ApprovalError,
    ApprovalNotFound,
    PendingAction,
    PendingActionStore,
)
from agent.language import detect_requested_language
from agent.learning import extract_facts, infer_deletions
from agent.prompt import build_system_prompt
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings
from tools.executor import ToolExecutor
from tools.schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

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


def _tool_status_message(name: str, arguments: dict[str, Any]) -> str:
    if name == "start_program":
        return f"Opening {arguments.get('program_name', 'an application')}..."
    if name == "create_folder":
        return f"Creating folder {arguments.get('path', '')}..."
    if name == "create_file":
        return f"Creating file {arguments.get('path', '')}..."
    if name == "copy_file":
        return (
            f"Copying {arguments.get('source_path', '')} to "
            f"{arguments.get('destination_path', '')}..."
        )
    if name == "move_file_or_folder":
        return (
            f"Moving {arguments.get('source_path', '')} to "
            f"{arguments.get('destination_path', '')}..."
        )
    if name == "delete_file_or_folder":
        return f"Moving {arguments.get('path', '')} to the Recycle Bin..."
    if name == "unzip_file":
        return f"Extracting {arguments.get('archive_path', '')}..."
    if name == "read_file_content":
        return f"Reading {arguments.get('path', '')}..."
    if name == "search_system":
        return f"Searching {arguments.get('root', 'the file system')}..."
    return f"Running {name}..."


def _action_description(name: str, arguments: dict[str, Any]) -> str:
    if name == "start_program":
        return f"Open {arguments.get('program_name', 'an application')}"
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
        self._client = client or OpenAI(
            base_url=settings.ollama.base_url,
            api_key="ollama",
            timeout=settings.ollama.timeout_seconds,
            max_retries=settings.ollama.max_retries,
        )
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
                yield from self._run_segment(conversation_id)
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001
                yield self._error_event(conversation_id, exc)

    def _run_segment(self, conversation_id: str) -> Iterator[dict[str, Any]]:
        max_tool_rounds = self._settings.agent.max_iterations

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
                    *learned.deletions,
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
        tool_choice: str = "auto",
    ) -> Any:
        model = self._settings.ollama.model
        messages = self._build_messages(history)

        if nudge:
            messages.append(
                {
                    "role": "system",
                    "content": (
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

    def _build_messages(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requested_language = detect_requested_language(history)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(
                    self._settings,
                    self._knowledge.read(),
                    requested_language,
                ),
            }
        ]

        for item in history:
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

        return messages
