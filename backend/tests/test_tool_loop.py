"""Tests for HITL pauses, approvals, denials, and streaming events."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.chat import ChatService, ConfirmationRequired, LLMError
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


def tool_calls_message(*calls: Any, content: str = "") -> Any:
    return SimpleNamespace(content=content, tool_calls=list(calls))


def content_message(content: str) -> Any:
    return SimpleNamespace(content=content, tool_calls=None)


def make_service(
    tmp_path: Path,
    messages: list[Any],
    max_iterations: int = 3,
) -> tuple[ChatService, ConversationStore, ScriptedClient]:
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
    client = ScriptedClient(messages)
    service = ChatService(settings, store, knowledge, executor, client=client)
    return service, store, client


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
    assert not first.exists() and not second.exists()

    resumed = list(service.resume(action["approval_id"], True))

    assert first.is_dir() and second.is_dir()
    assert final_event(resumed)["reply"] == "Both folders are ready."
    tool_messages = [m for m in store.load(conversation_id) if m["role"] == "tool"]
    assert len(tool_messages) == 2


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


def test_read_only_loop_hits_max_rounds_and_rolls_back(tmp_path: Path) -> None:
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
        ],
        max_iterations=1,
    )
    conversation_id = store.new_conversation_id()

    events = list(service.start_turn(conversation_id, "Keep reading."))

    assert any(event["type"] == "error" for event in events)
    assert store.load(conversation_id) == []
    assert len(client.completions.requests) == 2
