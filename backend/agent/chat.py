"""Ollama-backed chat service with in-memory context and long-term knowledge."""

from __future__ import annotations

import logging
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
    OpenAI,
    OpenAIError,
)

from agent.learning import extract_facts, infer_deletions
from agent.language import detect_requested_language
from agent.prompt import build_system_prompt
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A user-facing LLM failure with an HTTP status code."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatService:
    """Compose prompts, call Ollama, keep chat history, and learn about the user."""

    def __init__(
        self,
        settings: Settings,
        store: ConversationStore,
        knowledge: KnowledgeBase,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._knowledge = knowledge
        self._client = client or OpenAI(
            base_url=settings.ollama.base_url,
            api_key="ollama",
            timeout=settings.ollama.timeout_seconds,
            max_retries=settings.ollama.max_retries,
        )

    def generate_reply(self, conversation_id: str, message: str) -> str:
        """Add the user turn, ask Hermes 3, and store the assistant reply."""
        self._store.append(conversation_id, "user", message)

        try:
            reply = self._request_reply(self._store.load(conversation_id))
        except Exception:
            # Roll back so a failed turn leaves no dangling user message.
            self._store.pop_last(conversation_id)
            raise

        self._store.append(conversation_id, "assistant", reply)
        return reply

    def learn_from_exchange(self, user_message: str) -> list[tuple[str, str]]:
        """Extract durable facts from the user's message and store them.

        Facts are written into the matching knowledge-base sections. Runs after
        the reply has been returned; failures are logged and never propagate to
        the chat request.
        """
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

    def _request_reply(self, history: list[dict[str, Any]]) -> str:
        model = self._settings.ollama.model

        try:
            completion = self._client.chat.completions.create(
                model=model,
                messages=self._build_messages(history),
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

        return self._extract_content(completion)

    def _build_messages(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        requested_language = detect_requested_language(history)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": build_system_prompt(
                    self._settings,
                    self._knowledge.read(),
                    requested_language,
                ),
            }
        ]
        messages.extend(
            {"role": item["role"], "content": item["content"]} for item in history
        )

        # Reinforce a requested language at the newest user turn. This keeps the
        # instruction close to the message that would otherwise dominate.
        if requested_language and messages:
            last = messages[-1]
            messages[-1] = {
                "role": last["role"],
                "content": (
                    f"{last['content']}\n\n"
                    f"[Reply entirely in {requested_language}.]"
                ),
            }

        return messages

    @staticmethod
    def _extract_content(completion: Any) -> str:
        choices = getattr(completion, "choices", None)
        if not choices:
            raise LLMError("Ollama returned an empty response.")

        content = getattr(choices[0].message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Ollama returned an empty response.")

        return content.strip()
