"""Phase 29 + 30 verification: semantic memory and LAN bridge protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from agent.chat import ChatService
from bridge import LanBridge
from main import create_app
from memory.knowledge import KnowledgeBase
from memory.semantic import SemanticMemory, chunk_text
from memory.store import ConversationStore
from settings import (
    BridgeSettings,
    MemorySettings,
    OllamaSettings,
    SemanticSettings,
    Settings,
    ToolSettings,
)


class FakeEmbedder:
    """Tiny deterministic embedder: one axis per topic."""

    name = "fake"
    dimension = 4
    using_fallback = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            low = text.casefold()
            vector = [0.0, 0.0, 0.0, 0.0]
            if "build" in low or "compile" in low:
                vector[0] = 1.0
            if "deploy" in low:
                vector[1] = 1.0
            if "python" in low:
                vector[2] = 1.0
            if not any(vector):
                vector[3] = 1.0
            vectors.append(vector)
        return vectors


def make_semantic(tmp_path: Path) -> SemanticMemory:
    settings = SemanticSettings(
        enabled=True,
        storage_path=tmp_path / "qdrant",
        collection="test_memory",
        min_score=0.1,
        ingest_min_chars=1,
    )
    return SemanticMemory(
        settings,
        embedder=FakeEmbedder(),
        client=QdrantClient(location=":memory:"),
    )


def test_semantic_ingest_search_and_context(tmp_path: Path) -> None:
    memory = make_semantic(tmp_path)
    stored = memory.ingest(
        "The desktop build command is `pnpm build:desktop`.",
        category="code",
        source="test",
    )
    assert stored == 1

    hits = memory.search("How do I build the desktop app?")
    assert hits and "pnpm build:desktop" in hits[0]["text"]
    assert hits[0]["category"] == "code"

    context = memory.context_for("How do I build the desktop app?")
    assert context.startswith("<memory_context>")
    assert "pnpm build:desktop" in context
    assert "untrusted reference data" in context

    # A query about another topic must not leak the build note.
    assert memory.search("Which deploy pipeline is used?") == []
    memory.shutdown()


def test_semantic_deduplicates_identical_chunks(tmp_path: Path) -> None:
    memory = make_semantic(tmp_path)
    memory.ingest("Stable fact about python packaging.", category="fact")
    memory.ingest("Stable fact about python packaging.", category="fact")
    assert memory.status()["points"] == 1
    memory.shutdown()


def test_chunking_keeps_document_order() -> None:
    text = "First sentence. Second sentence. Third sentence."
    chunks = chunk_text(text, 20)
    assert chunks
    assert "First" in chunks[0]
    assert "Third" in chunks[-1]


def _make_chat(tmp_path: Path, semantic: SemanticMemory) -> ChatService:
    settings = Settings(
        agent_name="MemoryBot",
        personality="Precise.",
        personality_file=tmp_path / "missing.md",
        ollama=OllamaSettings(model="test-model"),
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
        tools=ToolSettings(sandbox_dir=tmp_path / "sandbox"),
        semantic=SemanticSettings(enabled=True),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    service = ChatService(
        settings,
        store,
        knowledge,
        client=object(),
        semantic=semantic,
    )
    return service


def test_retrieval_injects_after_five_turns_without_touching_history(
    tmp_path: Path,
) -> None:
    memory = make_semantic(tmp_path)
    memory.ingest(
        "The user's release checklist requires `pnpm build:desktop`.",
        category="fact",
        source="test",
    )
    service = _make_chat(tmp_path, memory)

    history = [
        {"role": "user", "content": "hello there, this is turn one"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "tell me about python packaging"},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "and deployment"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "remind me how the release build works"},
    ]
    messages = service._build_messages(history)

    system = messages[0]["content"]
    assert "<memory_context>" in system
    assert "pnpm build:desktop" in system
    # The retrieval block is prompt-only; stored history stays clean.
    assert all("memory_context" not in (m.get("content") or "") for m in history)
    memory.shutdown()


def test_semantic_disabled_returns_nothing(tmp_path: Path) -> None:
    memory = SemanticMemory(
        SemanticSettings(enabled=False, storage_path=tmp_path / "qdrant"),
        embedder=FakeEmbedder(),
    )
    assert memory.search("anything relevant") == []
    assert memory.submit("long enough text to ingest") is False
    assert memory.status()["enabled"] is False


# ------------------------------------------------------------------ bridge


class FakeStore:
    def new_conversation_id(self) -> str:
        return "conv-mobile"


class FakeChat:
    def __init__(self) -> None:
        self.interrupted: list[str] = []

    def start_turn(self, conversation_id: str, text: str):
        yield {"type": "status", "conversation_id": conversation_id, "message": "working"}
        yield {
            "type": "final",
            "conversation_id": conversation_id,
            "reply": f"done: {text}",
        }

    def resume(self, approval_id: str, approved: bool):
        yield {"type": "final", "reply": f"approved={approved}"}

    def interrupt(self, conversation_id: str) -> bool:
        self.interrupted.append(conversation_id)
        return True


def make_bridge(tmp_path: Path) -> LanBridge:
    return LanBridge(
        BridgeSettings(
            enabled=True,
            host="127.0.0.1",
            port=8799,
            token_file=tmp_path / "bridge-token.txt",
        ),
        FakeChat(),  # type: ignore[arg-type]
        FakeStore(),
    )


def test_bridge_rejects_bad_token(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    client = TestClient(bridge.app)
    with client.websocket_connect("/ws?token=wrong") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == 4401


def test_bridge_authenticated_task_and_poll(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    client = TestClient(bridge.app)
    token = bridge.token()

    with client.websocket_connect(f"/ws?token={token}") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_text('{"type":"task","text":"compile the app"}')
        events = [websocket.receive_json() for _ in range(3)]
    assert [event["type"] for event in events] == [
        "task_accepted",
        "status",
        "final",
    ]
    assert events[-1]["reply"] == "done: compile the app"
    assert all(event["origin"] == "mobile" for event in events)

    polled = client.get(
        "/api/poll",
        params={"token": token, "since": 0},
    )
    assert polled.status_code == 200
    body = polled.json()
    assert body["cursor"] >= 3
    assert [event["type"] for event in body["events"]][:3] == [
        "task_accepted",
        "status",
        "final",
    ]

    push = bridge.notify("Build finished", "Compa compiled the app", "success")
    assert push["type"] == "notification"
    polled = client.get(
        "/api/poll",
        params={"token": token, "since": body["cursor"]},
    ).json()
    assert polled["events"][-1]["title"] == "Build finished"

    unauthorised = client.get("/api/poll", params={"token": "nope"})
    assert unauthorised.status_code == 401


def test_bridge_cancel_and_status(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path)
    client = TestClient(bridge.app)
    token = bridge.token()
    with client.websocket_connect(f"/ws?token={token}") as websocket:
        websocket.receive_json()
        websocket.send_text('{"type":"cancel","conversation_id":"conv-mobile"}')
        reply = websocket.receive_json()
    assert reply["ok"] is True
    assert bridge._chat.interrupted == ["conv-mobile"]

    status = client.get("/api/status", params={"token": token})
    assert status.status_code == 200
    assert "token" not in status.json()
    assert bridge.status()["token"] == token


def test_semantic_http_routes(tmp_path: Path) -> None:
    memory = make_semantic(tmp_path)

    class NoopChat:
        def shutdown(self) -> None:
            return None

        def start_turn(self, *args: Any, **kwargs: Any):
            return iter(())

        def resume(self, *args: Any, **kwargs: Any):
            return iter(())

        def get_pending(self, approval_id: str):
            raise KeyError(approval_id)

        def interrupt(self, conversation_id: str) -> bool:
            return True

        def discard_pending(self, conversation_id: str) -> bool:
            return False

        def learn_from_exchange(self, user_message: str) -> None:
            return None

    settings = Settings(
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
    )
    app = create_app(
        settings=settings,
        store=ConversationStore(),
        chat_service=NoopChat(),  # type: ignore[arg-type]
        semantic_service=memory,
        config_path=tmp_path / "settings.json",
    )
    with TestClient(app) as client:
        ingested = client.post(
            "/memory/semantic/ingest",
            json={"text": "The build command is pnpm build.", "category": "code"},
        )
        assert ingested.status_code == 200
        assert ingested.json()["chunks"] == 1
        found = client.post(
            "/memory/semantic/search",
            json={"query": "how do I build?", "limit": 3},
        )
        assert found.status_code == 200
        assert "pnpm build" in found.json()["results"][0]["text"]
        status = client.get("/memory/semantic/status")
        assert status.json()["points"] == 1
    memory.shutdown()
