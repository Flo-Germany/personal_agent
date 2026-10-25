"""Tests for prompt construction, learning, and the Ollama chat service."""

from __future__ import annotations

import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx2 import Request
from openai import APIConnectionError, APITimeoutError

from agent.chat import CONTEXT_WINDOW_CHARS, ChatService, LLMError
from agent.learning import infer_deletions, parse_facts, should_apply_deletion
from agent.language import detect_language_request, detect_requested_language
from agent.prompt import build_knowledge_block, build_system_prompt
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


class StreamingClient:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.requests: list[dict[str, Any]] = []

    def stream_chat(self, **kwargs: Any):
        self.requests.append(kwargs)
        yield from self.chunks


class ScriptedStreamingClient:
    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds = list(rounds)
        self.requests: list[dict[str, Any]] = []

    def stream_chat(self, **kwargs: Any):
        self.requests.append(kwargs)
        yield from self.rounds.pop(0)


class StubTools:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def requires_confirmation(self, name: str) -> bool:
        return False

    def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        self.executed.append((name, arguments))
        return {"ok": True, "windows": []}


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


def test_system_prompt_contains_identity_and_guidance(tmp_path) -> None:
    settings = Settings(
        agent_name="Nova",
        personality="Warm and practical.",
        personality_file=tmp_path / "missing-personality.md",
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


def test_system_prompt_describes_the_gui_automation_loop(tmp_path) -> None:
    prompt = build_system_prompt(make_settings(tmp_path))

    assert "analyze_screen_ui" in prompt
    assert "prefer the CLI" in prompt
    assert "take their hands off the mouse and keyboard" in prompt
    assert "never reuse a snapshot" in prompt
    assert "aborted: true" in prompt
    assert "Never click Send" in prompt


def test_system_prompt_loads_personality_file(tmp_path) -> None:
    personality_file = tmp_path / "personality.md"
    personality_file.write_text(
        "# Character\n- Dry humor and calm.\n- Your name is {{agent_name}}.",
        encoding="utf-8",
    )
    settings = Settings(
        agent_name="Compa",
        personality="This inline text must be ignored.",
        personality_file=personality_file,
    )

    prompt = build_system_prompt(settings)

    assert "Dry humor and calm." in prompt
    assert "Your name is Compa." in prompt
    assert "This inline text must be ignored." not in prompt


def test_system_prompt_falls_back_to_inline_personality(tmp_path) -> None:
    settings = Settings(
        agent_name="Compa",
        personality="Warm and practical.",
        personality_file=tmp_path / "missing-personality.md",
    )

    assert "Warm and practical." in build_system_prompt(settings)


def test_system_prompt_includes_self_aware_identity() -> None:
    settings = Settings(
        agent_name="Compa",
        ollama=OllamaSettings(model="hermes3"),
    )

    prompt = build_system_prompt(
        settings,
        now=datetime(2026, 10, 24, 9, 30),
        project_root=Path(r"C:\agent"),
    )

    assert "Your identity, which stays the same in every conversation" in prompt
    assert "Name: Compa" in prompt
    assert "AI language model, not a human" in prompt
    assert "served by Ollama" in prompt
    assert "not your name or your identity" in prompt
    assert "Personal Agent desktop app" in prompt
    assert "Where you are running" in prompt
    assert "Operating system:" in prompt
    assert "Computer name:" in prompt
    assert "User account:" in prompt
    assert r"Project folder: C:\agent" in prompt
    assert "Current local date: Saturday, 24 October 2026" in prompt
    assert "say you are not sure instead of inventing it" in prompt


def test_system_prompt_is_stable_across_minutes() -> None:
    settings = Settings(agent_name="Compa", ollama=OllamaSettings(model="hermes3"))

    first = build_system_prompt(settings, now=datetime(2026, 10, 24, 9, 30))
    second = build_system_prompt(settings, now=datetime(2026, 10, 24, 9, 31))

    assert first == second


def test_quantization_warning_flags_large_models() -> None:
    assert OllamaSettings(model="qwen3-vl:8b-instruct").quantization_warning() is None
    warning = OllamaSettings(model="some-model:q8_0").quantization_warning()
    assert warning is not None
    assert "q4_K_M" in warning
    assert OllamaSettings().keep_alive_seconds == 1800
    assert OllamaSettings().warmup_on_startup is False


def test_system_prompt_instructs_powershell_administration() -> None:
    prompt = build_system_prompt(Settings(agent_name="Compa"))

    assert "Windows administrator" in prompt
    assert "run_powershell_query" in prompt
    assert "run_powershell_action" in prompt
    assert "Query first" in prompt
    assert "description" in prompt
    assert "Never try to bypass approval" in prompt
    assert "If a run_powershell_action fails" in prompt
    assert "stderr" in prompt


def test_system_prompt_follows_the_users_language() -> None:
    prompt = build_system_prompt(
        Settings(agent_name="Nova"),
        "## Communication style\n- The user prefers to speak German.",
    )

    assert "language of the user's latest message" in prompt
    assert "switch to that language immediately" in prompt
    assert "Critical: choose the language from the latest user message" in prompt
    assert "Never announce or mention the knowledge-base language preference" in prompt
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


def test_system_prompt_can_leave_knowledge_for_the_user_turn() -> None:
    settings = Settings(agent_name="Nova")
    knowledge = "## Interests\n- Likes cycling"

    with_knowledge = build_system_prompt(settings, knowledge)
    without = build_system_prompt(
        settings,
        knowledge,
        include_knowledge=False,
    )

    assert "Likes cycling" in with_knowledge
    assert "Likes cycling" not in without
    assert "Likes cycling" in build_knowledge_block(knowledge)


def test_knowledge_stays_in_the_system_prompt_and_clock_in_the_user_turn(
    tmp_path,
) -> None:
    knowledge_path = tmp_path / "knowledge.md"
    knowledge_path.write_text("# User\n- Name is Flo\n", encoding="utf-8")
    client = FakeClient()
    service, _, _ = make_service(
        tmp_path,
        client,
        ollama=OllamaSettings(model="qwen3:8b", temperature=0.2),
    )
    history = [{"role": "user", "content": "What is my name?"}]

    first = service._build_messages(
        history,
        now=datetime(2026, 10, 24, 9, 30),
    )

    assert "Name is Flo" in first[0]["content"]
    assert "Name is Flo" not in first[-1]["content"]
    assert first[-1]["content"].startswith("[Current local time:")

    second = service._build_messages(
        history,
        now=datetime(2026, 10, 24, 9, 31),
    )

    # The system prompt is stable across minutes; only the user clock changes.
    assert first[0]["content"] == second[0]["content"]
    assert first[-1]["content"] != second[-1]["content"]

    knowledge_path.write_text(
        "# User\n- Name is Flo\n- Age 20\n",
        encoding="utf-8",
    )
    third = service._build_messages(
        history,
        now=datetime(2026, 10, 24, 9, 31),
    )
    assert "Age 20" in third[0]["content"]


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
    assert "explicitly requested Spanish" in messages[-1]["content"]
    assert messages[-1]["role"] == "user"
    assert "Reply entirely in Spanish" in messages[-1]["content"]


def test_system_prompt_strips_standing_language_preferences() -> None:
    knowledge = (
        "## Preferences\n"
        "- Likes cycling\n"
        "- The user prefers to speak German in every conversation with the "
        "assistant, no matter the topic or timing.\n"
        "- Wants the assistant to always answer in Spanish.\n"
    )

    prompt = build_system_prompt(Settings(agent_name="Nova"), knowledge)

    assert "Likes cycling" in prompt
    assert "prefers to speak German" not in prompt
    assert "always answer in Spanish" not in prompt


def test_system_prompt_includes_knowledge() -> None:
    settings = Settings(agent_name="Nova", system_prompt="Be helpful.")

    prompt = build_system_prompt(settings, "## Interests\n- Likes cycling")

    assert "Long-term knowledge about the user" in prompt
    assert "Likes cycling" in prompt


def test_streaming_completion_emits_deltas_and_metrics(tmp_path) -> None:
    client = StreamingClient(
        [
            {"message": {"role": "assistant", "content": "Hel"}},
            {"message": {"role": "assistant", "content": "lo"}},
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 2,
                "eval_duration": 1_000_000_000,
                "total_duration": 2_000_000_000,
            },
        ]
    )
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Hi"))
    types = [event["type"] for event in events]

    assert "assistant_start" in types
    assert types.count("assistant_delta") == 2
    assert "assistant_end" in types
    assert "metrics" in types
    assert types[-1] == "final"
    assert "".join(
        event["content"]
        for event in events
        if event["type"] == "assistant_delta"
    ) == "Hello"
    metrics = next(event for event in events if event["type"] == "metrics")
    assert metrics["live"] is False
    assert metrics["model"] == "hermes3"
    assert "first_token_ms" in metrics
    assert metrics["prompt_tokens"] == 10
    assert metrics["completion_tokens"] == 2
    assert metrics["tokens_per_second"] == 2.0
    assert metrics["eval_ms"] == 1000.0
    assert metrics["total_ms"] == 2000.0
    assert events[-1]["reply"] == "Hello"
    assert events[-1]["streamed"] is True
    assert store.load(conversation_id)[-1]["content"] == "Hello"


def test_non_streaming_client_still_emits_stream_events(tmp_path) -> None:
    service, store, _ = make_service(tmp_path, FakeClient("Hello there"))
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Hi"))
    deltas = [event for event in events if event["type"] == "assistant_delta"]

    assert [event["content"] for event in deltas] == ["Hello there"]
    assert any(event["type"] == "assistant_start" for event in events)
    assert any(event["type"] == "assistant_end" for event in events)
    assert events[-1]["reply"] == "Hello there"
    assert events[-1]["streamed"] is True


def test_text_tool_call_stream_is_reset_before_the_tool_runs(tmp_path) -> None:
    raw_call = '{"name": "get_open_windows", "arguments": {}}'
    client = ScriptedStreamingClient(
        [
            [
                {"message": {"role": "assistant", "content": raw_call}},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ],
            [
                {"message": {"role": "assistant", "content": "Done."}},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "eval_count": 1,
                    "eval_duration": 1_000_000_000,
                },
            ],
        ]
    )
    settings = make_settings(tmp_path)
    store = ConversationStore(max_conversations=10)
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    tools = StubTools()
    service = ChatService(settings, store, knowledge, tools=tools, client=client)
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Which windows are open?"))
    types = [event["type"] for event in events]

    assert types.count("assistant_reset") == 1
    assert [event["content"] for event in events if event["type"] == "assistant_delta"] == [
        raw_call,
        "Done.",
    ]
    assert tools.executed == [("get_open_windows", "{}")]
    assert events[-1]["reply"] == "Done."
    assert events[-1]["streamed"] is True

    assistant_contents = [
        message["content"]
        for message in store.load(conversation_id)
        if message["role"] == "assistant"
    ]
    assert raw_call not in assistant_contents
    assert assistant_contents[-1] == "Done."


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
    # The system message is always at index 0. Hermes also gets the prompt
    # folded into the latest user turn because its template drops system
    # messages when tools are supplied.
    assert sent[0]["role"] == "system"
    assert "TestBot" in sent[0]["content"]
    assert sent[1] == {"role": "user", "content": "My name is Flo."}
    assert sent[2] == {"role": "assistant", "content": "Nice to meet you, Flo."}
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"].startswith("SYSTEM PROMPT")
    assert "TestBot" in sent[-1]["content"]
    assert sent[-1]["content"].endswith("What is my name?")
    assert reply == "You are Flo."

    stored = store.load(conversation_id)
    assert stored[-2]["content"] == "What is my name?"
    assert stored[-1]["content"] == "You are Flo."


def test_non_hermes_model_keeps_the_system_message(tmp_path) -> None:
    client = FakeClient()
    service, store, _ = make_service(
        tmp_path,
        client,
        ollama=OllamaSettings(model="qwen3:8b", temperature=0.2),
    )

    service.generate_reply(store.new_conversation_id(), "Hello.")

    sent = client.completions.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "TestBot" in sent[0]["content"]
    assert sent[1]["role"] == "user"
    assert sent[1]["content"].startswith("[Current local time:")
    assert "SYSTEM PROMPT" not in sent[1]["content"]
    assert sent[1]["content"].endswith("Hello.")


def test_clock_is_added_to_the_newest_user_turn_only(tmp_path) -> None:
    client = FakeClient()
    service, _, _ = make_service(
        tmp_path,
        client,
        ollama=OllamaSettings(model="qwen3:8b", temperature=0.2),
    )
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new"},
    ]

    first = service._build_messages(history, now=datetime(2026, 10, 24, 9, 30))
    second = service._build_messages(history, now=datetime(2026, 10, 24, 9, 31))

    # The cacheable system prompt must not contain the minute-precision clock.
    assert first[0]["content"] == second[0]["content"]
    assert first[1]["content"] == "old"
    assert first[-1]["content"].startswith(
        "[Current local time: 09:30, Saturday, 24 October 2026]"
    )
    assert first[-1]["content"].endswith("new")
    assert second[-1]["content"].startswith(
        "[Current local time: 09:31, Saturday, 24 October 2026]"
    )


def test_warm_up_sends_a_minimal_prefill_request(tmp_path) -> None:
    client = FakeClient()
    service, _, _ = make_service(tmp_path, client)

    assert service.warm_up() is True

    request = client.completions.requests[0]
    assert request["max_tokens"] == 1
    assert request["tool_choice"] == "auto"
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][-1]["role"] == "user"
    assert request["messages"][-1]["content"].endswith("Reply with OK.")


def test_warm_up_is_skipped_when_keep_alive_is_zero(tmp_path) -> None:
    client = FakeClient()
    service, _, _ = make_service(
        tmp_path,
        client,
        ollama=OllamaSettings(model="hermes3", keep_alive_seconds=0),
    )

    assert service.warm_up() is False
    assert client.completions.requests == []


def test_system_prompt_mode_can_force_user_folding(tmp_path) -> None:
    client = FakeClient()
    service, store, _ = make_service(
        tmp_path,
        client,
        ollama=OllamaSettings(
            model="qwen3:8b",
            temperature=0.2,
            system_prompt_mode="user",
        ),
    )

    service.generate_reply(store.new_conversation_id(), "Hello.")

    sent = client.completions.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "TestBot" in sent[0]["content"]
    assert sent[-1]["role"] == "user"
    assert "SYSTEM PROMPT" in sent[-1]["content"]
    assert sent[-1]["content"].endswith("Hello.")


def test_generate_reply_sends_recent_chat_history(tmp_path) -> None:
    client = FakeClient()
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    store.append_exchange(conversation_id, "u1", "a1")
    store.append_exchange(conversation_id, "u2", "a2")
    store.append_exchange(conversation_id, "u3", "a3")

    service.generate_reply(conversation_id, "u4")

    sent = client.completions.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1:-1] == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"].endswith("u4")


def test_generate_reply_uses_a_ten_message_context_window(tmp_path) -> None:
    client = FakeClient()
    service, store, _ = make_service(tmp_path, client)
    conversation_id = store.new_conversation_id()
    for index in range(1, 16):
        store.append_exchange(conversation_id, f"u{index}", f"a{index}")

    service.generate_reply(conversation_id, "u16")

    sent = client.completions.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert len(sent) <= 11  # system + at most 10 history messages
    assert sent[1] == {"role": "user", "content": "u12"}
    assert sent[-1]["role"] == "user"
    assert sent[-1]["content"].endswith("u16")
    sent_contents = {message.get("content", "") for message in sent[1:]}
    assert "u1" not in sent_contents
    assert "a1" not in sent_contents

    # The saved history and the UI history remain complete.
    stored = store.load(conversation_id)
    assert len(stored) == 32
    assert stored[0]["content"] == "u1"
    assert stored[-2]["content"] == "u16"


def test_context_window_keeps_the_request_in_a_long_tool_loop() -> None:
    history: list[dict[str, object]] = [
        {"role": "user", "content": "diagnose the whole machine"}
    ]
    for index in range(30):
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": "run_powershell_query"},
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "content": f"result {index}",
                "tool_call_id": f"call_{index}",
            }
        )

    window = ChatService._context_window(history)

    assert window[0] == history[0]
    assert len(window) <= 11
    seen_calls: set[str] = set()
    for message in window:
        if message.get("role") == "assistant":
            seen_calls.update(
                call["id"] for call in message.get("tool_calls") or []
            )
        if message.get("role") == "tool":
            assert message["tool_call_id"] in seen_calls


def test_context_window_respects_the_character_budget() -> None:
    history: list[dict[str, object]] = [
        {"role": "user", "content": "keep this request"}
    ]
    for index in range(8):
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": "read_file_content"},
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "content": "x" * 12000,
                "tool_call_id": f"call_{index}",
            }
        )

    window = ChatService._context_window(history)

    assert window[0] == history[0]
    assert len(window) < len(history)
    total = sum(len(str(message.get("content") or "")) for message in window)
    assert total <= CONTEXT_WINDOW_CHARS + len("keep this request")


def test_context_window_drops_orphaned_tool_messages() -> None:
    window = [
        {"role": "tool", "content": "orphan", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "ok"},
    ]

    assert ChatService._context_window(window) == [
        {"role": "assistant", "content": "ok"}
    ]


def test_context_window_drops_a_tool_call_without_results() -> None:
    window = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {}}],
        },
        {"role": "user", "content": "next"},
    ]

    assert ChatService._context_window(window) == [
        {"role": "user", "content": "next"}
    ]


def test_generate_reply_injects_knowledge_base(tmp_path) -> None:
    client = FakeClient("Sure.")
    service, store, knowledge = make_service(tmp_path, client)
    knowledge.add_facts(
        {"identity": ["Name is Flo"], "preferences": ["Favorite color is teal"]}
    )

    service.generate_reply(store.new_conversation_id(), "What do you know about me?")

    system_prompt = client.completions.requests[0]["messages"][-1]["content"]
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


def test_learning_skips_commands_without_personal_cues(tmp_path) -> None:
    client = ScriptedClient(["IDENTITY: Name is Flo"])
    service, _, _ = make_service(tmp_path, client)

    assert service.learn_from_exchange("Create a folder named Test.") == []
    assert client.completions.requests == []


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


def test_infer_deletions_finds_facts_without_a_positive_verb() -> None:
    knowledge = (
        "## Preferences\n"
        "- Favorite color is teal\n"
        "- Likes cycling\n"
    )

    assert infer_deletions(knowledge, "I no longer like teal.") == [
        "Favorite color is teal"
    ]


def test_parse_facts_rejects_negative_restatements() -> None:
    result = parse_facts(
        "PREFERENCES: no longer likes teal\n"
        "PREFERENCES: Prefers short answers\n"
    )

    assert result.facts["preferences"] == ["Prefers short answers"]


def test_parse_facts_rejects_one_off_action_requests() -> None:
    result = parse_facts(
        "INTERESTS: The user is interested in finding an image of a car.\n"
        "PROJECTS: The user wants the assistant to open Steam for them.\n"
        "PREFERENCES: The user prefers to use Notepad to open specific text files.\n"
        "PREFERENCES: The user prefers using Microsoft Outlook.\n"
        "HABITS: The user has the habit of reading the newest file in their "
        "Downloads folder and asking for its size.\n"
    )

    assert result.facts["interests"] == []
    assert result.facts["projects"] == []
    assert result.facts["preferences"] == [
        "The user prefers using Microsoft Outlook."
    ]
    assert result.facts["habits"] == []


def test_should_apply_deletion_only_for_related_messages() -> None:
    assert should_apply_deletion(
        "The user is 20 years old and a student.",
        "What do you know about me?",
    ) is False
    assert should_apply_deletion("Name is Flo", "My name is Alex") is True
    assert should_apply_deletion("Favorite color is teal", "I no longer like teal") is True
    assert should_apply_deletion(
        "The user prefers to speak German.",
        "Answer in English, please.",
    ) is False


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


def test_parse_facts_rejects_tool_requests_and_permission_instructions() -> None:
    result = parse_facts(
        "PROJECTS: Create a folder named UIRedesignCheck in the Downloads folder.\n"
        "PREFERENCES: The user prefers to use the create_folder command without "
        "asking for permission in text.\n"
        "PREFERENCES: The user prefers short answers.\n"
    )

    assert result.facts["projects"] == []
    assert result.facts["preferences"] == ["The user prefers short answers."]


def test_parse_facts_rejects_none_and_query_requests() -> None:
    result = parse_facts(
        "PROJECTS: NONE\n"
        "LEARNED: SEARCH_FILES: The user asked the assistant to search for files.\n"
        "PROJECTS: The user wants to create a new folder named AgentFeatureTest "
        "in Downloads.\n"
        "PROJECTS: The user wants to use unzip_file on C:\\Downloads\\x.zip.\n"
        "PROJECTS: The user wants the assistant to search their whole computer "
        "for a picture of a cyberpunk city.\n"
        "PROJECTS: The user is currently searching their computer for a picture "
        "of a cyberpunk city at night.\n"
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


def test_reply_file_urls_get_the_drive_colon() -> None:
    text = (
        "[a](file:///C/Users/Flo/a.txt) "
        "[b](file:///d/Users/Flo/b.txt) "
        "[c](file:///D:/already/correct.txt)"
    )

    fixed = ChatService._normalize_reply(text)

    assert "file:///C:/Users/Flo/a.txt" in fixed
    assert "file:///D:/Users/Flo/b.txt" in fixed
    assert "file:///D:/already/correct.txt" in fixed


def test_generate_reply_repairs_file_urls(tmp_path) -> None:
    client = FakeClient("See [notes.txt](file:///C/Users/Flo/notes.txt).")
    service, store, _ = make_service(tmp_path, client)

    reply = service.generate_reply(store.new_conversation_id(), "Where is it?")

    assert reply == "See [notes.txt](file:///C:/Users/Flo/notes.txt)."


def test_missing_native_model_maps_to_503(tmp_path) -> None:
    error = urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat",
        404,
        "Not Found",
        None,
        None,
    )
    service, store, _ = make_service(tmp_path, make_failing_client(error))
    conversation_id = store.new_conversation_id()

    with pytest.raises(LLMError) as raised:
        service.generate_reply(conversation_id, "Hello")

    assert raised.value.status_code == 503
    assert "not installed" in str(raised.value)
    assert store.load(conversation_id) == []


def test_unreachable_native_ollama_maps_to_503(tmp_path) -> None:
    service, store, _ = make_service(
        tmp_path,
        make_failing_client(urllib.error.URLError("connection refused")),
    )
    conversation_id = store.new_conversation_id()

    with pytest.raises(LLMError) as raised:
        service.generate_reply(conversation_id, "Hello")

    assert raised.value.status_code == 503
    assert "can't reach Ollama" in str(raised.value)
    assert store.load(conversation_id) == []
