"""Phase 19 end-to-end scenario matrix.

Run from the repository root:

    backend/.venv/Scripts/python.exe backend/tests/e2e_agent_tests.py

Use ``--skip-gui`` on a headless machine, or ``--scenario NAME`` to run one
scenario. The GUI scenarios use real WinForms mocks and real Windows UI
Automation; they never touch the user's real documents or email.

The file is intentionally not named ``test_*.py``: it is a long-running
acceptance harness, not a unit-test module. The pytest suite still covers the
same helpers individually.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.chat import ChatService  # noqa: E402
from agent.self_improvement import SelfImprovementService  # noqa: E402
from memory.knowledge import KnowledgeBase  # noqa: E402
from memory.store import ConversationStore  # noqa: E402
from settings import (  # noqa: E402
    AgentSettings,
    AutomationSettings,
    MemorySettings,
    OllamaSettings,
    Settings,
)
from tools.executor import ToolExecutor  # noqa: E402
from tools.gui_automation import GUIAutomationService  # noqa: E402
from tools.som_vision import SOMSnapshot  # noqa: E402


# ---------------------------------------------------------------- chat harness
class ScriptedCompletions:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = list(messages)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self.messages:
            raise AssertionError("The scripted model ran out of messages.")
        message = self.messages.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedClient:
    def __init__(self, messages: list[Any]) -> None:
        self.completions = ScriptedCompletions(messages)
        self.chat = SimpleNamespace(completions=self.completions)


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call") -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def tool_calls_message(*calls: Any, content: str = "") -> Any:
    return SimpleNamespace(content=content, tool_calls=list(calls))


def content_message(content: str) -> Any:
    return SimpleNamespace(content=content, tool_calls=None)


def make_chat(
    tmp_path: Path,
    messages: list[Any],
    *,
    automation_service: Any | None = None,
) -> tuple[ChatService, ConversationStore, KnowledgeBase]:
    settings = Settings(
        agent_name="E2E",
        personality="Deterministic acceptance harness.",
        personality_file=tmp_path / "missing-personality.md",
        system_prompt="Run the requested scenario safely.",
        ollama=OllamaSettings(model="hermes3", max_retries=0),
        agent=AgentSettings(
            max_iterations=0,
            self_improvement_enabled=True,
            self_improvement_workflow_min_tools=3,
        ),
        memory=MemorySettings(
            knowledge_base_path=tmp_path / "knowledge.md",
            learn_from_chats=False,
        ),
    )
    store = ConversationStore()
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    knowledge.ensure_file()
    executor = ToolExecutor(
        settings.tools,
        automation=settings.automation,
        automation_service=automation_service,
    )
    service = ChatService(
        settings,
        store,
        knowledge,
        executor,
        client=ScriptedClient(messages),
    )
    return service, store, knowledge


def run_turn(
    service: ChatService,
    store: ConversationStore,
    message: str,
) -> tuple[str, list[dict[str, Any]]]:
    conversation_id = store.new_conversation_id()
    events = list(service.start_turn(conversation_id, message))
    consumed: set[str] = set()
    while True:
        action = next(
            (
                event
                for event in reversed(events)
                if event.get("type") == "action_required"
                and str(event.get("approval_id")) not in consumed
            ),
            None,
        )
        if action is None:
            break
        consumed.add(str(action["approval_id"]))
        events.extend(service.resume(action["approval_id"], True))
    return conversation_id, events


def final_reply(events: list[dict[str, Any]]) -> str:
    final = next(
        (event for event in reversed(events) if event.get("type") == "final"),
        None,
    )
    if final is None:
        raise AssertionError(f"No final event. Events: {events[-5:]}")
    return str(final.get("reply") or "")


def tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == "tool_result"]


def assert_no_error(events: list[dict[str, Any]]) -> None:
    errors = [event for event in events if event.get("type") == "error"]
    if errors:
        raise AssertionError(f"Agent error: {errors[-1].get('message')}")


# ------------------------------------------------------------------ scenarios
def scenario_filesystem(tmp: Path) -> str:
    workspace = tmp / "matrix_fs"
    archive = workspace / "Archive"
    workspace.mkdir(parents=True)
    files = []
    for name, content in (
        ("quarterly report.txt", "numbers"),
        ("vacation photo.jpg", "pixels"),
        ("meeting notes.md", "# notes"),
    ):
        path = workspace / name
        path.write_text(content, encoding="utf-8")
        files.append(path)

    calls = [tool_call("create_folder", {"path": str(archive)}, "c0")]
    calls += [
        tool_call(
            "move_file_or_folder",
            {
                "source_path": str(path),
                "destination_path": str(archive / path.name),
            },
            f"c{index + 1}",
        )
        for index, path in enumerate(files)
    ]
    service, store, knowledge = make_chat(
        tmp,
        [
            tool_calls_message(*calls),
            content_message("Moved all three files into the Archive folder."),
        ],
    )

    _cid, events = run_turn(
        service,
        store,
        "Restructure the workspace: put all three files into an Archive folder.",
    )
    assert_no_error(events)
    assert final_reply(events)

    assert archive.is_dir()
    for path in files:
        assert not path.exists(), f"original still exists: {path}"
        assert (archive / path.name).is_file(), f"missing moved file: {path.name}"

    text = knowledge.read()
    assert "Workflow:" in text, "the successful file workflow was not learned"
    return f"moved {len(files)} files; workflow fact saved"


def scenario_ide_configuration(tmp: Path) -> str:
    directory = tmp / "matrix_ide"
    directory.mkdir()
    settings_path = directory / "settings.json"
    settings_path.write_text(
        json.dumps({"editor.fontSize": 14, "files.autoSave": "off"}),
        encoding="utf-8",
    )
    updated = {
        "editor.fontSize": 16,
        "editor.formatOnSave": True,
        "files.autoSave": "afterDelay",
    }

    service, store, knowledge = make_chat(
        tmp,
        [
            tool_calls_message(
                tool_call(
                    "read_file_content",
                    {"path": str(settings_path)},
                    "c1",
                )
            ),
            tool_calls_message(
                tool_call(
                    "create_file",
                    {
                        "path": str(settings_path),
                        "content": json.dumps(updated, indent=2),
                        "overwrite": True,
                    },
                    "c2",
                )
            ),
            tool_calls_message(
                tool_call(
                    "read_file_content",
                    {"path": str(settings_path)},
                    "c3",
                )
            ),
            content_message("Updated and verified the IDE settings."),
        ],
    )

    _cid, events = run_turn(
        service,
        store,
        "Update the IDE settings: font size 16, format on save, autosave after delay.",
    )
    assert_no_error(events)
    assert final_reply(events)

    parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    assert parsed == updated, parsed
    assert "Workflow:" in knowledge.read()
    return "settings.json updated and verified; workflow fact saved"


def scenario_system_settings(tmp: Path) -> str:
    key = rf"HKCU:\Software\CompaE2E\{uuid.uuid4().hex[:10]}"
    create_command = (
        f"New-Item -Path '{key}' -Force | Out-Null; "
        f"Set-ItemProperty -Path '{key}' -Name 'Theme' -Value 'Dark'"
    )
    read_command = f"(Get-ItemProperty -Path '{key}' -Name 'Theme').Theme"
    cleanup_command = f"Remove-Item -Path '{key}' -Recurse -Force"
    verify_command = f"Test-Path '{key}'"

    service, store, knowledge = make_chat(
        tmp,
        [
            tool_calls_message(
                tool_call(
                    "run_powershell_action",
                    {
                        "command": create_command,
                        "description": "Create a scoped test settings key",
                    },
                    "c1",
                )
            ),
            tool_calls_message(
                tool_call("run_powershell_query", {"command": read_command}, "c2")
            ),
            tool_calls_message(
                tool_call(
                    "run_powershell_action",
                    {
                        "command": cleanup_command,
                        "description": "Remove the scoped test settings key",
                    },
                    "c3",
                )
            ),
            tool_calls_message(
                tool_call("run_powershell_query", {"command": verify_command}, "c4")
            ),
            content_message("Created, read, and removed the scoped test setting."),
        ],
    )

    try:
        _cid, events = run_turn(
            service,
            store,
            "Exercise a deep Windows setting with a temporary HKCU key.",
        )
        assert_no_error(events)
        assert final_reply(events)

        results = tool_results(events)
        outputs = [str(item.get("result", {}).get("stdout") or "") for item in results]
        assert any("Dark" in output for output in outputs), outputs
        assert any(
            "false" in output.casefold() for output in outputs
        ), outputs

        check = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", verify_command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert "false" in (check.stdout or "").casefold()
        assert "Workflow:" in knowledge.read()
        return "HKCU test key created, read, removed, and verified"
    finally:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Remove-Item -Path '{key}' -Recurse -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def scenario_ambiguous_request(tmp: Path) -> str:
    workspace = tmp / "matrix_ambiguous"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_text("do not touch", encoding="utf-8")

    service, store, knowledge = make_chat(
        tmp,
        [
            content_message(
                "Which folder should I clean up, and should I move the files "
                "or delete them? I do not want to guess."
            )
        ],
    )

    _cid, events = run_turn(service, store, "Clean up my computer.")
    assert_no_error(events)
    assert not any(event.get("type") == "action_required" for event in events)
    reply = final_reply(events)
    assert "?" in reply
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert "Workflow:" not in knowledge.read()
    return "agent asked for clarification without touching files"


# --------------------------------------------------------------- GUI helpers
class NameGrounder:
    """Deterministic stand-in for the vision model in GUI scenarios."""

    _TARGETS = (
        ("new email", "New Email", "Button"),
        ("show popup", "Show Popup", "Button"),
        ("resize window", "Resize Window", "Button"),
        ("note", "Note", "Edit"),
        ("save", "Save", "Button"),
        ("subject", "Subject", "Edit"),
        ("to field", "To", "Edit"),
        ("ok button", "OK", "Button"),
        ("ok", "OK", "Button"),
    )

    def __call__(
        self,
        question: str,
        snapshot: SOMSnapshot,
        image_b64: str,
    ) -> dict[str, Any]:
        intent = " ".join(question.casefold().split())
        wanted, preferred = "", ""
        for marker, name, control_type in self._TARGETS:
            if marker in intent:
                wanted, preferred = name, control_type
                break

        candidates = []
        for element in snapshot.elements:
            name = " ".join(element.name.casefold().split())
            if wanted and name != wanted.casefold() and wanted.casefold() not in name:
                continue
            score = 10 if element.control_type == preferred else 0
            if wanted and name == wanted.casefold():
                score += 10
            candidates.append((score, element))
        if not candidates:
            return {"id": 0, "reason": "no matching element", "confidence": 0.0}
        candidates.sort(key=lambda item: (item[0], -item[1].id), reverse=True)
        score, element = candidates[0]
        return {
            "id": element.id,
            "reason": f"matched {element.name!r}",
            "confidence": min(0.99, 0.6 + score / 100),
        }


def wait_for_window(title: str, timeout: float = 15.0) -> bool:
    import pygetwindow as gw

    deadline = time.time() + timeout
    while time.time() < deadline:
        if gw.getWindowsWithTitle(title):
            return True
        time.sleep(0.2)
    return False


def launch_mock(script_name: str, title: str, env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_extra)
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path(__file__).with_name(script_name)),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not wait_for_window(title):
        process.terminate()
        raise AssertionError(f"{title} did not appear")
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
    users = []
    try:
        import pygetwindow as gw

        users = [
            window
            for window in gw.getAllWindows()
            if "Mock" in str(window.title)
        ]
    except Exception:  # noqa: BLE001
        users = []
    for window in users:
        try:
            window.close()
        except Exception:  # noqa: BLE001
            pass


def gui_service() -> GUIAutomationService:
    return GUIAutomationService(
        AutomationSettings(
            enabled=True,
            move_duration_seconds=0.35,
            highlight_seconds=0.15,
            arm_delay_seconds=0.5,
            type_interval_seconds=0.005,
            screenshot_max_width=1600,
            max_elements=80,
        ),
        vision=None,
        grounder=NameGrounder(),
    )


def find_element(snapshot_id: str, name: str, service: GUIAutomationService):
    snapshot = service.store.get(snapshot_id)
    if snapshot is None:
        return None
    for element in snapshot.elements:
        if " ".join(element.name.casefold().split()) == name.casefold():
            return element
    return None


def analyze_named(
    service: GUIAutomationService,
    question: str,
    name: str,
    window_title: str,
) -> tuple[str, Any]:
    result = service.analyze(question, window_title=window_title)
    if not result.get("ok"):
        raise AssertionError(f"analyze failed: {result}")
    snapshot_id = result["snapshot_id"]
    element = find_element(snapshot_id, name, service)
    if element is None:
        names = [
            item.get("name")
            for item in result.get("elements", [])
            if item.get("type") in {"Button", "Edit", "Document"}
        ]
        raise AssertionError(f"{name!r} not found in {window_title}: {names}")
    return snapshot_id, element


def click_named(
    service: GUIAutomationService,
    question: str,
    name: str,
    window_title: str,
) -> dict[str, Any]:
    snapshot_id, element = analyze_named(service, question, name, window_title)
    return service.click(snapshot_id, element.id, element.name)


def scenario_gui_multi_app(tmp: Path) -> str:
    notes_result = tmp / "notes_result.txt"
    popup_result = tmp / "popup_result.txt"
    notes = launch_mock(
        "mock_notes.ps1",
        "Notes - Mock",
        {
            "MOCK_NOTES_RESULT": str(notes_result),
            "MOCK_NOTES_POPUP_RESULT": str(popup_result),
        },
    )
    outlook = None
    service = gui_service()
    try:
        analysis = service.analyze(
            "Find the Note field",
            window_title="Notes - Mock",
        )
        assert analysis.get("ok"), analysis
        note = find_element(analysis["snapshot_id"], "Note", service)
        assert note is not None
        clicked = service.click(
            analysis["snapshot_id"],
            note.id,
            note.name,
        )
        assert clicked.get("ok"), clicked
        typed = service.type_text("Context check from the E2E matrix.")
        assert typed.get("ok"), typed

        saved = click_named(
            service,
            "Find the Save button",
            "Save",
            "Notes - Mock",
        )
        assert saved.get("ok"), saved
        deadline = time.time() + 5
        while time.time() < deadline and not notes_result.exists():
            time.sleep(0.1)
        assert notes_result.exists(), "Notes mock did not save"
        assert "Context check" in notes_result.read_text(encoding="utf-8-sig")
        stop_process(notes)
        notes = None

        outlook = launch_mock("mock_outlook.ps1", "Outlook - Mock", {})
        clicked_new = click_named(
            service,
            "Find the New Email button",
            "New Email",
            "Outlook - Mock",
        )
        assert clicked_new.get("ok"), clicked_new
        compose = service.analyze(
            "Find the To field",
            window_title="Outlook - Mock",
        )
        assert compose.get("ok"), compose
        assert any(
            item.get("name") == "To"
            for item in compose.get("elements", [])
        ), "compose fields did not appear"
        return "typed into Notes, saved it, then navigated the Outlook mock"
    finally:
        service.guard.stop()
        stop_process(notes)
        stop_process(outlook)


def scenario_gui_popup(tmp: Path) -> str:
    notes_result = tmp / "notes_popup_result.txt"
    popup_result = tmp / "popup_ok.txt"
    notes = launch_mock(
        "mock_notes.ps1",
        "Notes - Mock",
        {
            "MOCK_NOTES_RESULT": str(notes_result),
            "MOCK_NOTES_POPUP_RESULT": str(popup_result),
        },
    )
    service = gui_service()
    try:
        snapshot_id, popup_button = analyze_named(
            service,
            "Find the Show Popup button",
            "Show Popup",
            "Notes - Mock",
        )
        opened = service.click(
            snapshot_id,
            popup_button.id,
            popup_button.name,
        )
        assert opened.get("ok"), opened
        assert wait_for_window("Compa Popup", 8), "popup did not appear"

        # A stale click on the now-disabled owner must be blocked.
        stale = service.click(snapshot_id, popup_button.id, popup_button.name)
        assert stale.get("ok") is False, stale
        assert stale.get("context_changed") or stale.get("element_missing"), stale

        popup_id, ok_button = analyze_named(
            service,
            "Find the OK button",
            "OK",
            "Compa Popup",
        )
        closed = service.click(
            popup_id,
            ok_button.id,
            ok_button.name,
        )
        assert closed.get("ok"), closed

        deadline = time.time() + 5
        while time.time() < deadline and not popup_result.exists():
            time.sleep(0.1)
        assert popup_result.exists(), "popup OK was not recorded"
        assert popup_result.read_text(encoding="utf-8-sig").strip() == "popup-ok"
        return "unexpected popup detected, stale click blocked, OK clicked"
    finally:
        service.guard.stop()
        stop_process(notes)


def scenario_gui_dynamic_resize(tmp: Path) -> str:
    notes_result = tmp / "notes_resize_result.txt"
    notes = launch_mock(
        "mock_notes.ps1",
        "Notes - Mock",
        {
            "MOCK_NOTES_RESULT": str(notes_result),
            "MOCK_NOTES_POPUP_RESULT": str(tmp / "unused_popup.txt"),
        },
    )
    service = gui_service()
    try:
        old_id, old_note = analyze_named(
            service,
            "Find the Note field",
            "Note",
            "Notes - Mock",
        )
        resize_id, resize_button = analyze_named(
            service,
            "Find the Resize Window button",
            "Resize Window",
            "Notes - Mock",
        )
        resized = service.click(
            resize_id,
            resize_button.id,
            resize_button.name,
        )
        assert resized.get("ok"), resized
        time.sleep(0.4)

        stale = service.click(old_id, old_note.id, old_note.name)
        assert stale.get("ok") is False, stale
        assert stale.get("context_changed"), stale

        new_id, new_note = analyze_named(
            service,
            "Find the Note field",
            "Note",
            "Notes - Mock",
        )
        clicked = service.click(new_id, new_note.id, new_note.name)
        assert clicked.get("ok"), clicked
        typed = service.type_text("Resized window recovery.")
        assert typed.get("ok"), typed
        return "stale coordinates blocked after resize; fresh snapshot recovered"
    finally:
        service.guard.stop()
        stop_process(notes)


def scenario_gui_missing_element(tmp: Path) -> str:
    notes = launch_mock(
        "mock_notes.ps1",
        "Notes - Mock",
        {
            "MOCK_NOTES_RESULT": str(tmp / "notes_missing.txt"),
            "MOCK_NOTES_POPUP_RESULT": str(tmp / "unused_popup2.txt"),
        },
    )
    service = gui_service()
    knowledge = KnowledgeBase(tmp / "knowledge_gui.md")
    knowledge.ensure_file()
    improvement = SelfImprovementService(knowledge)
    improvement.begin_turn("gui-missing")
    try:
        snapshot_id, note = analyze_named(
            service,
            "Find the Note field",
            "Note",
            "Notes - Mock",
        )
        stop_process(notes)
        notes = None
        time.sleep(0.4)

        failed = service.click(snapshot_id, note.id, note.name)
        assert failed.get("ok") is False, failed
        assert failed.get("element_missing") or failed.get("context_changed"), failed
        improvement.record_tool_result(
            "gui-missing",
            "click_element",
            {"element_name": note.name, "element_id": note.id},
            failed,
        )

        # Relaunch and re-analyze: this is the successful recovery.
        notes = launch_mock(
            "mock_notes.ps1",
            "Notes - Mock",
            {
                "MOCK_NOTES_RESULT": str(tmp / "notes_missing.txt"),
                "MOCK_NOTES_POPUP_RESULT": str(tmp / "unused_popup2.txt"),
            },
        )
        recovered = service.analyze(
            "Find the Note field",
            window_title="Notes - Mock",
        )
        assert recovered.get("ok"), recovered
        improvement.record_tool_result(
            "gui-missing",
            "analyze_screen_ui",
            {
                "question": "Find the Note field",
                "window_title": "Notes - Mock",
            },
            recovered,
        )
        improvement.finish_turn(
            "gui-missing",
            "Type into the note",
            "Recovered.",
            success=True,
        )
        assert "Learned recovery" in knowledge.read()
        return "missing element blocked; relaunch/re-analysis recovery was learned"
    finally:
        service.guard.stop()
        stop_process(notes)


# ------------------------------------------------------------------ reporting
@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float


SCENARIOS: tuple[tuple[str, Callable[[Path], str], bool], ...] = (
    ("filesystem_restructure", scenario_filesystem, False),
    ("ide_configuration", scenario_ide_configuration, False),
    ("system_settings", scenario_system_settings, False),
    ("ambiguous_request", scenario_ambiguous_request, False),
    ("gui_multi_app", scenario_gui_multi_app, True),
    ("gui_popup", scenario_gui_popup, True),
    ("gui_dynamic_resize", scenario_gui_dynamic_resize, True),
    ("gui_missing_element", scenario_gui_missing_element, True),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    base = Path(
        os.environ.get("TEMP", str(Path.home()))
    ) / f"compa_e2e_{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    selected = set(args.scenario)
    results: list[Result] = []

    print(f"Phase 19 scenario matrix: {base}")
    for name, function, is_gui in SCENARIOS:
        if selected and name not in selected:
            continue
        if is_gui and (args.skip_gui or os.name != "nt"):
            print(f"[SKIP] {name} (GUI disabled)")
            continue
        scenario_dir = base / name
        scenario_dir.mkdir()
        started = time.monotonic()
        try:
            detail = function(scenario_dir)
            results.append(
                Result(name, True, detail, time.monotonic() - started)
            )
            print(f"[PASS] {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 - acceptance harness
            detail = f"{exc}\n{traceback.format_exc(limit=6)}"
            results.append(
                Result(name, False, detail, time.monotonic() - started)
            )
            print(f"[FAIL] {name}: {exc}")

    print("\nSummary")
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"  {status:4} {result.name:26} {result.seconds:6.1f}s  {result.detail[:90]}")
    print(
        f"\n{sum(1 for result in results if result.ok)}/{len(results)} scenarios passed"
    )

    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f"Kept artifacts: {base}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
