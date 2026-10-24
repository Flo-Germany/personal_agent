"""In-memory conversation history.

Memory is strictly process-local: it is never written to disk and is lost when
the backend restarts or the conversation is deleted. Every message in a
conversation is kept, so the model can recall the whole chat.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

VALID_ROLES = {"user", "assistant"}


class ConversationStore:
    """Keep complete chat history per conversation in memory."""

    def __init__(self, max_conversations: int = 100) -> None:
        if max_conversations < 1:
            raise ValueError("max_conversations must be at least 1")

        self.max_conversations = max_conversations
        # Insertion order doubles as LRU order: the least recently used
        # conversation is evicted first once max_conversations is exceeded.
        self._conversations: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def load(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return a copy of all stored messages for a conversation."""
        key = self._key(conversation_id)

        with self._lock:
            messages = self._conversations.get(key)
            if messages is None:
                return []

            self._conversations.move_to_end(key)
            return [dict(message) for message in messages]

    def append(self, conversation_id: str, role: str, content: str) -> None:
        """Append one message without discarding earlier ones."""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid message role: {role}")

        key = self._key(conversation_id)
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            messages = self._conversations.setdefault(key, [])
            self._conversations.move_to_end(key)
            messages.append({"role": role, "content": content, "created_at": now})

            while len(self._conversations) > self.max_conversations:
                self._conversations.popitem(last=False)

    def append_exchange(
        self,
        conversation_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Append a completed user/assistant turn."""
        self.append(conversation_id, "user", user_content)
        self.append(conversation_id, "assistant", assistant_content)

    def pop_last(self, conversation_id: str) -> dict[str, Any] | None:
        """Remove and return the newest message, if any.

        Used to roll back a user message when the model call fails so a failed
        turn never pollutes the conversation.
        """
        key = self._key(conversation_id)

        with self._lock:
            messages = self._conversations.get(key)
            if not messages:
                return None

            message = messages.pop()
            if not messages:
                self._conversations.pop(key, None)
            return message

    def clear(self, conversation_id: str) -> bool:
        """Drop one conversation. Returns whether it existed."""
        key = self._key(conversation_id)

        with self._lock:
            return self._conversations.pop(key, None) is not None

    def clear_all(self) -> None:
        """Drop every conversation (used by tests and a fresh start)."""
        with self._lock:
            self._conversations.clear()

    @property
    def conversation_count(self) -> int:
        with self._lock:
            return len(self._conversations)

    @staticmethod
    def _key(conversation_id: str) -> str:
        try:
            return str(uuid.UUID(str(conversation_id)))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid conversation id") from exc
