"""Tests for the visible thinking phase and its speed controls."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.chat import ChatService, _THINKING_HINT, _SMALL_TALK
from agent.ollama_client import NativeOllamaClient
from main import create_app
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import MemorySettings, OllamaSettings, Settings, ToolSettings


def make_settings(tmp_path: Path, **ollama: Any) -> Settings:
    return Settings(
        agent_name="TestBot",
        personality="Curious and kind.",
        system_prompt="Answer in one short paragraph.",
        personality_file=tmp_path / "personality.md",
        ollama=OllamaSettings(
            model="qwen3-vl:8b",
            temperature=0.2,
            max_retries=0,
            **ollama,
        ),
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
        tools=ToolSettings(sandbox_dir=tmp_path / "sandbox"),
    )


class ThinkingClient:
    """Streams a thinking phase, then the answer."""

    def __init__(
        self,
        thinking: list[str],
        content: list[str],
        *,
        supports: bool = True,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.thinking = thinking
        self.content = content
        self.supports = supports
        self.tool_calls = tool_calls or []
        self.requests: list[dict[str, Any]] = []

    def supports_thinking(self, model: str) -> bool:
        return self.supports

    def stream_chat(self, **kwargs: Any):
        self.requests.append(kwargs)
        for part in self.thinking:
            yield {"message": {"role": "assistant", "thinking": part}}
        for part in self.content:
            yield {"message": {"role": "assistant", "content": part}}
        for call in self.tool_calls:
            yield {"message": {"role": "assistant", "tool_calls": [call]}}
        yield {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": len(self.content) + len(self.thinking),
            "eval_duration": 1_000_000_000,
            "load_duration": 0,
        }


def make_service(tmp_path: Path, client: Any, **ollama: Any) -> tuple[
    ChatService, ConversationStore
]:
    settings = make_settings(tmp_path, **ollama)
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    return ChatService(settings, store, knowledge, client=client), store


def test_thinking_streams_before_the_answer_and_is_stored(tmp_path: Path) -> None:
    client = ThinkingClient(["First I count. ", "17 * 23 = 391."], ["391"])
    service, store = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "What is 17 * 23?"))

    types = [event["type"] for event in events]
    assert types.count("assistant_thinking") == 2
    thinking_index = types.index("assistant_thinking")
    delta_index = types.index("assistant_delta")
    assert thinking_index < delta_index

    final = events[-1]
    assert final["type"] == "final"
    assert final["reply"] == "391"
    assert final["thinking"] == "First I count. 17 * 23 = 391."
    assert final["thinking_tokens"] == 2
    assert final["thinking_ms"] is not None

    stored = store.load(conversation_id)
    assistant = [item for item in stored if item["role"] == "assistant"][-1]
    assert assistant["thinking"] == "First I count. 17 * 23 = 391."
    assert assistant["thinking_ms"] == final["thinking_ms"]

    # Reasoning is UI-only: it must never be replayed to the model.
    sent = service._build_messages(stored)
    assert all("thinking" not in message for message in sent)


def test_metrics_include_the_thinking_phase(tmp_path: Path) -> None:
    client = ThinkingClient(["Think."], ["Done."])
    service, store = make_service(tmp_path, client)

    events = list(
        service.start_turn(store.new_conversation_id(), "Explain the trade-offs.")
    )

    metrics = [
        event
        for event in events
        if event["type"] == "metrics" and not event.get("live")
    ]
    assert metrics, events
    assert metrics[-1]["thinking_ms"] is not None
    assert metrics[-1]["thinking_tokens"] == 1


def test_thinking_is_skipped_for_small_talk_but_used_for_plans(
    tmp_path: Path,
) -> None:
    greeting = ThinkingClient(["unused"], ["Hi!"])

    service, store = make_service(tmp_path, greeting)

    list(service.start_turn(store.new_conversation_id(), "Hi!"))
    assert greeting.requests[0]["think"] is False

    plan = ThinkingClient(["unused"], ["Step one."])

    service, store = make_service(tmp_path, plan)

    list(
        service.start_turn(
            store.new_conversation_id(),
            "How should I plan the migration?",
        )
    )
    assert plan.requests[0]["think"] is True


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_explicit_thinking_budgets_are_forwarded(tmp_path: Path, level: str) -> None:
    client = ThinkingClient(["unused"], ["Ok."])
    service, store = make_service(
        tmp_path,
        client,
        thinking=level,  # type: ignore[arg-type]
    )

    list(service.start_turn(store.new_conversation_id(), "Hi!"))

    assert client.requests[0]["think"] == level


def test_thinking_is_off_when_configured_or_unsupported(tmp_path: Path) -> None:
    off = ThinkingClient(["unused"], ["Ok."])
    service, store = make_service(tmp_path, off, thinking="off")

    list(service.start_turn(store.new_conversation_id(), "Explain this plan."))
    assert off.requests[0]["think"] is False
    assert off.requests[0]["think"] is not True

    unsupported = ThinkingClient(["unused"], ["Ok."], supports=False)
    service, store = make_service(tmp_path, unsupported)

    list(service.start_turn(store.new_conversation_id(), "Explain this plan."))
    # The field is omitted entirely for models without the capability.
    assert unsupported.requests[0]["think"] is None


def test_small_talk_and_thinking_hints_are_classified() -> None:
    assert _SMALL_TALK.match("Hallo!")
    assert _SMALL_TALK.match("how are you?")
    assert not _SMALL_TALK.match("How do I plan a migration?")
    assert _THINKING_HINT.search("Why is the build slow?")
    assert _THINKING_HINT.search("Berechne den Unterschied")
    assert not _THINKING_HINT.search("Open the Downloads folder")


def test_history_exposes_thinking_for_the_ui(tmp_path: Path) -> None:
    client = ThinkingClient(["Weigh the options. ", "Then answer."], ["Pick A."])
    service, store = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    list(service.start_turn(conversation_id, "Explain the trade-offs."))

    app = create_app(
        settings=make_settings(tmp_path),
        store=store,
        chat_service=service,
    )
    response = TestClient(app).get(f"/chat/history/{conversation_id}")

    assert response.status_code == 200
    assistant = response.json()["messages"][-1]
    assert assistant["thinking"] == "Weigh the options. Then answer."
    assert assistant["thinking_ms"] is not None


def test_native_client_sends_think_and_reads_thinking() -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, body: bytes, timeout: float) -> bytes:
        calls.append(json.loads(body))
        return json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "thinking": "Hidden reasoning.",
                    "content": "Answer.",
                }
            }
        ).encode("utf-8")

    client = NativeOllamaClient("http://127.0.0.1:11434/v1", post_json=fake_post)
    completion = client.chat.completions.create(
        model="qwen3-vl:8b",
        messages=[{"role": "user", "content": "Why?"}],
        think="low",
    )

    assert calls[0]["think"] == "low"
    assert completion.choices[0].message.thinking == "Hidden reasoning."

    client.chat.completions.create(
        model="qwen3-vl:8b-instruct",
        messages=[{"role": "user", "content": "Hi"}],
        think=None,
    )
    assert "think" not in calls[1]


def test_native_client_probes_the_thinking_capability(monkeypatch) -> None:
    payload = {
        "models": [
            {
                "name": "qwen3-vl:8b",
                "capabilities": ["completion", "vision", "tools", "thinking"],
            },
            {"name": "qwen3-vl:8b-instruct", "capabilities": ["completion", "tools"]},
        ]
    }

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )

    client = NativeOllamaClient("http://127.0.0.1:11434")
    assert client.supports_thinking("qwen3-vl:8b") is True
    assert client.supports_thinking("qwen3-vl:8b-instruct") is False


def test_thinking_is_capped_before_it_reaches_the_store() -> None:
    fields = ChatService._thinking_fields(["x" * 30_000], 100.0, 3)

    assert len(fields["thinking"]) < 30_000
    assert fields["thinking"].endswith("[...]")
    assert fields["thinking_ms"] == 100.0
    assert fields["thinking_tokens"] == 3


def test_thinking_fields_are_omitted_without_reasoning() -> None:
    assert ChatService._thinking_fields([], None, None) == {}


def test_tool_rounds_keep_thinking_for_the_final_answer(tmp_path: Path) -> None:
    class TwoRoundClient:
        def __init__(self) -> None:
            self.rounds = [
                [
                    {"message": {"role": "assistant", "thinking": "Look it up."}},
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "get_open_windows",
                                        "arguments": {},
                                    },
                                }
                            ],
                        }
                    },
                    {"message": {}, "done": True, "eval_count": 3},
                ],
                [
                    {"message": {"role": "assistant", "thinking": "Now answer."}},
                    {"message": {"role": "assistant", "content": "Two windows."}},
                    {"message": {}, "done": True, "eval_count": 2},
                ],
            ]

        def supports_thinking(self, model: str) -> bool:
            return True

        def stream_chat(self, **kwargs: Any):
            yield from self.rounds.pop(0)

    class StubTools:
        def requires_confirmation(self, name: str) -> bool:
            return False

        def execute(self, name: str, arguments: Any) -> dict[str, Any]:
            return {"ok": True, "windows": []}

    settings = make_settings(tmp_path)
    store = ConversationStore()
    service = ChatService(
        settings,
        store,
        KnowledgeBase(settings.memory.resolved_knowledge_path()),
        tools=StubTools(),  # type: ignore[arg-type]
        client=TwoRoundClient(),
    )

    events = list(service.start_turn(store.new_conversation_id(), "List my windows."))

    final = events[-1]
    assert final["type"] == "final"
    assert final["thinking"] == "Look it up.\n\nNow answer."


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.strip().split("\n\n"):
        lines = [
            line[len("data:") :].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if lines:
            events.append(json.loads("\n".join(lines)))
    return events


def test_stream_endpoint_delivers_thinking_events(tmp_path: Path) -> None:
    """The SSE layer must carry the reasoning, not only the service events."""
    client = ThinkingClient(["Compare the two options. "], ["Pick A."])
    service, store = make_service(tmp_path, client)
    app = create_app(
        settings=make_settings(tmp_path),
        store=store,
        chat_service=service,
    )

    with TestClient(app) as http:
        response = http.post(
            "/chat/stream",
            json={"message": "Explain the trade-offs."},
        )
    events = _parse_sse(response.text)

    thinking = [event for event in events if event["type"] == "assistant_thinking"]
    assert thinking
    assert "Compare the two options." in "".join(item["content"] for item in thinking)
    final = next(event for event in events if event["type"] == "final")
    assert final["thinking"] == "Compare the two options."
    assert final["thinking_ms"] is not None
