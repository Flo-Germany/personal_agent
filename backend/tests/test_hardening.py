"""Regression tests for lifecycle, resource and async hardening."""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.chat import ChatService
from engine.catalog import ModelEntry, ModelFile
from engine.downloads import InstallJob, ModelStore
from main import PARENT_PID_ENV, _parent_pid, _process_is_alive
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import MemorySettings, Settings


def _entry(tmp_path: Path, payload: bytes = b"") -> tuple[ModelEntry, ModelFile]:
    item = ModelFile(
        role="model",
        repo="test/repo",
        filename="model.gguf",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        url_override="http://127.0.0.1:9/model.gguf",
    )
    entry = ModelEntry(
        id="test-model",
        name="Test",
        task="text",
        family="test",
        parameters="1B",
        quantization="q4",
        badge="test",
        min_vram_gb=0.0,
        description="test",
        context=2048,
        files=(item,),
    )
    return entry, item


def test_finished_install_jobs_are_pruned(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)

    active = InstallJob(id="active", model_id="m")
    active.state = "downloading"
    store._jobs[active.id] = active

    for index in range(30):
        job = InstallJob(id=f"done-{index}", model_id="m")
        job.state = "done"
        store._jobs[job.id] = job

    store._prune_finished_jobs()

    finished = [job for job in store._jobs.values() if job.state == "done"]
    assert len(finished) == 20
    assert "active" in store._jobs
    # The newest results stay queryable for the UI.
    assert "done-29" in store._jobs
    assert "done-0" not in store._jobs


def test_download_416_resets_the_partial_file_without_recursion(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = ModelStore(tmp_path)
    payload = b"data"
    entry, item = _entry(tmp_path, payload)
    target = store._file_path(entry, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    # Smaller than the remote file, so the 416 branch (not the size guard) runs.
    partial.write_bytes(b"st")

    calls = {"count": 0}

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, _size: int = -1) -> bytes:
            body, self._body = self._body, b""
            return body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                item.url, 416, "Range Not Satisfiable", {}, None
            )
        return FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    job = InstallJob(id="job", model_id=entry.id, total=len(payload))
    store._fetch(entry, item, job)

    assert calls["count"] == 2
    assert target.read_bytes() == payload
    assert not partial.exists()


def test_parent_pid_parsing(monkeypatch: Any) -> None:
    monkeypatch.delenv(PARENT_PID_ENV, raising=False)
    assert _parent_pid() is None

    monkeypatch.setenv(PARENT_PID_ENV, "not-a-pid")
    assert _parent_pid() is None

    monkeypatch.setenv(PARENT_PID_ENV, "0")
    assert _parent_pid() is None

    monkeypatch.setenv(PARENT_PID_ENV, str(os.getpid()))
    assert _parent_pid() == os.getpid()


def test_process_probe_never_reports_a_live_process_as_dead() -> None:
    assert _process_is_alive(os.getpid()) is True
    assert _process_is_alive(2**31 - 1) is False


def _service(tmp_path: Path) -> ChatService:
    settings = Settings(
        agent_name="TestBot",
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
    )
    return ChatService(
        settings,
        ConversationStore(max_conversations=5),
        KnowledgeBase(tmp_path / "knowledge.md"),
        tools=SimpleNamespace(),
    )


def test_forget_conversation_drops_idle_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation_id = "00000000-0000-0000-0000-000000000001"

    service._lock_for(conversation_id)
    service._cancel_event(conversation_id)
    assert conversation_id in service._locks
    assert conversation_id in service._cancel_events

    service.forget_conversation(conversation_id)

    assert conversation_id not in service._locks
    assert conversation_id not in service._cancel_events


def test_forget_conversation_keeps_an_active_turn(tmp_path: Path) -> None:
    service = _service(tmp_path)
    conversation_id = "00000000-0000-0000-0000-000000000002"

    service._lock_for(conversation_id)
    service._active_conversations.add(conversation_id)

    service.forget_conversation(conversation_id)

    assert conversation_id in service._locks


def test_tool_bug_is_not_reported_as_bad_arguments(tmp_path: Path) -> None:
    """A TypeError raised inside a tool is a tool failure, not a bad call."""
    from settings import ToolSettings
    from tools.executor import ToolExecutor

    executor = ToolExecutor(ToolSettings())

    def broken(path: str) -> dict[str, Any]:
        raise TypeError("boom inside the tool")

    executor._handlers["create_folder"] = broken

    result = executor.execute("create_folder", {"path": "x"})

    assert result["ok"] is False
    assert "Invalid arguments" not in str(result["error"])
    assert str(result["error"]).startswith("create_folder failed")


def test_cancel_during_install_is_not_reported_as_a_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from engine.downloads import _Cancelled

    store = ModelStore(tmp_path)
    entry, _item = _entry(tmp_path, b"payload")
    job = InstallJob(id="job", model_id=entry.id, total=7)

    def cancel(*_args: Any, **_kwargs: Any) -> None:
        raise _Cancelled()

    monkeypatch.setattr(store, "_fetch", cancel)

    store._run_install(entry, job)

    assert job.state == "cancelled"
    assert job.message == "Cancelled"
    assert job.error == ""


def test_partial_listener_start_is_rolled_back(monkeypatch: Any) -> None:
    """A half-installed global hook must not outlive a failed start."""
    import sys as _sys

    from tools import input_failsafe as mod

    stopped: list[Any] = []

    class FakeListener:
        daemon = False

        def __init__(self, **_kwargs: Any) -> None:
            self.fail = False

        def start(self) -> None:
            if self.fail:
                raise OSError("hook denied")

        def stop(self) -> None:
            stopped.append(self)

    mouse_listener = FakeListener()
    keyboard_listener = FakeListener()
    keyboard_listener.fail = True

    monkeypatch.setitem(
        _sys.modules,
        "pynput",
        SimpleNamespace(
            mouse=SimpleNamespace(Listener=lambda **_kwargs: mouse_listener),
            keyboard=SimpleNamespace(Listener=lambda **_kwargs: keyboard_listener),
        ),
    )

    guard = mod.InputFailsafe()
    guard.start()

    assert mouse_listener in stopped
    assert guard.status()["listeners_started"] is False

def test_shutdown_cancels_pending_learning_timers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.schedule_learning(
        "00000000-0000-0000-0000-000000000003",
        "My name is Alex and I live in Berlin.",
    )
    timers = list(service._learning_timers.values())
    assert timers, "learning should have been scheduled"

    service.shutdown()

    assert service._learning_timers == {}
    assert all(timer.finished.wait(1.0) for timer in timers)

