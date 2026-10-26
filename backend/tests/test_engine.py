"""Tests for the Phase 22 llama.cpp engine and model manager."""

from __future__ import annotations

import hashlib
import json
import struct
import threading
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.catalog import CATALOG, TEXT, VISION, ModelEntry, ModelFile
from engine.downloads import ModelStore
from engine.gguf import read_gguf
from engine.hardware import HardwareProfile, _parse_vulkan_memory, plan_gpu_layers
from engine.service import EngineService, RuntimeJob
from main import create_app
from settings import EngineSettings, MemorySettings, Settings


# --------------------------------------------------------------- hardware
def test_vulkan_heap_parser_picks_the_device_local_heap() -> None:
    output = """
Devices:
========
GPU0:
    deviceName         = AMD Radeon RX 9070 XT
    memoryHeaps: count = 2
        memoryHeaps[0]:
            size   = 50589007872 (0xbc7570000) (47.11 GiB)
            flags:
                None
        memoryHeaps[1]:
            size   = 17095983104 (0x3fb000000) (15.92 GiB)
            flags: count = 2
                MEMORY_HEAP_DEVICE_LOCAL_BIT
                MEMORY_HEAP_MULTI_INSTANCE_BIT
memoryTypes: count = 16
        memoryHeaps[0]:
            size   = 50589007872 (0xbc7570000) (47.11 GiB)
"""
    name, vram = _parse_vulkan_memory(output)
    assert name == "AMD Radeon RX 9070 XT"
    assert vram == 17_095_983_104


def _info(layers: int = 36, size: int = 5_027_784_512) -> Any:
    from engine.gguf import GgufInfo

    return GgufInfo(
        path=Path("model.gguf"),
        file_size=size,
        block_count=layers,
        embedding_length=4096,
        head_count=32,
        head_count_kv=[8],
    )


def test_plan_uses_vram_when_it_fits() -> None:
    profile = HardwareProfile(vram_bytes=17_095_983_104, ram_bytes=64 * 1024**3)
    plan = plan_gpu_layers(profile, _info(), 8192)

    assert plan.n_gpu_layers == 36
    assert plan.fully_on_gpu is True
    assert plan.kv_gb > 0


def test_plan_falls_back_to_cpu_without_vram() -> None:
    profile = HardwareProfile(vram_bytes=0, ram_bytes=8 * 1024**3)
    plan = plan_gpu_layers(profile, _info(), 8192)

    assert plan.n_gpu_layers == 0
    assert plan.fully_on_gpu is False
    assert "CPU" in plan.reason


def test_plan_partially_offloads_on_a_small_gpu() -> None:
    profile = HardwareProfile(vram_bytes=6 * 1024**3, ram_bytes=16 * 1024**3)
    plan = plan_gpu_layers(profile, _info(), 4096)

    assert 0 < plan.n_gpu_layers < 36
    assert plan.fully_on_gpu is False


def test_plan_respects_an_explicit_override() -> None:
    profile = HardwareProfile(vram_bytes=17_095_983_104)
    plan = plan_gpu_layers(profile, _info(), 8192, override=12)

    assert plan.n_gpu_layers == 12
    assert "override" in plan.reason


# ------------------------------------------------------------------- gguf
def _gguf_bytes(metadata: dict[str, Any]) -> bytes:
    def string(value: str) -> bytes:
        raw = value.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    def value(kind: int, item: Any) -> bytes:
        if kind == 8:
            return string(str(item))
        if kind == 4:
            return struct.pack("<I", int(item))
        raise AssertionError(kind)

    body = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
    body += struct.pack("<Q", len(metadata))
    for key, (kind, item) in metadata.items():
        body += string(key) + struct.pack("<I", kind) + value(kind, item)
    return body


def test_gguf_reader_extracts_layer_and_kv_shape(tmp_path: Path) -> None:
    path = tmp_path / "tiny.gguf"
    path.write_bytes(
        _gguf_bytes(
            {
                "general.architecture": (8, "qwen3"),
                "general.name": (8, "Tiny"),
                "qwen3.block_count": (4, 28),
                "qwen3.embedding_length": (4, 1024),
                "qwen3.attention.head_count": (4, 16),
                "qwen3.attention.head_count_kv": (4, 8),
            }
        )
    )

    info = read_gguf(path)

    assert info.error is None
    assert info.architecture == "qwen3"
    assert info.block_count == 28
    assert info.head_count == 16
    assert info.head_count_kv == [8]
    assert info.kv_bytes_per_token() > 0


def test_gguf_reader_rejects_non_gguf(tmp_path: Path) -> None:
    path = tmp_path / "nope.bin"
    path.write_bytes(b"not a model")
    info = read_gguf(path)
    assert info.error
    assert info.block_count == 0


# ---------------------------------------------------------------- catalog
def test_catalog_entries_are_pinned_and_typed() -> None:
    ids = [entry.id for entry in CATALOG]
    assert len(ids) == len(set(ids))
    assert any(entry.task == TEXT for entry in CATALOG)
    assert any(entry.task == VISION for entry in CATALOG)

    for entry in CATALOG:
        assert entry.total_bytes > 0
        assert entry.min_vram_gb > 0
        assert entry.files[0].role == "model"
        assert entry.files[0].url.startswith("https://huggingface.co/")
        for item in entry.files:
            assert len(item.sha256) == 64
            int(item.sha256, 16)
        if entry.task == VISION:
            assert entry.file("mmproj") is not None


def test_low_end_badge_is_present() -> None:
    badges = {entry.badge for entry in CATALOG}
    assert any("Low-End" in badge for badge in badges)
    assert any("Mid-Range" in badge for badge in badges)
    assert any("High-End" in badge for badge in badges)


# --------------------------------------------------------------- downloads
class _FileServer:
    """Tiny Range-capable HTTP server for download tests."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requests: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                outer.requests.append(dict(self.headers))
                start = 0
                header = self.headers.get("Range")
                if header and header.startswith("bytes="):
                    start = int(header.split("=", 1)[1].split("-", 1)[0])
                body = outer.payload[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(body)))
                if start:
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(outer.payload) - 1}/{len(outer.payload)}",
                    )
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _entry_for(payload: bytes, url: str, sha: str | None = None) -> ModelEntry:
    return ModelEntry(
        id="tiny-test",
        name="Tiny Test",
        task=TEXT,
        family="test",
        parameters="1M",
        quantization="Q4",
        badge="Low-End PC (4GB VRAM or CPU) - Fast & lightweight",
        min_vram_gb=0.1,
        description="test entry",
        context=512,
        files=(
            ModelFile(
                role="model",
                repo="test/tiny",
                filename="tiny.gguf",
                size=len(payload),
                sha256=sha or hashlib.sha256(payload).hexdigest(),
                url_override=url,
            ),
        ),
    )


def _wait(store: ModelStore, job_id: str, timeout: float = 10.0) -> Any:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.job(job_id)
        assert job is not None
        if job.state in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("download did not finish")


def test_download_verifies_sha256_and_records_install(tmp_path: Path) -> None:
    payload = b"model-bytes" * 10_000
    server = _FileServer(payload)
    try:
        entry = _entry_for(payload, server.url("tiny.gguf"))
        store = ModelStore(tmp_path)
        job = store.start_install(entry)

        done = _wait(store, job.id)

        assert done.state == "done", done.error
        assert done.downloaded == len(payload)
        assert store.is_installed(entry)
        assert store.model_path(entry).read_bytes() == payload
        assert entry.id in store.installed_ids()
    finally:
        server.close()


def test_download_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    payload = b"corrupt-me"
    server = _FileServer(payload)
    try:
        entry = _entry_for(payload, server.url("tiny.gguf"), sha="0" * 64)
        store = ModelStore(tmp_path)
        job = store.start_install(entry)

        done = _wait(store, job.id)

        assert done.state == "error"
        assert "Checksum" in done.error
        assert not store.model_path(entry).exists()
        assert not store.is_installed(entry)
    finally:
        server.close()


def test_download_resumes_a_partial_file(tmp_path: Path) -> None:
    payload = b"0123456789" * 5_000
    server = _FileServer(payload)
    try:
        entry = _entry_for(payload, server.url("tiny.gguf"))
        store = ModelStore(tmp_path)
        partial = store.model_path(entry).with_suffix(".gguf.part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(payload[: len(payload) // 2])

        job = store.start_install(entry)
        done = _wait(store, job.id)

        assert done.state == "done", done.error
        assert store.model_path(entry).read_bytes() == payload
        assert any(item.get("Range") for item in server.requests)
    finally:
        server.close()


def test_delete_removes_files_and_state(tmp_path: Path) -> None:
    payload = b"x" * 1024
    server = _FileServer(payload)
    try:
        entry = _entry_for(payload, server.url("tiny.gguf"))
        store = ModelStore(tmp_path)
        _wait(store, store.start_install(entry).id)

        assert store.delete(entry) is True
        assert not store.is_installed(entry)
        assert not (tmp_path / "models" / entry.id).exists()
    finally:
        server.close()


# ----------------------------------------------------------------- service
class _FakeServers:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.ensured: list[Any] = []
        self.roles: dict[str, Any] = {}

    def _status(self, role: str) -> dict[str, Any]:
        server = self.roles.get(role)
        return {"role": role, "running": server is not None}

    def status(self, role: str | None = None) -> Any:
        if role is not None:
            return self._status(role)
        return {"roles": [], "ports": {}}

    def port_for(self, role: str) -> int:
        return 8123 if role == "text" else 8124

    def base_url(self, role: str) -> str:
        return f"http://127.0.0.1:{self.port_for(role)}/v1"

    def stop(self, role: str) -> bool:
        self.stopped.append(role)
        self.roles.pop(role, None)
        return True

    def release(self, role: str) -> bool:
        return self.stop(role)

    def ensure(self, spec: Any, force: bool = False) -> Any:
        self.ensured.append((spec, force))
        server = SimpleNamespace(spec=spec, started_at=0.0)
        self.roles[spec.role] = server
        return server

    def stop_all(self) -> None:
        for role in list(self.roles):
            self.stop(role)


def _service(tmp_path: Path, **overrides: Any) -> EngineService:
    settings = EngineSettings(type="llamacpp", **overrides)
    service = EngineService(settings, tmp_path)
    service.servers = _FakeServers()  # type: ignore[assignment]
    return service


def test_switch_flushes_before_starting_the_new_model(tmp_path: Path) -> None:
    payload = b"weights" * 100
    server = _FileServer(payload)
    try:
        entry = _entry_for(payload, server.url("tiny.gguf"))
        # Install the tiny model through the store, then make it part of the
        # active catalog by installing a real catalog id instead.
        store = ModelStore(tmp_path)
        _wait(store, store.start_install(entry).id)

        service = _service(tmp_path, text_model="qwen3-0.6b-q4")
        # Point the service at the installed test model by injecting a catalog
        # entry under the expected id.
        import engine.catalog as catalog_module

        catalog_module.CATALOG = (entry,)
        catalog_module.BY_ID = {entry.id: entry}
        try:
            result = service.switch("text", entry.id)
        finally:
            catalog_module.CATALOG = CATALOG
            import importlib

            catalog_module.BY_ID = {item.id: item for item in CATALOG}
            importlib.reload(catalog_module)

        assert result["vram_flushed"] is True
        assert service.servers.stopped == ["text"]
        spec, force = service.servers.ensured[-1]
        assert force is True
        assert spec.model_path == store.model_path(entry)
    finally:
        server.close()


def test_switch_rejects_missing_or_wrong_task(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.switch("text", "qwen3-0.6b-q4")  # not installed
    with pytest.raises(ValueError):
        service.switch("text", "qwen3-vl-2b-q4")  # vision model


def test_text_client_is_none_when_ollama_is_selected(tmp_path: Path) -> None:
    service = EngineService(EngineSettings(type="ollama"), tmp_path)
    service.servers = _FakeServers()  # type: ignore[assignment]
    assert service.text_client() is None


def test_catalog_marks_fit_for_installed_models(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.hardware = HardwareProfile(vram_bytes=17_095_983_104)
    entries = {item["id"]: item for item in service.catalog()}

    assert entries["qwen3-8b-q4"]["fit"] == "gpu"
    assert entries["qwen3-8b-q4"]["installed"] is False
    assert entries["qwen3-0.6b-q4"]["likely_fits_vram"] is True


# --------------------------------------------------------------------- api
class _FakeChat:
    def __init__(self) -> None:
        self.clients: list[Any] = []

    def set_client(self, client: Any) -> None:
        self.clients.append(client)

    def generate_reply(self, *args: Any, **kwargs: Any) -> str:
        return "ok"

    def learn_from_exchange(self, *args: Any, **kwargs: Any) -> None:
        return None

    def discard_pending(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def interrupt(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def get_pending_for_conversation(self, *args: Any, **kwargs: Any) -> None:
        return None


def _app(tmp_path: Path) -> tuple[TestClient, EngineService, _FakeChat]:
    settings = Settings(
        agent_name="TestBot",
        personality="Direct.",
        personality_file=tmp_path / "personality.md",
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md"),
    )
    service = EngineService(
        EngineSettings(type="ollama"),
        tmp_path / "engine",
        config_path=tmp_path / "settings.json",
    )
    chat = _FakeChat()
    app = create_app(
        settings=settings,
        engine_service=service,
        chat_service=chat,  # type: ignore[arg-type]
    )
    return TestClient(app), service, chat


def test_engine_status_and_catalog_api(tmp_path: Path) -> None:
    client, _service, _chat = _app(tmp_path)
    with client:
        status = client.get("/engine/status").json()
        assert status["engine"] == "ollama"
        assert "hardware" in status
        assert status["runtime"]["installed"] is False

        catalog = client.get("/models/catalog").json()
        assert len(catalog["models"]) == len(CATALOG)
        assert catalog["models"][0]["installed"] is False
        assert catalog["models"][0]["badge"]


def test_models_api_rejects_unknown_and_uninstalled(tmp_path: Path) -> None:
    client, _service, _chat = _app(tmp_path)
    with client:
        assert client.post("/models/install", json={"model_id": "nope"}).status_code == 404
        result = client.post(
            "/models/active", json={"task": "text", "model_id": "qwen3-0.6b-q4"}
        )
        assert result.status_code == 400
        assert "not installed" in result.json()["detail"]
        assert client.delete("/models/nope").status_code == 404


# --------------------------------------------------------------- llama.cpp client
class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def __iter__(self):
        return iter(self.lines)

    def read(self) -> bytes:
        return b"".join(self.lines)


def test_llama_client_translates_streaming_chunks(monkeypatch) -> None:
    from engine.client import LlamaServerClient

    events = [
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"get_open_windows","arguments":"{}"}}]}}]}\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3},'
        b'"timings":{"prompt_ms":10.0,"predicted_ms":5.0}}\n',
        b"data: [DONE]\n",
    ]

    def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(events)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LlamaServerClient("http://127.0.0.1:8123/v1")

    chunks = list(
        client.stream_chat(
            model="tiny",
            messages=[{"role": "user", "content": "hi"}],
            think=False,
        )
    )

    content = "".join(
        (chunk.get("message") or {}).get("content") or "" for chunk in chunks
    )
    assert content == "Hello"
    assert any((chunk.get("message") or {}).get("thinking") == "think" for chunk in chunks)
    tools = [
        (chunk.get("message") or {}).get("tool_calls")
        for chunk in chunks
        if (chunk.get("message") or {}).get("tool_calls")
    ]
    assert tools and tools[0][0]["function"]["name"] == "get_open_windows"
    final = chunks[-1]
    assert final["done"] is True
    assert final["prompt_eval_count"] == 12
    assert final["eval_count"] == 3

def test_engine_type_switches_at_runtime(tmp_path: Path) -> None:
    client, service, chat = _app(tmp_path)
    with client:
        response = client.post("/engine/type", json={"type": "ollama"})
        assert response.status_code == 200
        assert response.json()["engine"] == "ollama"
        assert service.settings.type == "ollama"
        # Chat is rebound to the Ollama transport.
        assert chat.clients and chat.clients[-1] is not None

        denied = client.post("/engine/type", json={"type": "llamacpp"})
        assert denied.status_code == 400
        assert "runtime" in denied.json()["detail"].lower()
