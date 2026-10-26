"""Phase 22.5: llama.cpp vision transport and runtime engine switching."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.catalog import get_entry
from engine.runtime import RuntimeStatus
from engine.service import EngineService
from settings import EngineSettings, VisionSettings
from tools.vision_tools import VisionService


class FakeEngine:
    def __init__(self, engine_type: str = "llamacpp") -> None:
        self.settings = SimpleNamespace(type=engine_type)
        self.ensure_calls = 0
        self.releases = 0

    def ensure_vision(self) -> str:
        self.ensure_calls += 1
        return "http://127.0.0.1:8124/v1"

    def release_vision(self) -> None:
        self.releases += 1


def make_vision(tmp_path: Path, engine: Any) -> VisionService:
    settings = VisionSettings(
        enabled=False,
        index_path=tmp_path / "index.sqlite3",
        scan_roots=[str(tmp_path)],
    )
    return VisionService(
        settings,
        embedder=object(),  # type: ignore[arg-type] - unused by the transport
        vision_engine=engine,
    )


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_native_vision_uses_openai_image_url(monkeypatch, tmp_path: Path) -> None:
    engine = FakeEngine()
    service = make_vision(tmp_path, engine)
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> _Response:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _Response(
            {"choices": [{"message": {"content": "A browser window."}}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    answer = service._ask_vision_model(
        "What is shown?",
        "AAAA",
        instruction="Answer briefly.",
        max_tokens=64,
    )

    assert answer == "A browser window."
    assert captured["url"].endswith("/v1/chat/completions")
    payload = captured["body"]
    assert payload["max_tokens"] == 64
    assert "images" not in json.dumps(payload)
    parts = payload["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "What is shown?\n\nAnswer briefly."}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert engine.ensure_calls == 1


def test_ollama_vision_keeps_native_images_payload(monkeypatch, tmp_path: Path) -> None:
    engine = FakeEngine("ollama")
    service = make_vision(tmp_path, engine)
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> _Response:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _Response({"message": {"content": "Ollama answer"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    answer = service._ask_vision_model("What is shown?", "AAAA")

    assert answer == "Ollama answer"
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["messages"][-1]["images"] == ["AAAA"]
    assert engine.ensure_calls == 0


def test_release_only_unloads_the_native_engine(tmp_path: Path) -> None:
    native = FakeEngine("llamacpp")
    make_vision(tmp_path, native).release_native_vision()
    assert native.releases == 1

    ollama = FakeEngine("ollama")
    make_vision(tmp_path, ollama).release_native_vision()
    assert ollama.releases == 0


def test_native_vision_reports_missing_model(monkeypatch, tmp_path: Path) -> None:
    engine = FakeEngine()

    def fail() -> str:
        raise ValueError("No vision model is installed and selected.")

    engine.ensure_vision = fail  # type: ignore[assignment]
    service = make_vision(tmp_path, engine)

    with pytest.raises(RuntimeError) as exc:
        service._ask_vision_model("What is shown?", "AAAA")
    assert "Install and select a vision model" in str(exc.value)


# ------------------------------------------------------------- engine service
class FakeServers:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.ensured: list[Any] = []

    def status(self, role: str | None = None) -> Any:
        return {"role": role, "running": False} if role else {"roles": []}

    def port_for(self, role: str) -> int:
        return 8124 if role == "vision" else 8123

    def base_url(self, role: str) -> str:
        return f"http://127.0.0.1:{self.port_for(role)}/v1"

    def stop(self, role: str) -> bool:
        self.stopped.append(role)
        return True

    def stop_all(self) -> None:
        self.stopped.extend(["text", "vision"])

    def ensure(self, spec: Any, force: bool = False) -> Any:
        self.ensured.append((spec, force))
        return SimpleNamespace(spec=spec, started_at=0.0)


def fake_installed_service(tmp_path: Path) -> EngineService:
    service = EngineService(
        EngineSettings(type="ollama", text_model="qwen3-0.6b-q4"),
        tmp_path / "engine",
        config_path=tmp_path / "settings.json",
    )
    service.servers = FakeServers()  # type: ignore[assignment]
    service.runtime = SimpleNamespace(  # type: ignore[assignment]
        status=lambda: RuntimeStatus(
            installed=True,
            version="b11900",
            directory=str(tmp_path),
            server=str(tmp_path / "llama-server.exe"),
            size_bytes=1,
        ),
        ensure=lambda: tmp_path / "llama-server.exe",
    )
    service.store = SimpleNamespace(  # type: ignore[assignment]
        is_installed=lambda entry: True,
        model_path=lambda entry: tmp_path / "model.gguf",
        mmproj_path=lambda entry: tmp_path / "mmproj.gguf",
        installed_ids=lambda: {"qwen3-vl-2b-q4", "qwen3-0.6b-q4"},
    )
    return service


def test_ensure_vision_passes_the_mmproj(tmp_path: Path) -> None:
    service = fake_installed_service(tmp_path)
    service._state["active"]["vision"] = "qwen3-vl-2b-q4"

    base = service.ensure_vision()

    spec, _force = service.servers.ensured[-1]
    assert spec.role == "vision"
    assert spec.mmproj_path == tmp_path / "mmproj.gguf"
    assert spec.n_gpu_layers >= 0
    assert base.endswith(":8124/v1")


def test_set_type_persists_and_flushes(tmp_path: Path) -> None:
    service = fake_installed_service(tmp_path)
    service.config_path.write_text(
        json.dumps({"agent_name": "Compa", "engine": {"type": "ollama"}}),
        encoding="utf-8",
    )

    result = service.set_type("llamacpp")
    assert result["engine"] == "llamacpp"
    assert service.settings.type == "llamacpp"
    saved = json.loads(service.config_path.read_text(encoding="utf-8"))
    assert saved["agent_name"] == "Compa"
    assert saved["engine"]["type"] == "llamacpp"

    service.set_type("ollama")
    assert "text" in service.servers.stopped
    assert service.settings.type == "ollama"


def test_set_type_requires_a_model_and_runtime(tmp_path: Path) -> None:
    service = fake_installed_service(tmp_path)
    service.store.is_installed = lambda entry: False  # type: ignore[assignment]
    with pytest.raises(ValueError):
        service.set_type("llamacpp")

    service = fake_installed_service(tmp_path)
    service.runtime.status = lambda: RuntimeStatus(  # type: ignore[assignment]
        installed=False,
        version="b11900",
        directory="",
        server="",
        size_bytes=0,
    )
    with pytest.raises(ValueError):
        service.set_type("llamacpp")
