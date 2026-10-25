"""SSE protocol tests for streaming status and HITL approvals."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from agent.chat import ChatService
from main import create_app
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import AgentSettings, MemorySettings, OllamaSettings, Settings
from tools.executor import ToolExecutor


class ScriptedCompletions:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = list(messages)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        message = self.messages.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedClient:
    def __init__(self, messages: list[Any]) -> None:
        self.completions = ScriptedCompletions(messages)
        self.chat = SimpleNamespace(completions=self.completions)


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def tool_message(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> Any:
    return SimpleNamespace(
        content="",
        tool_calls=[tool_call(name, arguments, call_id)],
    )


def content_message(content: str) -> Any:
    return SimpleNamespace(content=content, tool_calls=None)


def parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.strip().split("\n\n"):
        data_lines = [
            line[len("data:") :].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def build_app(tmp_path: Path, messages: list[Any]) -> tuple[TestClient, ConversationStore]:
    settings = Settings(
        ollama=OllamaSettings(model="hermes3", temperature=0.2, max_retries=0),
        agent=AgentSettings(max_iterations=3),
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=False,
        ),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    executor = ToolExecutor(settings.tools)
    client = ScriptedClient(messages)
    service = ChatService(settings, store, knowledge, executor, client=client)
    app = create_app(settings=settings, store=store, chat_service=service)
    return TestClient(app), store


def build_display_app(
    tmp_path: Path,
    messages: list[Any],
) -> tuple[TestClient, Any]:
    class DisplayVision:
        is_loaded = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None]] = []

        def display_choices(self) -> list[dict[str, Any]]:
            return [
                {
                    "index": 1,
                    "name": "Display 1",
                    "width": 1920,
                    "height": 1080,
                    "left": -1920,
                    "top": 325,
                    "primary": False,
                    "position": "Left of the primary display",
                    "preview": "data:image/jpeg;base64,AAAA",
                },
                {
                    "index": 2,
                    "name": "Display 2",
                    "width": 3840,
                    "height": 2160,
                    "left": 0,
                    "top": 0,
                    "primary": True,
                    "position": "Primary display",
                    "preview": "data:image/jpeg;base64,BBBB",
                },
            ]

        def analyze_screen(
            self,
            user_question: str,
            display: int | None = None,
            *,
            system_prompt: str | None = None,
            tools: Any = None,
        ) -> dict[str, Any]:
            self.calls.append((user_question, display))
            return {
                "ok": True,
                "question": user_question,
                "answer": f"Display {display} shows a browser.",
                "display": {"index": display},
                "screen": {"width": 1600, "height": 900},
                "message": f"Analyzed Display {display}.",
            }

    settings = Settings(
        ollama=OllamaSettings(model="hermes3", temperature=0.2, max_retries=0),
        agent=AgentSettings(max_iterations=3),
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=False,
        ),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    vision = DisplayVision()
    executor = ToolExecutor(settings.tools, vision=vision)  # type: ignore[arg-type]
    client = ScriptedClient(messages)
    service = ChatService(settings, store, knowledge, executor, client=client)
    app = create_app(settings=settings, store=store, chat_service=service)
    return TestClient(app), vision


def test_display_required_then_chosen_display(tmp_path: Path) -> None:
    client, vision = build_display_app(
        tmp_path,
        [
            tool_message(
                "analyze_screen",
                {"user_question": "What is on my screen?"},
            ),
            content_message("Display 2 shows a browser."),
        ],
    )
    conversation_id = str(uuid4())

    with client:
        response = client.post(
            "/chat/stream",
            json={
                "message": "What is on my screen?",
                "conversation_id": conversation_id,
            },
        )
        events = parse_sse(response.text)
        choice = next(
            event for event in events if event["type"] == "display_required"
        )
        assert [item["index"] for item in choice["displays"]] == [1, 2]
        assert vision.calls == []

        pending = client.get(f"/chat/pending/{conversation_id}")
        assert pending.json()["pending"]["kind"] == "display"

        resumed = client.post(
            "/chat/display",
            json={"approval_id": choice["approval_id"], "display": 2},
        )
        assert resumed.status_code == 200
        resumed_events = parse_sse(resumed.text)
        assert any(
            event["type"] == "tool_result"
            and event["tool"] == "analyze_screen"
            and event["ok"]
            for event in resumed_events
        )
        final = next(
            event for event in resumed_events if event["type"] == "final"
        )
        assert final["reply"] == "Display 2 shows a browser."
        assert vision.calls == [("What is on my screen?", 2)]

        cleared = client.get(f"/chat/pending/{conversation_id}")
        assert cleared.json()["pending"] is None


def test_display_choice_rejects_an_unknown_number(tmp_path: Path) -> None:
    client, _vision = build_display_app(
        tmp_path,
        [
            tool_message(
                "analyze_screen",
                {"user_question": "What is on my screen?"},
            ),
            content_message("Done."),
        ],
    )
    conversation_id = str(uuid4())

    with client:
        response = client.post(
            "/chat/stream",
            json={
                "message": "What is on my screen?",
                "conversation_id": conversation_id,
            },
        )
        choice = next(
            event
            for event in parse_sse(response.text)
            if event["type"] == "display_required"
        )

        invalid = client.post(
            "/chat/display",
            json={"approval_id": choice["approval_id"], "display": 9},
        )
        assert invalid.status_code == 400

        still_pending = client.get(f"/chat/pending/{conversation_id}")
        assert still_pending.json()["pending"] is not None


def test_stream_action_required_then_approve(tmp_path: Path) -> None:
    target = tmp_path / "stream-approved"
    client, _store = build_app(
        tmp_path,
        [
            tool_message("create_folder", {"path": str(target)}),
            content_message("Created it."),
        ],
    )
    conversation_id = str(uuid4())

    with client:
        response = client.post(
            "/chat/stream",
            json={"message": "Create a folder.", "conversation_id": conversation_id},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["X-Conversation-ID"] == conversation_id
        events = parse_sse(response.text)
        action = next(event for event in events if event["type"] == "action_required")
        assert action["actions"][0]["tool"] == "create_folder"
        assert not target.exists()

        pending = client.get(f"/chat/pending/{conversation_id}")
        assert pending.status_code == 200
        assert pending.json()["pending"]["approval_id"] == action["approval_id"]

        resumed = client.post(
            "/chat/approve",
            json={"approval_id": action["approval_id"], "approved": True},
        )
        assert resumed.status_code == 200
        resumed_events = parse_sse(resumed.text)
        final = next(event for event in resumed_events if event["type"] == "final")
        assert final["reply"] == "Created it."
        assert target.is_dir()

        cleared = client.get(f"/chat/pending/{conversation_id}")
        assert cleared.json()["pending"] is None


def test_stream_read_only_tool_reports_steps(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    client, _store = build_app(
        tmp_path,
        [
            tool_message("read_file_content", {"path": str(note)}),
            content_message("It says hello."),
        ],
    )

    with client:
        response = client.post(
            "/chat/stream",
            json={"message": "Read my note.", "conversation_id": str(uuid4())},
        )

        events = parse_sse(response.text)
        types = [event["type"] for event in events]
        assert "action_required" not in types
        assert "tool_result" in types
        assert any(
            event["type"] == "status" and event["phase"] == "tool_started"
            for event in events
        )
        final = next(event for event in events if event["type"] == "final")
        assert final["reply"] == "It says hello."


def test_deny_streams_cancellation(tmp_path: Path) -> None:
    target = tmp_path / "stream-denied"
    client, _store = build_app(
        tmp_path,
        [
            tool_message("create_folder", {"path": str(target)}),
            content_message("Cancelled."),
        ],
    )
    conversation_id = str(uuid4())

    with client:
        response = client.post(
            "/chat/stream",
            json={"message": "Create a folder.", "conversation_id": conversation_id},
        )
        action = next(
            event
            for event in parse_sse(response.text)
            if event["type"] == "action_required"
        )

        resumed = client.post(
            "/chat/approve",
            json={"approval_id": action["approval_id"], "approved": False},
        )
        events = parse_sse(resumed.text)
        final = next(event for event in events if event["type"] == "final")
        assert final["reply"] == "Cancelled."
        assert not target.exists()
        tool_result = next(
            event for event in events if event["type"] == "tool_result"
        )
        assert tool_result["ok"] is False
        assert tool_result["summary"] == "Action cancelled by user."


def test_double_approve_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "double"
    client, _store = build_app(
        tmp_path,
        [
            tool_message("create_folder", {"path": str(target)}),
            content_message("Done."),
        ],
    )
    with client:
        response = client.post(
            "/chat/stream",
            json={"message": "Create a folder.", "conversation_id": str(uuid4())},
        )
        action = next(
            event
            for event in parse_sse(response.text)
            if event["type"] == "action_required"
        )

        first = client.post(
            "/chat/approve",
            json={"approval_id": action["approval_id"], "approved": True},
        )
        assert first.status_code == 200

        second = client.post(
            "/chat/approve",
            json={"approval_id": action["approval_id"], "approved": True},
        )
        assert second.status_code == 404
