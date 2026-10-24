"""Tests for the in-memory conversation store."""

from __future__ import annotations

import pytest

from memory.store import ConversationStore


def test_append_and_load_roundtrip() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()

    store.append_exchange(conversation_id, "Hello", "Hi there")

    messages = store.load(conversation_id)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "Hello"),
        ("assistant", "Hi there"),
    ]
    assert all(message["created_at"] for message in messages)


def test_history_is_not_shared_between_store_instances() -> None:
    conversation_id = ConversationStore().new_conversation_id()
    store = ConversationStore()
    store.append_exchange(conversation_id, "Remember me", "Sure")

    assert ConversationStore().load(conversation_id) == []
    assert len(store.load(conversation_id)) == 2


def test_conversations_are_isolated() -> None:
    store = ConversationStore()
    first = store.new_conversation_id()
    second = store.new_conversation_id()

    store.append_exchange(first, "first conversation", "ok")
    store.append_exchange(second, "second conversation", "ok")

    assert store.load(first)[0]["content"] == "first conversation"
    assert store.load(second)[0]["content"] == "second conversation"


def test_entire_chat_history_is_kept() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()

    for index in range(250):
        store.append(conversation_id, "user", f"question {index}")
        store.append(conversation_id, "assistant", f"answer {index}")

    messages = store.load(conversation_id)
    assert len(messages) == 500
    assert messages[0]["content"] == "question 0"
    assert messages[-1]["content"] == "answer 249"


def test_oldest_conversation_is_evicted_at_capacity() -> None:
    store = ConversationStore(max_conversations=2)
    first = store.new_conversation_id()
    second = store.new_conversation_id()
    third = store.new_conversation_id()

    store.append(first, "user", "one")
    store.append(second, "user", "two")

    # Loading refreshes recency, so the untouched conversation is evicted.
    assert store.load(first)
    store.append(third, "user", "three")

    assert store.conversation_count == 2
    assert store.load(first)
    assert store.load(second) == []
    assert store.load(third)


def test_pop_last_removes_newest_message() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()
    store.append(conversation_id, "user", "Hello")

    removed = store.pop_last(conversation_id)

    assert removed is not None
    assert removed["content"] == "Hello"
    assert store.load(conversation_id) == []
    assert store.pop_last(conversation_id) is None


def test_tool_messages_roundtrip_and_are_copied() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()

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

    messages = store.load(conversation_id)
    assert [message["role"] for message in messages] == ["assistant", "tool"]
    assert messages[0]["tool_calls"][0]["id"] == "call_1"
    assert messages[1]["tool_call_id"] == "call_1"
    assert messages[1]["name"] == "create_folder"

    messages[0]["tool_calls"][0]["function"]["name"] = "changed"
    assert store.load(conversation_id)[0]["tool_calls"][0]["function"]["name"] == (
        "create_folder"
    )


def test_truncate_keeps_only_the_requested_prefix() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()
    for content in ("one", "two", "three", "four"):
        store.append(conversation_id, "user", content)

    store.truncate(conversation_id, 2)
    assert [message["content"] for message in store.load(conversation_id)] == [
        "one",
        "two",
    ]

    store.truncate(conversation_id, 0)
    assert store.load(conversation_id) == []
    assert store.conversation_count == 0


def test_clear_removes_conversation() -> None:
    store = ConversationStore()
    conversation_id = store.new_conversation_id()
    store.append_exchange(conversation_id, "Hello", "Hi")

    assert store.clear(conversation_id) is True
    assert store.load(conversation_id) == []
    assert store.clear(conversation_id) is False


def test_clear_all_removes_every_conversation() -> None:
    store = ConversationStore()
    store.append(store.new_conversation_id(), "user", "one")
    store.append(store.new_conversation_id(), "user", "two")

    store.clear_all()

    assert store.conversation_count == 0


def test_invalid_conversation_id_is_rejected() -> None:
    store = ConversationStore()

    with pytest.raises(ValueError):
        store.load("../../etc/passwd")


def test_invalid_role_is_rejected() -> None:
    store = ConversationStore()

    with pytest.raises(ValueError):
        store.append(store.new_conversation_id(), "system", "nope")
