"""API tests for the local FastAPI backend."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent.chat import LLMError
from main import create_app
from memory.store import ConversationStore
from settings import MemorySettings, Settings, VisionSettings


class FakeChatService:
    """Deterministic stand-in for the Ollama-backed chat service."""

    def __init__(self, store: ConversationStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []
        self.learned: list[tuple[str, str]] = []
        self.error: LLMError | None = None

    def generate_reply(self, conversation_id: str, message: str) -> str:
        self.calls.append((conversation_id, message))

        if self.error is not None:
            raise self.error

        reply = f"Echo: {message}"
        self.store.append_exchange(conversation_id, message, reply)
        return reply

    def learn_from_exchange(self, user_message: str) -> None:
        self.learned.append(user_message)

    def discard_pending(self, conversation_id: str) -> bool:
        return False

    def get_pending_for_conversation(self, conversation_id: str):
        return None


@pytest.fixture
def store() -> ConversationStore:
    return ConversationStore(max_conversations=20)


@pytest.fixture
def fake_service(store: ConversationStore) -> FakeChatService:
    return FakeChatService(store)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        agent_name="TestBot",
        personality="Friendly and direct.",
        personality_file=tmp_path / "missing-personality.md",
        system_prompt="Keep answers short.",
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            max_conversations=20,
        ),
        vision=VisionSettings(
            enabled=False,
            index_path=tmp_path / "vision.sqlite3",
            scan_roots=[str(tmp_path)],
        ),
    )


@pytest.fixture
def client(
    settings: Settings,
    store: ConversationStore,
    fake_service: FakeChatService,
) -> TestClient:
    app = create_app(settings=settings, store=store, chat_service=fake_service)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "personal-agent-backend",
        "version": "0.1.0",
    }


def test_vision_status_reports_the_disabled_index(client: TestClient) -> None:
    response = client.get("/vision/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["indexed_images"] == 0
    assert payload["model"] == "clip-ViT-B-32"
    assert isinstance(payload["roots"], list)


def test_identity_is_exposed(client: TestClient) -> None:
    response = client.get("/identity")

    assert response.status_code == 200
    assert response.json() == {
        "name": "TestBot",
        "personality": "Friendly and direct.",
        "model": "qwen3-vl:8b-instruct",
    }


def test_chat_returns_reply_and_conversation_id(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "Echo: Hello"
    assert str(UUID(payload["conversation_id"])) == payload["conversation_id"]


def test_chat_uses_provided_conversation_id(
    client: TestClient,
    fake_service: FakeChatService,
) -> None:
    conversation_id = str(uuid4())

    response = client.post(
        "/chat",
        json={"message": "Hi", "conversation_id": conversation_id},
    )

    assert response.status_code == 200
    assert response.json()["conversation_id"] == conversation_id
    assert fake_service.calls == [(conversation_id, "Hi")]


def test_chat_learns_from_the_exchange_after_replying(
    client: TestClient,
    fake_service: FakeChatService,
) -> None:
    response = client.post("/chat", json={"message": "My name is Flo"})

    assert response.status_code == 200
    assert fake_service.learned == ["My name is Flo"]


def test_history_roundtrip(client: TestClient) -> None:
    chat = client.post("/chat", json={"message": "Remember the number 42"}).json()

    response = client.get(f"/chat/history/{chat['conversation_id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation_id"] == chat["conversation_id"]
    assert [(m["role"], m["content"]) for m in payload["messages"]] == [
        ("user", "Remember the number 42"),
        ("assistant", "Echo: Remember the number 42"),
    ]
    assert all(message["created_at"] for message in payload["messages"])


def test_history_endpoint_keeps_full_history_beyond_context_window(
    client: TestClient,
    store: ConversationStore,
) -> None:
    conversation_id = store.new_conversation_id()
    for index in range(12):
        store.append_exchange(
            conversation_id,
            f"question {index}",
            f"answer {index}",
        )

    response = client.get(f"/chat/history/{conversation_id}")

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert len(messages) == 24
    assert messages[0] == {
        "role": "user",
        "content": "question 0",
        "created_at": messages[0]["created_at"],
    }
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "answer 11"


def test_history_hides_internal_tool_messages(
    client: TestClient,
    store: ConversationStore,
) -> None:
    chat = client.post("/chat", json={"message": "Visible"}).json()
    conversation_id = chat["conversation_id"]
    store.append(
        conversation_id,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "create_folder", "arguments": '{"path":"x"}'},
            }
        ],
    )
    store.append(
        conversation_id,
        "tool",
        '{"ok": true}',
        tool_call_id="call_1",
        name="create_folder",
    )

    response = client.get(f"/chat/history/{conversation_id}")

    assert response.status_code == 200
    assert [
        (message["role"], message["content"])
        for message in response.json()["messages"]
    ] == [
        ("user", "Visible"),
        ("assistant", "Echo: Visible"),
    ]


def test_history_for_unknown_conversation_is_empty(client: TestClient) -> None:
    response = client.get(f"/chat/history/{uuid4()}")

    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_history_rejects_invalid_conversation_id(client: TestClient) -> None:
    response = client.get("/chat/history/not-a-uuid")

    assert response.status_code == 422


def test_chat_rejects_invalid_conversation_id(client: TestClient) -> None:
    response = client.post(
        "/chat",
        json={"message": "Hi", "conversation_id": "not-a-uuid"},
    )

    assert response.status_code == 422


def test_delete_history_clears_memory(client: TestClient) -> None:
    chat = client.post("/chat", json={"message": "Temporary"}).json()

    deleted = client.delete(f"/chat/history/{chat['conversation_id']}")
    history = client.get(f"/chat/history/{chat['conversation_id']}")

    assert deleted.status_code == 204
    assert history.json()["messages"] == []


def test_chat_maps_llm_errors(
    client: TestClient,
    fake_service: FakeChatService,
) -> None:
    fake_service.error = LLMError("Ollama is offline.", status_code=503)

    response = client.post("/chat", json={"message": "Hi"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Ollama is offline."


def test_chat_accepts_unicode_and_newlines(client: TestClient) -> None:
    message = 'Grüße 🚀 日本\n"quoted" <b>'

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json()["reply"] == f"Echo: {message}"


def test_chat_accepts_maximum_length_message(client: TestClient) -> None:
    message = "a" * 8000

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json()["reply"] == f"Echo: {message}"


def test_chat_rejects_empty_message(client: TestClient) -> None:
    response = client.post("/chat", json={"message": ""})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_short"


def test_chat_rejects_missing_message(client: TestClient) -> None:
    response = client.post("/chat", json={})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "missing"


def test_chat_rejects_too_long_message(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "a" * 8001})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"


def test_cors_allows_tauri_origin(client: TestClient) -> None:
    response = client.options(
        "/chat",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"
