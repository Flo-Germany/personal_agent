"""Tests for HITL pauses, approvals, denials, and streaming events."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.chat import ChatService, ConfirmationRequired
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import AgentSettings, MemorySettings, OllamaSettings, Settings
from tools.executor import ToolExecutor
from tools.schemas import TOOL_SCHEMAS


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


class InterruptingStreamingClient:
    def __init__(self, on_first_chunk: Any) -> None:
        self.on_first_chunk = on_first_chunk

    def stream_chat(self, **kwargs: Any):
        yield {"message": {"role": "assistant", "content": "Partial"}}
        self.on_first_chunk()
        yield {"message": {"role": "assistant", "content": " later"}}
        yield {"message": {"role": "assistant", "content": ""}, "done": True}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def tool_calls_message(*calls: Any, content: str = "") -> Any:
    return SimpleNamespace(content=content, tool_calls=list(calls))


def content_message(content: str) -> Any:
    return SimpleNamespace(content=content, tool_calls=None)


def make_service(
    tmp_path: Path,
    messages: list[Any],
    max_iterations: int = 3,
    learn_from_chats: bool = False,
) -> tuple[ChatService, ConversationStore, ScriptedClient]:
    settings = Settings(
        ollama=OllamaSettings(model="hermes3", temperature=0.2, max_retries=0),
        agent=AgentSettings(max_iterations=max_iterations),
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=learn_from_chats,
        ),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    executor = ToolExecutor(settings.tools)
    client = ScriptedClient(messages)
    service = ChatService(settings, store, knowledge, executor, client=client)
    return service, store, client


def make_service_with_client(
    tmp_path: Path,
    client: Any,
    max_iterations: int = 3,
) -> tuple[ChatService, ConversationStore]:
    settings = Settings(
        ollama=OllamaSettings(model="hermes3", temperature=0.2, max_retries=0),
        agent=AgentSettings(max_iterations=max_iterations),
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=False,
        ),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    executor = ToolExecutor(settings.tools)
    service = ChatService(settings, store, knowledge, executor, client=client)
    return service, store


def final_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    final = next((event for event in events if event["type"] == "final"), None)
    assert final is not None, events
    return final


def action_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    action = next(
        (event for event in events if event["type"] == "action_required"),
        None,
    )
    assert action is not None, events
    return action


def test_read_only_tool_runs_without_approval(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("read_file_content", {"path": str(note)})
            ),
            content_message("The note says hello."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Read my note."))

    assert not any(event["type"] == "action_required" for event in events)
    assert final_event(events)["reply"] == "The note says hello."
    assert any(event["type"] == "tool_result" for event in events)
    roles = [message["role"] for message in store.load(conversation_id)]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_modifying_tool_pauses_until_approved(tmp_path: Path) -> None:
    target = tmp_path / "approved-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
            content_message("I created the folder."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create a folder."))
    action = action_event(paused)

    assert not target.exists()
    assert action["actions"][0]["tool"] == "create_folder"
    assert [m["role"] for m in store.load(conversation_id)] == [
        "user",
        "assistant",
    ]

    resumed = list(service.resume(action["approval_id"], True))

    assert target.is_dir()
    assert final_event(resumed)["reply"] == "I created the folder."
    history = store.load(conversation_id)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2]["tool_call_id"] == "call_1"
    assert json.loads(history[2]["content"])["ok"] is True


def test_denial_cancels_action_and_continues(tmp_path: Path) -> None:
    target = tmp_path / "denied-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
            content_message("No problem, I cancelled that."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create a folder."))
    action = action_event(paused)
    resumed = list(service.resume(action["approval_id"], False))

    assert not target.exists()
    assert final_event(resumed)["reply"] == "No problem, I cancelled that."
    history = store.load(conversation_id)
    tool_result = json.loads(history[2]["content"])
    assert tool_result["cancelled"] is True
    assert tool_result["ok"] is False


def test_interrupt_stops_the_turn_and_keeps_a_marker(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}

    def stop_after_first_chunk() -> None:
        assert holder["service"].interrupt(holder["conversation_id"]) is True

    client = InterruptingStreamingClient(stop_after_first_chunk)
    service, store = make_service_with_client(tmp_path, client)
    conversation_id = store.new_conversation_id()
    holder["service"] = service
    holder["conversation_id"] = conversation_id

    events = list(service.start_turn(conversation_id, "Write a long answer."))

    assert events[-1]["type"] == "cancelled"
    assert not any(event["type"] == "final" for event in events)
    assert any(event["type"] == "assistant_delta" for event in events)

    history = store.load(conversation_id)
    assert [message["content"] for message in history] == [
        "Write a long answer.",
        "Partial",
        "Interrupted by user.",
    ]


def test_interrupt_without_an_active_turn_is_a_noop(tmp_path: Path) -> None:
    client = ScriptedClient([content_message("ok")])
    service, store = make_service_with_client(tmp_path, client)
    conversation_id = store.new_conversation_id()

    assert service.interrupt(conversation_id) is False


def test_scheduled_learning_coalesces_rapid_messages(tmp_path: Path) -> None:
    service, store, _ = make_service(
        tmp_path,
        [content_message("ok")],
        learn_from_chats=True,
    )
    service._learning_delay = 0.05  # type: ignore[attr-defined]
    recorded: list[str] = []
    service.learn_from_exchange = lambda message: recorded.append(message) or []  # type: ignore[method-assign]
    conversation_id = store.new_conversation_id()

    service.schedule_learning(conversation_id, "I am 20.")
    service.schedule_learning(conversation_id, "I live in Berlin.")
    time.sleep(0.25)

    assert recorded == ["I am 20.\n\nI live in Berlin."]


def test_scheduled_learning_waits_for_an_active_turn(tmp_path: Path) -> None:
    service, store, _ = make_service(
        tmp_path,
        [content_message("ok")],
        learn_from_chats=True,
    )
    service._learning_delay = 0.05  # type: ignore[attr-defined]
    recorded: list[str] = []
    service.learn_from_exchange = lambda message: recorded.append(message) or []  # type: ignore[method-assign]
    conversation_id = store.new_conversation_id()

    service.schedule_learning(conversation_id, "I am 20.")
    service._begin_turn(conversation_id)
    time.sleep(0.2)
    assert recorded == []

    service._end_turn(conversation_id)
    time.sleep(0.3)
    assert recorded == ["I am 20."]


def test_existing_pending_is_reemitted_without_new_user_message(tmp_path: Path) -> None:
    target = tmp_path / "pending-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
        ],
    )
    conversation_id = store.new_conversation_id()

    first = list(service.start_turn(conversation_id, "Create a folder."))
    second = list(service.start_turn(conversation_id, "Are you done?"))

    assert action_event(first)["approval_id"] == action_event(second)["approval_id"]
    assert len(store.load(conversation_id)) == 2


def test_multiple_modifications_share_one_approval(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(first)}, "call_1"),
                tool_call("create_folder", {"path": str(second)}, "call_2"),
            ),
            content_message("Both folders are ready."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create two folders."))
    action = action_event(paused)

    assert len(action["actions"]) == 2
    assert action["auto_approve_remaining"] is True
    assert not first.exists() and not second.exists()

    resumed = list(service.resume(action["approval_id"], True))

    assert first.is_dir() and second.is_dir()
    assert final_event(resumed)["reply"] == "Both folders are ready."
    tool_messages = [m for m in store.load(conversation_id) if m["role"] == "tool"]
    assert len(tool_messages) == 2


def test_approval_event_includes_the_request_and_following_plan(
    tmp_path: Path,
) -> None:
    target = tmp_path / "planned-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)}),
                content=(
                    "1. Create the folder.\n"
                    "2. Verify that it exists.\n"
                    "3. Report the result.\n"
                ),
            ),
            content_message("Created and verified."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            "First create the folder, then verify it.",
        )
    )
    action = action_event(paused)

    assert action["request"] == "First create the folder, then verify it."
    assert action["plan"] == [
        "Create the folder",
        "Verify that it exists",
        "Report the result",
    ]


def test_plan_extraction_accepts_numbered_and_bulleted_steps() -> None:
    assert ChatService._extract_plan_steps(
        "Here is the plan:\n1. Create a folder.\n2) Write the file.\n- Verify it.\nDone."
    ) == ["Create a folder.", "Write the file.", "Verify it."]


def test_plan_from_request_parses_first_then_sequences() -> None:
    assert ChatService._plan_from_request(
        'First create the folder C:\\temp, then create the file note.txt '
        'with "hello". Perform both tasks in order.'
    ) == [
        "Create the folder C:\\temp",
        'Create the file note.txt with "hello"',
    ]
    assert ChatService._plan_from_request("Create the folder.") == []


def test_plan_from_request_parses_and_und_sequences() -> None:
    assert ChatService._plan_from_request(
        'erstelle einen order und erstelle dort eine text datei mit '
        '"hello world" als text drin'
    ) == [
        "Erstelle einen order",
        'Erstelle dort eine text datei mit "hello world" als text drin',
    ]
    assert ChatService._plan_from_request(
        "Create a folder and write a note."
    ) == ["Create a folder", "Write a note"]
    assert ChatService._plan_from_request(
        "Create a folder and its contents."
    ) == []


def test_plan_from_request_splits_comma_action_lists() -> None:
    assert ChatService._plan_from_request(
        "erstelle einen Ordner, erstelle darin eine Textdatei und schreibe "
        "'hello world' als Text hinein"
    ) == [
        "Erstelle einen Ordner",
        "Erstelle darin eine Textdatei",
        "Schreibe 'hello world' als Text hinein",
    ]
    assert ChatService._plan_from_request(
        "Create a folder, create a text file inside it, and write hello "
        "world into the file."
    ) == [
        "Create a folder",
        "Create a text file inside it",
        "Write hello world into the file",
    ]
    # A comma inside quoted content is not an action separator.
    assert ChatService._plan_from_request(
        'Create a file named "hello, world.txt".'
    ) == []


def test_approval_falls_back_to_german_und_sequence(tmp_path: Path) -> None:
    target = tmp_path / "Order"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
            content_message("Done."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            'erstelle einen order und erstelle dort eine text datei mit '
            '"hello world" als text drin',
        )
    )
    action = action_event(paused)

    assert action["plan"] == [
        "Erstelle einen order",
        'Erstelle dort eine text datei mit "hello world" als text drin',
    ]


def test_plan_selection_prefers_the_more_complete_request_plan(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "complete-plan"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "propose_plan",
                    {"steps": ["Create a folder", "Write hello"]},
                    "call_plan",
                )
            ),
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            content_message("Done."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            "Create a folder, create a text file inside it, and write hello "
            "into the file.",
        )
    )
    action = action_event(paused)

    assert action["plan"] == [
        "Create a folder",
        "Create a text file inside it",
        "Write hello into the file",
    ]


def test_approval_falls_back_to_the_user_sequence(tmp_path: Path) -> None:
    folder = tmp_path / "fallback-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            content_message("Done."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            "First create the folder, then create the note.",
        )
    )
    action = action_event(paused)

    assert action["plan"] == ["Create the folder", "Create the note"]


def test_propose_plan_tool_feeds_the_approval_card(tmp_path: Path) -> None:
    folder = tmp_path / "planned-tool-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "propose_plan",
                    {"steps": ["Create the folder", "Create the note"]},
                    "call_plan",
                )
            ),
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            content_message("Created."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            "First create the folder, then create the note.",
        )
    )
    action = action_event(paused)

    assert action["plan"] == ["Create the folder", "Create the note"]
    assert not folder.exists()

    resumed = list(service.resume(action["approval_id"], True))

    assert folder.is_dir()
    assert final_event(resumed)["reply"] == "Created."
    tool_messages = [
        message for message in store.load(conversation_id) if message["role"] == "tool"
    ]
    assert any(message.get("name") == "propose_plan" for message in tool_messages)


def test_approval_covers_remaining_steps_in_the_same_turn(tmp_path: Path) -> None:
    folder = tmp_path / "sequence-folder"
    note = folder / "note.txt"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            tool_calls_message(
                tool_call(
                    "create_file",
                    {"path": str(note), "content": "hello"},
                    "call_2",
                )
            ),
            content_message("Created the folder and the note."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(
            conversation_id,
            "First create a folder, then create a note inside it.",
        )
    )
    action = action_event(paused)
    assert not folder.exists()

    resumed = list(service.resume(action["approval_id"], True))

    assert folder.is_dir()
    assert note.read_text(encoding="utf-8") == "hello"
    assert final_event(resumed)["reply"] == "Created the folder and the note."
    assert not any(event["type"] == "action_required" for event in resumed)
    tool_messages = [m for m in store.load(conversation_id) if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert all(json.loads(m["content"])["ok"] for m in tool_messages)


def test_denied_approval_does_not_authorize_following_steps(tmp_path: Path) -> None:
    first = tmp_path / "denied-first"
    second = tmp_path / "denied-second"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(first)})
            ),
            tool_calls_message(
                tool_call("create_folder", {"path": str(second)}, "call_2")
            ),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(conversation_id, "First create one, then another.")
    )
    first_action = action_event(paused)
    resumed = list(service.resume(first_action["approval_id"], False))

    second_action = action_event(resumed)
    assert not second.exists()
    assert second_action["actions"][0]["tool"] == "create_folder"

    list(service.resume(second_action["approval_id"], False))


def test_auto_approval_does_not_repeat_an_action(tmp_path: Path) -> None:
    folder = tmp_path / "duplicate-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)}, "call_2")
            ),
            content_message("Done."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create the folder."))
    action = action_event(paused)
    resumed = list(service.resume(action["approval_id"], True))

    assert final_event(resumed)["reply"] == "Done."
    tool_messages = [m for m in store.load(conversation_id) if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert json.loads(tool_messages[0]["content"])["ok"] is True
    assert "already ran" in json.loads(tool_messages[1]["content"])["error"]


def test_auto_approval_preserves_mixed_call_order(tmp_path: Path) -> None:
    folder = tmp_path / "ordered-folder"
    note = folder / "note.txt"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(folder)})
            ),
            tool_calls_message(
                tool_call(
                    "create_file",
                    {"path": str(note), "content": "hello"},
                    "call_2",
                ),
                tool_call("read_file_content", {"path": str(note)}, "call_3"),
            ),
            content_message("Created and read it."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create the folder."))
    action = action_event(paused)
    resumed = list(service.resume(action["approval_id"], True))

    assert final_event(resumed)["reply"] == "Created and read it."
    tool_messages = [m for m in store.load(conversation_id) if m["role"] == "tool"]
    read_result = next(
        json.loads(message["content"])
        for message in tool_messages
        if message.get("name") == "read_file_content"
    )
    assert read_result["ok"] is True
    assert read_result["content"] == "hello"


def test_protected_path_is_refused_after_approval(tmp_path: Path) -> None:
    protected = Path(r"C:\Windows\phase5_must_not_exist")
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(protected)})
            ),
            content_message("Windows is protected, so I did not create it."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create it in Windows."))
    action = action_event(paused)
    resumed = list(service.resume(action["approval_id"], True))

    assert not protected.exists()
    assert final_event(resumed)["reply"] == "Windows is protected, so I did not create it."
    tool_message = next(
        m for m in store.load(conversation_id) if m["role"] == "tool"
    )
    result = json.loads(tool_message["content"])
    assert result["ok"] is False
    assert "protected" in result["error"].lower()


def test_generate_reply_raises_confirmation_instead_of_executing(tmp_path: Path) -> None:
    target = tmp_path / "legacy-folder"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
        ],
    )
    conversation_id = store.new_conversation_id()

    with pytest.raises(ConfirmationRequired) as error:
        service.generate_reply(conversation_id, "Create a folder.")

    assert not target.exists()
    assert error.value.pending.actions[0]["tool"] == "create_folder"
    assert service.get_pending_for_conversation(conversation_id) is not None


def test_screen_question_does_not_force_a_second_analysis(tmp_path: Path) -> None:
    service, store, client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "what is on my screen"},
                )
            ),
            content_message("I see a test."),
        ],
    )

    events = list(
        service.start_turn(store.new_conversation_id(), "What is on my screen?")
    )

    assert len(client.completions.requests) == 2
    assert (
        sum(
            1
            for event in events
            if event["type"] == "tool_result" and event["tool"] == "analyze_screen"
        )
        == 1
    )
    assert final_event(events)["reply"] == "I see a test."


def test_screen_question_forces_analyze_screen(tmp_path: Path) -> None:
    service, store, client = make_service(
        tmp_path,
        [
            content_message("I can't see your screen."),
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "what is on my screen"},
                )
            ),
            content_message("The screen shows a test."),
        ],
    )

    events = list(
        service.start_turn(store.new_conversation_id(), "What is on my screen?")
    )

    assert len(client.completions.requests) == 3
    nudge = client.completions.requests[1]["messages"][-1]["content"]
    assert "Call the analyze_screen tool now" in nudge
    assert any(
        event["type"] == "tool_result" and event["tool"] == "analyze_screen"
        for event in events
    )
    assert final_event(events)["reply"] == "The screen shows a test."


class DisplayVision:
    """Fake vision service with two displays and a recording analyze call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.contexts: list[dict[str, Any]] = []
        self.is_loaded = True

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
        self.contexts.append(
            {"system_prompt": system_prompt, "tools": tools}
        )
        return {
            "ok": True,
            "question": user_question,
            "answer": f"Display {display} shows a browser.",
            "display": {"index": display, "width": 3840, "height": 2160},
            "screen": {"width": 1600, "height": 900},
            "message": f"Analyzed Display {display}.",
        }


def make_screen_service(
    tmp_path: Path,
    messages: list[Any],
) -> tuple[ChatService, ConversationStore, DisplayVision]:
    settings = Settings(
        ollama=OllamaSettings(
            model="qwen3-vl:8b-instruct",
            temperature=0.2,
            max_retries=0,
        ),
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
    return service, store, vision


def test_screen_analysis_asks_for_a_display_then_uses_the_choice(
    tmp_path: Path,
) -> None:
    service, store, vision = make_screen_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "What is on my screen?"},
                )
            ),
            content_message("The main display shows a browser."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(
        service.start_turn(conversation_id, "What is on my screen?")
    )

    choice = next(
        event for event in events if event["type"] == "display_required"
    )
    assert [display["index"] for display in choice["displays"]] == [1, 2]
    assert choice["displays"][1]["primary"] is True
    assert choice["question"] == "What is on my screen?"
    assert vision.calls == []
    assert [message["role"] for message in store.load(conversation_id)] == [
        "user"
    ]
    assert service.get_pending_for_conversation(conversation_id) is not None

    resumed = list(service.resume_display(choice["approval_id"], 2))

    assert vision.calls == [("What is on my screen?", 2)]
    assert vision.contexts[0]["system_prompt"] is not None
    assert "You are Nova" in vision.contexts[0]["system_prompt"]
    assert vision.contexts[0]["tools"] == TOOL_SCHEMAS
    assert final_event(resumed)["reply"] == (
        "The main display shows a browser."
    )
    history = store.load(conversation_id)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    arguments = json.loads(
        history[1]["tool_calls"][0]["function"]["arguments"]
    )
    assert arguments["display"] == 2
    assert json.loads(history[2]["content"])["ok"] is True
    assert service.get_pending_for_conversation(conversation_id) is None


def test_display_choice_rejects_an_unknown_display(
    tmp_path: Path,
) -> None:
    service, store, vision = make_screen_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "What is on my screen?"},
                )
            ),
            content_message("The left display shows a browser."),
        ],
    )
    conversation_id = store.new_conversation_id()
    events = list(
        service.start_turn(conversation_id, "What is on my screen?")
    )
    choice = next(
        event for event in events if event["type"] == "display_required"
    )

    with pytest.raises(ValueError):
        service.resume_display(choice["approval_id"], 9)

    assert service.get_pending_for_conversation(conversation_id) is not None
    assert vision.calls == []

    resumed = list(service.resume_display(choice["approval_id"], 1))

    assert vision.calls == [("What is on my screen?", 1)]
    assert final_event(resumed)["reply"] == "The left display shows a browser."


def test_display_choice_can_be_cancelled(tmp_path: Path) -> None:
    service, store, vision = make_screen_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "What is on my screen?"},
                )
            ),
            content_message("I did not look at a display."),
        ],
    )
    conversation_id = store.new_conversation_id()
    events = list(
        service.start_turn(conversation_id, "What is on my screen?")
    )
    choice = next(
        event for event in events if event["type"] == "display_required"
    )

    resumed = list(service.resume_display(choice["approval_id"], None))

    assert vision.calls == []
    tool_result = next(
        event for event in resumed if event["type"] == "tool_result"
    )
    assert tool_result["result"]["cancelled"] is True
    assert final_event(resumed)["reply"] == "I did not look at a display."


def test_explicit_display_still_lets_the_user_choose(tmp_path: Path) -> None:
    service, store, vision = make_screen_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {
                        "user_question": "What is on my screen?",
                        "display": 2,
                    },
                )
            ),
            content_message("Display 1 shows a browser."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(
        service.start_turn(conversation_id, "Look at my second display.")
    )

    choice = next(
        event for event in events if event["type"] == "display_required"
    )
    assert [display["index"] for display in choice["displays"]] == [1, 2]
    assert vision.calls == []

    resumed = list(service.resume_display(choice["approval_id"], 1))

    assert vision.calls == [("What is on my screen?", 1)]
    assert final_event(resumed)["reply"] == "Display 1 shows a browser."


def test_generate_reply_surfaces_a_display_choice(tmp_path: Path) -> None:
    service, store, _vision = make_screen_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "analyze_screen",
                    {"user_question": "What is on my screen?"},
                )
            ),
        ],
    )
    conversation_id = store.new_conversation_id()

    with pytest.raises(ConfirmationRequired) as error:
        service.generate_reply(conversation_id, "What is on my screen?")

    assert error.value.pending.kind == "display"
    assert len(error.value.pending.displays) == 2
    assert service.get_pending_for_conversation(conversation_id) is not None


def test_powershell_query_runs_without_approval(tmp_path: Path) -> None:
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "run_powershell_query",
                    {
                        "command": (
                            "Get-Process -Id $PID | "
                            "Select-Object Id, ProcessName"
                        )
                    },
                )
            ),
            content_message("Done."),
        ],
    )

    events = list(
        service.start_turn(
            store.new_conversation_id(),
            "Check your PowerShell process.",
        )
    )

    assert any(
        event["type"] == "tool_result"
        and event["tool"] == "run_powershell_query"
        and event["ok"]
        for event in events
    )
    assert not any(event["type"] == "action_required" for event in events)
    assert final_event(events)["reply"] == "Done."


def test_powershell_action_requires_and_executes_after_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "powershell-action.txt"
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "run_powershell_action",
                    {
                        "command": (
                            f"New-Item -ItemType File -Path '{target}' -Force"
                        ),
                        "description": "Create a PowerShell test file.",
                    },
                )
            ),
            content_message("Created."),
        ],
    )

    paused = list(
        service.start_turn(
            store.new_conversation_id(),
            "Create a test file with PowerShell.",
        )
    )

    action = action_event(paused)
    assert action["actions"][0]["tool"] == "run_powershell_action"
    assert "Create a PowerShell test file." in action["actions"][0]["description"]
    assert "Command:" in action["actions"][0]["description"]
    assert not target.exists()

    resumed = list(service.resume(action["approval_id"], True))

    assert target.exists()
    assert final_event(resumed)["reply"] == "Created."


def test_powershell_action_failure_triggers_recovery_attempt(
    tmp_path: Path,
) -> None:
    service, store, client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "run_powershell_action",
                    {
                        "command": "Stop-Process -Id 999999 -Force",
                        "description": "Stop a stale process ID.",
                    },
                )
            ),
            content_message("The action failed; I could not stop it."),
            tool_calls_message(
                tool_call(
                    "run_powershell_query",
                    {
                        "command": (
                            "Get-Process -Id 999999 -ErrorAction SilentlyContinue"
                        )
                    },
                )
            ),
            content_message("The process no longer exists."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(
        service.start_turn(conversation_id, "Close process 999999.")
    )
    action = action_event(paused)
    resumed = list(service.resume(action["approval_id"], True))

    assert any(
        "The last run_powershell_action failed" in request["messages"][-1]["content"]
        for request in client.completions.requests
        if request["messages"][-1].get("role") == "user"
    )
    assert any(
        event["type"] == "tool_result"
        and event["tool"] == "run_powershell_query"
        for event in resumed
    )
    assert final_event(resumed)["reply"] == "The process no longer exists."


def test_empty_response_is_retried_with_a_nudge(tmp_path: Path) -> None:
    service, store, client = make_service(
        tmp_path,
        [
            SimpleNamespace(content="", tool_calls=None),
            content_message("Recovered after the retry."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Hello."))

    assert final_event(events)["reply"] == "Recovered after the retry."
    assert len(client.completions.requests) == 2
    retry_messages = client.completions.requests[1]["messages"]
    # hermes3 folds the system prompt into a user turn, so the nudge does too.
    assert retry_messages[-1]["role"] == "user"
    assert "Continue the user's request" in retry_messages[-1]["content"]


def test_action_promise_forces_a_tool_call(tmp_path: Path) -> None:
    target = tmp_path / "promised-folder"
    service, store, client = make_service(
        tmp_path,
        [
            SimpleNamespace(
                content="Next, I will create the folder for you.",
                tool_calls=None,
            ),
            tool_calls_message(
                tool_call("create_folder", {"path": str(target)})
            ),
            content_message("Created it."),
        ],
    )
    conversation_id = store.new_conversation_id()

    paused = list(service.start_turn(conversation_id, "Create a folder for me."))
    action = action_event(paused)
    requests = client.completions.requests

    assert len(requests) == 2
    assert requests[1]["tool_choice"] == "required"
    assert action["actions"][0]["tool"] == "create_folder"
    assert not target.exists()

    resumed = list(service.resume(action["approval_id"], True))
    assert final_event(resumed)["reply"] == "Created it."
    assert target.is_dir()


def test_long_answer_with_a_promise_is_not_forced(tmp_path: Path) -> None:
    long_answer = (
        "I will explain the results. " + "The system is healthy. " * 40
    ).strip()
    service, store, client = make_service(
        tmp_path,
        [content_message(long_answer)],
    )

    events = list(
        service.start_turn(
            store.new_conversation_id(),
            "Create a diagnosis report and explain it.",
        )
    )

    assert final_event(events)["reply"] == long_answer
    assert len(client.completions.requests) == 1


def test_action_promise_detection() -> None:
    from agent.chat import ChatService

    assert (
        ChatService._looks_like_action_promise(
            "Let me know if you need anything."
        )
        is False
    )
    assert (
        ChatService._looks_like_action_promise(
            "Let me also know if that helps."
        )
        is False
    )
    assert (
        ChatService._looks_like_action_promise("Let me check the process list.")
        is True
    )
    assert (
        ChatService._looks_like_action_promise(
            "Next, I will create the folder."
        )
        is True
    )


def test_text_tool_call_is_executed_without_leaking_json(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    raw_call = json.dumps(
        {
            "name": "search_system",
            "arguments": {
                "root": str(tmp_path),
                "name_contains": "note",
                "sort_by": "modified_desc",
                "max_results": 1,
            },
        }
    )
    service, store, client = make_service(
        tmp_path,
        [
            SimpleNamespace(content=raw_call, tool_calls=None),
            content_message("I found note.txt."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Find note.txt."))

    final = final_event(events)
    assert final["reply"] == "I found note.txt."
    assert raw_call not in final["reply"]
    history = store.load(conversation_id)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[1]["tool_calls"][0]["function"]["name"] == "search_system"
    assert json.loads(history[2]["content"])["ok"] is True


def test_normalization_fixes_downloads_last_five(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "was sind die 5 letzten Dateien die ich runtergeladen habe? "
        "Im Downloads verzeichnis",
    )

    normalized = service._normalize_search_arguments(
        conversation_id,
        json.dumps(
            {
                "root": "C:\\",
                "all_drives": True,
                "extension": "",
                "include_hidden": True,
                "max_size": 0,
                "min_size": 0,
                "modified_after": "2022-01-01",
                "modified_before": "",
                "name_contains": "",
            }
        ),
    )
    parsed = json.loads(normalized)

    assert parsed["root"] == "downloads"
    assert parsed["all_drives"] is False
    assert parsed["sort_by"] == "modified_desc"
    assert parsed["max_results"] == 5
    assert parsed["include_hidden"] is False
    assert parsed["recursive"] is False
    assert "max_size" not in parsed
    assert "modified_after" not in parsed
    assert "extension" not in parsed


def test_normalization_replaces_missing_placeholder_path(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "Search my Downloads folder for the newest 5 files.",
    )

    normalized = service._normalize_search_arguments(
        conversation_id,
        json.dumps(
            {
                "root": r"C:\Users\YourName\Downloads",
                "all_drives": True,
                "recursive": True,
                "max_results": 5,
            }
        ),
    )
    parsed = json.loads(normalized)

    assert parsed["root"] == "downloads"
    assert parsed["recursive"] is False
    assert parsed["max_results"] == 5
    assert parsed["sort_by"] == "modified_desc"


def test_open_file_asks_for_editor_when_user_did_not_name_one(
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.md"
    target.write_text("hello", encoding="utf-8")

    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "open_file",
                    {"path": str(target), "program": "code"},
                )
            )
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, f"Open {target}"))

    final = final_event(events)
    assert "Which editor or program" in final["reply"]
    assert not any(event["type"] == "action_required" for event in events)
    assert service.get_pending_for_conversation(conversation_id) is None

    history = store.load(conversation_id)
    assert any(
        message.get("role") == "tool"
        and "needs_program" in str(message.get("content", ""))
        for message in history
    )


def test_open_file_with_named_program_requires_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.md"
    target.write_text("hello", encoding="utf-8")

    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "open_file",
                    {"path": str(target), "program": "notepad"},
                )
            )
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(
        service.start_turn(
            conversation_id,
            f"Open {target} with Notepad",
        )
    )

    action = action_event(events)
    assert action["actions"][0]["tool"] == "open_file"
    assert "notepad" in action["actions"][0]["description"].lower()


def test_open_file_program_name_detection() -> None:
    from agent.chat import _program_was_named_by_user

    assert _program_was_named_by_user("Open it with VS Code", "code")
    assert _program_was_named_by_user("use notepad please", "notepad")
    assert _program_was_named_by_user("open in Chrome", "chrome")
    assert not _program_was_named_by_user("just open the file", "code")
    assert not _program_was_named_by_user("open in some editor", "notepad")


def test_normalization_replaces_placeholder_home_for_modifying_tools(
    tmp_path: Path,
) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "create_folder",
            "arguments": json.dumps(
                {"path": r"C:\Users\YourName\Downloads\UIRedesignCheck"}
            ),
        },
    }

    normalized = service._normalize_serialized_call(conversation_id, call)
    parsed = json.loads(normalized["function"]["arguments"])

    assert parsed["path"] == str(
        Path.home() / "Downloads" / "UIRedesignCheck"
    )


def test_normalization_replaces_your_username_placeholder(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "create_file",
            "arguments": json.dumps(
                {
                    "path": r"C:\Users\YourUsername\Downloads\note.txt",
                    "content": "x",
                }
            ),
        },
    }

    normalized = service._normalize_serialized_call(conversation_id, call)
    parsed = json.loads(normalized["function"]["arguments"])

    assert parsed["path"] == str(Path.home() / "Downloads" / "note.txt")
    assert parsed["content"] == "x"


def test_normalization_leaves_real_home_paths_untouched(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    arguments = json.dumps({"path": r"C:\Users\Flo\Downloads\KeepMe"})
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "create_folder", "arguments": arguments},
    }

    normalized = service._normalize_serialized_call(conversation_id, call)

    assert normalized is call


def test_normalization_drops_redundant_folder_name_filter(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    target = tmp_path / "AgentFeatureTest2"
    store.append(
        conversation_id,
        "user",
        f"Use search_system on {target} with extension 'txt', recursive false, "
        "and sort_by 'name'.",
    )

    normalized = service._normalize_search_arguments(
        conversation_id,
        json.dumps(
            {
                "root": str(target),
                "name_contains": "AgentFeatureTest2",
                "extension": "txt",
                "recursive": False,
                "sort_by": "name",
            }
        ),
    )
    parsed = json.loads(normalized)

    assert "name_contains" not in parsed
    assert parsed["extension"] == "txt"
    assert parsed["sort_by"] == "name"
    assert parsed["recursive"] is False


def test_normalization_fixes_documents_last_files(tmp_path: Path) -> None:
    service, store, _client = make_service(tmp_path, [])
    conversation_id = store.new_conversation_id()
    store.append(
        conversation_id,
        "user",
        "lese die letzten dateien auf im Dokumenten ordner",
    )

    normalized = service._normalize_search_arguments(
        conversation_id,
        json.dumps(
            {
                "root": r"C:\Users\Flo\Documents",
                "all_drives": False,
                "extension": "pdf",
                "min_size": 1,
                "max_size": 50_000_000,
                "modified_after": "2022-01-01",
                "modified_before": "2023-01-01",
                "include_hidden": True,
            }
        ),
    )
    parsed = json.loads(normalized)

    assert parsed["root"] == r"C:\Users\Flo\Documents"
    assert parsed["sort_by"] == "modified_desc"
    assert parsed["max_results"] == 5
    assert parsed["recursive"] is False
    assert "extension" not in parsed
    assert "min_size" not in parsed
    assert "max_size" not in parsed
    assert "modified_after" not in parsed
    assert "modified_before" not in parsed


def test_malformed_output_is_recovered_no_raw_json(tmp_path: Path) -> None:
    service, store, client = make_service(
        tmp_path,
        [
            SimpleNamespace(content="{aison", tool_calls=None),
            content_message("Recovered after the reset."),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Find my files."))

    final = final_event(events)
    assert final["reply"] == "Recovered after the reset."
    assert "{aison" not in final["reply"]
    assert client.completions.requests[1]["tool_choice"] == "required"


def test_malformed_output_never_reaches_the_user(tmp_path: Path) -> None:
    service, store, _client = make_service(
        tmp_path,
        [
            SimpleNamespace(content="{aison", tool_calls=None),
            SimpleNamespace(content="{aison", tool_calls=None),
            SimpleNamespace(content="{aison", tool_calls=None),
        ],
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Find my files."))

    assert not any(event["type"] == "final" for event in events)
    assert any(event["type"] == "error" for event in events)
    assert store.load(conversation_id) == []


def test_unlimited_tool_rounds_support_more_than_one_hundred_steps(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    rounds = 105
    messages = [
        tool_calls_message(
            tool_call(
                "read_file_content",
                {"path": str(note)},
                f"call_{index}",
            )
        )
        for index in range(rounds)
    ]
    messages.append(content_message(f"All {rounds} reads completed."))
    service, store, client = make_service(
        tmp_path,
        messages,
        max_iterations=0,
    )

    events = list(
        service.start_turn(store.new_conversation_id(), "Keep reading.")
    )

    assert final_event(events)["reply"] == f"All {rounds} reads completed."
    assert len(client.completions.requests) == rounds + 1


def test_diagnosis_artifact_paths_are_hidden_from_the_model(
    tmp_path: Path,
) -> None:
    service, _, _ = make_service(tmp_path, [content_message("ok")])
    raw = json.dumps(
        {
            "ok": True,
            "health_score": 80,
            "charts": [{"path": "C:\\tmp\\01_cpu.png", "title": "CPU"}],
            "files": ["C:\\tmp\\01_cpu.png"],
            "report_dir": "C:\\tmp",
            "report_file": "C:\\tmp\\report.json",
            "summary": {"cpu": {"name": "Test CPU"}},
        }
    )

    filtered = json.loads(service._model_facing_diagnosis(raw))

    assert "charts" not in filtered
    assert "files" not in filtered
    assert "report_dir" not in filtered
    assert "report_file" not in filtered
    assert filtered["summary"]["cpu"]["name"] == "Test CPU"


def test_gui_click_pauses_with_the_exact_hands_off_message(
    tmp_path: Path,
) -> None:
    class FakeAutomation:
        def __init__(self) -> None:
            self.clicks: list[tuple[str, int, str]] = []

        def click(
            self,
            snapshot_id: str,
            element_id: int,
            element_name: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.clicks.append((snapshot_id, element_id, element_name))
            return {"ok": True, "message": "Clicked."}

        def reset(self) -> None:
            return None

        def status(self) -> dict[str, Any]:
            return {}

    automation = FakeAutomation()
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
    executor = ToolExecutor(
        settings.tools,
        automation_service=automation,  # type: ignore[arg-type]
    )
    client = ScriptedClient(
        [
            tool_calls_message(
                tool_call(
                    "click_element",
                    {
                        "snapshot_id": "som_1",
                        "element_id": 7,
                        "element_name": "New Email",
                    },
                )
            ),
            content_message("The New Email button was clicked."),
        ]
    )
    service = ChatService(settings, store, knowledge, executor, client=client)
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Click New Email."))
    action = action_event(events)

    assert action["actions"][0]["tool"] == "click_element"
    assert (
        "take your hands off the mouse and keyboard"
        in action["actions"][0]["description"]
    )
    assert automation.clicks == []

    resumed = list(service.resume(action["approval_id"], True))

    assert final_event(resumed)["reply"] == "The New Email button was clicked."
    assert automation.clicks == [("som_1", 7, "New Email")]


def test_successful_multistep_turn_saves_a_workflow_fact(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("search_system", {"root": str(tmp_path)}, "call_1"),
                tool_call("read_file_content", {"path": str(note)}, "call_2"),
                tool_call("get_open_windows", {}, "call_3"),
            ),
            content_message("Done."),
        ],
        max_iterations=10,
    )

    events = list(
        service.start_turn(
            store.new_conversation_id(),
            "Inspect the workspace and the desktop.",
        )
    )

    assert final_event(events)["reply"] == "Done."
    knowledge = service._knowledge.read()
    assert "Workflow:" in knowledge
    assert "search_system" in knowledge
    assert "get_open_windows" in knowledge


def test_cli_failure_then_success_saves_a_recovery_fact(
    tmp_path: Path,
) -> None:
    service, store, _client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call(
                    "run_powershell_action",
                    {"command": "exit 1", "description": "intentional failure"},
                    "call_1",
                )
            ),
            tool_calls_message(
                tool_call(
                    "run_powershell_query",
                    {"command": "Write-Output ok"},
                    "call_2",
                )
            ),
            content_message("Recovered with a read-only check."),
        ],
        max_iterations=5,
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Try the command."))
    action = action_event(events)
    resumed = list(service.resume(action["approval_id"], True))

    assert final_event(resumed)["reply"] == "Recovered with a read-only check."
    knowledge = service._knowledge.read()
    assert "Learned recovery" in knowledge
    assert "PowerShell" in knowledge


def test_read_only_loop_summarizes_at_max_rounds(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    service, store, client = make_service(
        tmp_path,
        [
            tool_calls_message(
                tool_call("read_file_content", {"path": str(note)}, "call_1")
            ),
            tool_calls_message(
                tool_call("read_file_content", {"path": str(note)}, "call_2")
            ),
            content_message(
                "I reached the tool limit. I verified the note exists and read it."
            ),
        ],
        max_iterations=1,
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Keep reading."))

    assert not any(event["type"] == "error" for event in events)
    assert final_event(events)["reply"] == (
        "I reached the tool limit. I verified the note exists and read it."
    )
    history = store.load(conversation_id)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len(client.completions.requests) == 3
    assert client.completions.requests[-1]["tools"] is None
