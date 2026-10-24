"""Tests for prompt construction, learning, and the Ollama chat service."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx2 import Request
from openai import APIConnectionError, APITimeoutError

from agent.chat import ChatService, LLMError
from agent.learning import infer_deletions, parse_facts
from agent.language import detect_language_request, detect_requested_language
from agent.prompt import build_system_prompt
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import MemorySettings, OllamaSettings, Settings, ToolSettings


class FakeCompletions:
    def __init__(self, reply: str = "Default reply") -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))]
        )


class FakeClient:
    def __init__(self, reply: str = "Default reply") -> None:
        self.completions = FakeCompletions(reply)
        self.chat = SimpleNamespace(completions=self.completions)


class ScriptedCompletions:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )


class ScriptedClient:
    def __init__(self, replies: list[str]) -> None:
        self.completions = ScriptedCompletions(replies)
        self.chat = SimpleNamespace(completions=self.completions)


class FailingCompletions:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **kwargs: Any) -> Any:
        raise self.error


def make_failing_client(error: Exception) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=FailingCompletions(error))
    )


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "agent_name": "TestBot",
        "personality": "Curious and kind.",
        "system_prompt": "Answer in one short paragraph.",
        "ollama": OllamaSettings(model="hermes3", temperature=0.2),
        "memory": MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            max_conversations=10,
        ),
        "tools": ToolSettings(sandbox_dir=tmp_path / "sandbox"),
    }
    values.update(overrides)
    return Settings(**values)


def make_service(
    tmp_path: Path,
    client: Any,
    **settings_overrides: Any,
) -> tuple[ChatService, ConversationStore, KnowledgeBase]:
    settings = make_settings(tmp_path, **settings_overrides)
    store = ConversationStore(max_conversations=settings.memory.max_conversations)
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    return ChatService(settings, store, knowledge, client=client), store, knowledge


def test_system_prompt_contains_identity_and_guidance() -> None:
    settings = Settings(
        agent_name="Nova",
        personality="Warm and practical.",
        system_prompt="Ask clarifying questions.",
    )

    prompt = build_system_prompt(settings)

    assert "You are Nova" in prompt
    assert "Warm and practical." in prompt
    assert "Ask clarifying questions." in prompt
    assert "conversation history as your memory" in prompt
    assert "plain text" in prompt
    assert "takes precedence" in prompt
    assert "placeholder tokens" in prompt


def test_system_prompt_follows_the_users_language() -> None:
    prompt = build_system_prompt(
        Settings(agent_name="Nova"),
        "## Communication style\n- The user prefers to speak German.",
    )

    assert "language of the user's latest message" in prompt
    assert "switch to that language immediately" in prompt
    assert "Never enforce a language" in prompt
    assert "RESPONSE LANGUAGE" not in prompt


def test_detect_language_request() -> None:
    assert detect_language_request("Let's speak Spanish from now on.") == "Spanish"
    assert detect_language_request("Now answer in Chinese.") == "Chinese"
    assert detect_language_request("Switch back to English.") == "English"
    assert detect_language_request("Speak German, please.") == "German"
    assert detect_language_request(
        "Jetzt bitte auf Deutsch: Wie heisse ich?"
    ) == "Deutsch"
    assert detect_language_request("What is my name?") is None
    assert detect_language_request("Answer in detail.") is None
    assert detect_language_request("Answer briefly.") is None
    assert detect_language_request("Please tell me about my trip in Spain.") is None
    assert detect_language_request("In Spanish, please.") == "Spanish"
    assert detect_language_request("Please in German.") == "German"


def test_parse_facts_rejects_temporary_language_instructions() -> None:
    result = parse_facts(
        "COMMUNICATION: The user prefers to speak Chinese in every conversation "
        "with the assistant.\n"
        "COMMUNICATION: The user wants to speak Spanish during this chat.\n"
        "COMMUNICATION: The user would like to switch to speaking English for "
        "the conversation.\n"
        "COMMUNICATION: Prefers short answers.\n"
    )

    assert result.facts["communication"] == ["Prefers short answers."]


def test_detect_requested_language_prefers_most_recent_request() -> None:
    messages = [
        {"role": "user", "content": "Let's speak Spanish from now on."},
        {"role": "assistant", "content": "Claro."},
        {"role": "user", "content": "What is my name?"},
    ]

    assert detect_requested_language(messages) == "Spanish"

    messages.append({"role": "user", "content": "Switch back to English."})
    assert detect_requested_language(messages) == "English"


def test_system_prompt_includes_requested_language() -> None:
    prompt = build_system_prompt(
        Settings(agent_name="Nova"),
        "## Communication style\n- The user prefers to speak German.",
        "Spanish",
    )

    assert "explicitly requested Spanish" in prompt
    assert "Write the entire reply in Spanish" in prompt


def test_generate_reply_uses_requested_language(tmp_path) -> None:
    client = FakeClient("Claro.")
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    store.append_exchange(
        conversation_id,
        "Let's speak Spanish from now on.",
        "Claro.",
    )

    service.generate_reply(conversation_id, "What is my name?")

    messages = client.completions.requests[0]["messages"]
    assert "explicitly requested Spanish" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    assert "Reply entirely in Spanish" in messages[-1]["content"]


def test_system_prompt_includes_knowledge() -> None:
    settings = Settings(agent_name="Nova", system_prompt="Be helpful.")

    prompt = build_system_prompt(settings, "## Interests\n- Likes cycling")

    assert "Long-term knowledge about the user" in prompt
    assert "Likes cycling" in prompt


def test_generate_reply_sends_history_and_stores_exchange(tmp_path) -> None:
    client = FakeClient("You are Flo.")
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    store.append_exchange(conversation_id, "My name is Flo.", "Nice to meet you, Flo.")

    reply = service.generate_reply(conversation_id, "What is my name?")

    request = client.completions.requests[0]
    sent = request["messages"]
    assert request["model"] == "hermes3"
    assert request["temperature"] == 0.2
    assert sent[0]["role"] == "system"
    assert "TestBot" in sent[0]["content"]
    assert sent[1:3] == [
        {"role": "user", "content": "My name is Flo."},
        {"role": "assistant", "content": "Nice to meet you, Flo."},
    ]
    assert sent[-1] == {"role": "user", "content": "What is my name?"}
    assert reply == "You are Flo."

    stored = store.load(conversation_id)
    assert stored[-2]["content"] == "What is my name?"
    assert stored[-1]["content"] == "You are Flo."


def test_generate_reply_sends_the_entire_chat_history(tmp_path) -> None:
    client = FakeClient()
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    store.append_exchange(conversation_id, "u1", "a1")
    store.append_exchange(conversation_id, "u2", "a2")
    store.append_exchange(conversation_id, "u3", "a3")

    service.generate_reply(conversation_id, "u4")

    sent = client.completions.requests[0]["messages"]
    assert sent[1:] == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]


def test_generate_reply_injects_knowledge_base(tmp_path) -> None:
    client = FakeClient("Sure.")
    service, store, knowledge = make_service(tmp_path, client)
    knowledge.add_facts(
        {"identity": ["Name is Flo"], "preferences": ["Favorite color is teal"]}
    )

    service.generate_reply(store.new_conversation_id(), "What do you know about me?")

    system_prompt = client.completions.requests[0]["messages"][0]["content"]
    assert "Name is Flo" in system_prompt
    assert "Favorite color is teal" in system_prompt


def test_learn_from_exchange_extracts_and_stores_facts(tmp_path) -> None:
    client = ScriptedClient(
        [
            "Nice to meet you, Flo.",
            "IDENTITY: Name is Flo\n"
            "INTERESTS: Likes teal\n"
            "PROJECTS: Works on the personal agent project",
        ]
    )
    service, store, knowledge = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    service.generate_reply(conversation_id, "My name is Flo and I like teal.")

    added = service.learn_from_exchange(
        "My name is Flo and I like teal.",
    )

    assert added == [
        ("## Identity / name", "Name is Flo"),
        ("## Interests", "Likes teal"),
        ("## Projects", "Works on the personal agent project"),
    ]
    text = knowledge.read()
    assert text.index("## Identity / name") < text.index("Name is Flo")
    assert text.index("## Interests") < text.index("Likes teal")
    assert text.index("## Projects") < text.index("Works on the personal agent project")
    learning_request = client.completions.requests[1]
    learning_prompt = learning_request["messages"][1]["content"]
    assert "Current knowledge base" in learning_prompt
    # Only the user's own words may be used; assistant text caused false facts.
    assert "Nice to meet you, Flo." not in learning_prompt


def test_learn_from_exchange_skips_duplicates_and_none(tmp_path) -> None:
    client = FakeClient("NONE")
    service, _, knowledge = make_service(tmp_path, client)

    assert service.learn_from_exchange("Hi") == []

    knowledge.add_facts({"identity": ["Name is Flo"]})
    assert service.learn_from_exchange("I'm Flo") == []


def test_infer_deletions_finds_negated_preference() -> None:
    knowledge = (
        "## Preferences\n"
        "- The user enjoys jazz music.\n"
        "- The user likes tea.\n"
    )

    assert infer_deletions(
        knowledge,
        "Actually, I don't like jazz anymore.",
    ) == ["The user enjoys jazz music."]


def test_infer_deletions_ignores_generic_topics() -> None:
    knowledge = "## Preferences\n- The user enjoys jazz music.\n"

    assert infer_deletions(knowledge, "I don't like loud music anymore.") == []


def test_learn_from_exchange_removes_replaced_preference(tmp_path) -> None:
    client = ScriptedClient(
        [
            "Noted.",
            "PREFERENCES: Prefers classical music",
        ]
    )
    service, store, knowledge = make_service(tmp_path, client)
    knowledge.add_facts({"preferences": ["The user enjoys jazz music."]})
    message = "Actually, I don't like jazz anymore. I prefer classical music."

    service.generate_reply(store.new_conversation_id(), message)
    added = service.learn_from_exchange(message)

    text = knowledge.read().lower()
    assert "enjoys jazz" not in text
    assert "classical" in text
    assert added == [("## Preferences", "Prefers classical music")]


def test_learning_can_be_disabled(tmp_path) -> None:
    client = FakeClient("- Name is Flo")
    service, _, knowledge = make_service(
        tmp_path,
        client,
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=False,
        ),
    )

    assert service.learn_from_exchange("I'm Flo") == []
    assert "Name is Flo" not in knowledge.read()
    assert client.completions.requests == []


def test_learning_failure_is_swallowed(tmp_path) -> None:
    request = Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    service, _, _ = make_service(
        tmp_path,
        make_failing_client(APIConnectionError(request=request)),
    )

    assert service.learn_from_exchange("Hi") == []


def test_parse_facts_accepts_only_concrete_categorized_lines() -> None:
    result = parse_facts(
        "IDENTITY: Name is Flo\n"
        "INTERESTS: Likes teal\n"
        "- HABITS: Wakes at 6:00\n"
        "IDENTITY: Name is [NAME]\n"
        "INTERESTS: Enjoys [topic 1]\n"
        "- The user enjoys discussing\n"
        "PREFERENCES: Is allergic to peanuts\n"
        "DELETE: Name is Alex\n"
        "random line\n"
        "NONE"
    )

    assert result.facts["identity"] == ["Name is Flo"]
    assert result.facts["interests"] == ["Likes teal"]
    assert result.facts["habits"] == ["Wakes at 6:00"]
    assert result.facts["preferences"] == ["Is allergic to peanuts"]
    assert result.facts["other"] == ["random line"]
    assert result.deletions == ["Name is Alex"]


def test_parse_facts_rejects_none_and_query_requests() -> None:
    result = parse_facts(
        "PROJECTS: NONE\n"
        "LEARNED: SEARCH_FILES: The user asked the assistant to search for files.\n"
        "PROJECTS: The user wants to create a new folder named AgentFeatureTest "
        "in Downloads.\n"
        "PROJECTS: The user wants to use unzip_file on C:\\Downloads\\x.zip.\n"
        "PREFERENCES: The user prefers short answers.\n"
    )

    assert result.facts["projects"] == []
    assert result.facts["other"] == []
    assert result.facts["preferences"] == ["The user prefers short answers."]


def test_parse_facts_handles_nested_delete_and_replace() -> None:
    result = parse_facts(
        "IDENTITY: DELETE: Name is Bob\n"
        "IDENTITY: REPLACE: Name is Carl -> Name is Dana\n"
    )

    assert result.deletions == ["Name is Bob", "Name is Carl"]
    assert result.facts["identity"] == ["Name is Dana"]


def test_empty_llm_response_raises_error_and_rolls_back_user_turn(tmp_path) -> None:
    service, store, _ = make_service(tmp_path, FakeClient("   "))
    conversation_id = store.new_conversation_id()

    with pytest.raises(LLMError):
        service.generate_reply(conversation_id, "Hello")

    assert store.load(conversation_id) == []


def test_connection_failure_maps_to_503(tmp_path) -> None:
    request = Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    service, store, _ = make_service(
        tmp_path,
        make_failing_client(APIConnectionError(request=request)),
    )
    conversation_id = store.new_conversation_id()

    with pytest.raises(LLMError) as error:
        service.generate_reply(conversation_id, "Hello")

    assert error.value.status_code == 503
    assert "Ollama" in str(error.value)
    assert store.load(conversation_id) == []


def test_timeout_maps_to_504(tmp_path) -> None:
    request = Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    service, store, _ = make_service(
        tmp_path,
        make_failing_client(APITimeoutError(request=request)),
    )
    conversation_id = store.new_conversation_id()

    with pytest.raises(LLMError) as error:
        service.generate_reply(conversation_id, "Hello")

    assert error.value.status_code == 504
    assert store.load(conversation_id) == []
