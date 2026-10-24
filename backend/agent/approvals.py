"""Pending Human-in-the-Loop approvals for modifying tool calls."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class ApprovalError(RuntimeError):
    """Base class for approval-state errors."""


class ApprovalNotFound(ApprovalError):
    """No pending action exists for this id or conversation."""


class ApprovalExpired(ApprovalError):
    """The pending action outlived its TTL."""


class PendingActionExists(ApprovalError):
    """A conversation already has an unresolved pending action."""


@dataclass
class PendingAction:
    approval_id: str
    conversation_id: str
    user_message: str
    calls: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    assistant_message: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_public(self) -> dict[str, Any]:
        return {
            "type": "action_required",
            "approval_id": self.approval_id,
            "conversation_id": self.conversation_id,
            "actions": self.actions,
            "message": self.assistant_message,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
        }


class PendingActionStore:
    """Thread-safe, in-memory store with one pending action per conversation."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._by_id: dict[str, PendingAction] = {}
        self._by_conversation: dict[str, str] = {}

    def _cleanup(self) -> None:
        now = time.time()
        expired = [
            approval_id
            for approval_id, pending in self._by_id.items()
            if pending.expires_at <= now
        ]
        for approval_id in expired:
            pending = self._by_id.pop(approval_id, None)
            if pending is not None:
                self._by_conversation.pop(pending.conversation_id, None)

    def create(
        self,
        conversation_id: str,
        user_message: str,
        calls: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        assistant_message: str = "",
    ) -> PendingAction:
        with self._lock:
            self._cleanup()
            if conversation_id in self._by_conversation:
                raise PendingActionExists(
                    "This conversation already has a pending action."
                )

            approval_id = f"apr_{uuid.uuid4().hex}"
            now = time.time()
            pending = PendingAction(
                approval_id=approval_id,
                conversation_id=conversation_id,
                user_message=user_message,
                calls=calls,
                actions=actions,
                assistant_message=assistant_message,
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._by_id[approval_id] = pending
            self._by_conversation[conversation_id] = approval_id
            return pending

    def get(self, approval_id: str) -> PendingAction:
        with self._lock:
            self._cleanup()
            pending = self._by_id.get(approval_id)
            if pending is None:
                raise ApprovalNotFound("Approval request not found or expired.")
            return pending

    def get_for_conversation(self, conversation_id: str) -> PendingAction | None:
        with self._lock:
            self._cleanup()
            approval_id = self._by_conversation.get(conversation_id)
            if approval_id is None:
                return None
            return self._by_id.get(approval_id)

    def consume(self, approval_id: str) -> PendingAction:
        with self._lock:
            self._cleanup()
            pending = self._by_id.pop(approval_id, None)
            if pending is None:
                raise ApprovalNotFound("Approval request not found or already used.")
            self._by_conversation.pop(pending.conversation_id, None)
            return pending

    def discard(self, conversation_id: str) -> bool:
        with self._lock:
            approval_id = self._by_conversation.pop(conversation_id, None)
            if approval_id is None:
                return False
            self._by_id.pop(approval_id, None)
            return True
