"""Tests for the global CLIP image index (model-free fake embedder)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Sequence

import numpy as np
import pytest
from PIL import Image

from settings import ToolSettings, VisionSettings
from tools.executor import ToolExecutor
from tools.vision_tools import VisionService


class FakeEmbedder:
    """Deterministic 4-D embeddings so tests never load torch or CLIP."""

    def __init__(self) -> None:
        self.image_calls: list[Path] = []
        self.text_calls: list[str] = []
        self.is_loaded = True

    def encode_images(self, paths: Sequence[Path]) -> dict[Path, np.ndarray]:
        vectors: dict[Path, np.ndarray] = {}

        for path in paths:
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception:
                continue

            self.image_calls.append(path)
            name = path.stem.casefold()

            if "dog" in name:
                vector = [1.0, 0.0, 0.0, 0.0]
            elif "beach" in name:
                vector = [0.0, 1.0, 0.0, 0.0]
            else:
                vector = [0.0, 0.0, 1.0, 0.0]

            vectors[path] = np.asarray(vector, dtype=np.float32)

        return vectors

    def encode_text(self, query: str) -> np.ndarray:
        self.text_calls.append(query)
        lowered = query.casefold()

        if "dog" in lowered:
            vector = [1.0, 0.0, 0.0, 0.0]
        elif "beach" in lowered:
            vector = [0.0, 1.0, 0.0, 0.0]
        else:
            vector = [0.0, 0.0, 1.0, 0.0]

        return np.asarray(vector, dtype=np.float32)


def make_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 96), color).save(path)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "Pictures"
    make_image(root / "dog_park.jpg", (200, 40, 40))
    make_image(root / "beach_vacation.png", (30, 90, 200))
    make_image(root / "AppData" / "dog_cache.png", (10, 10, 10))
    make_image(root / "node_modules" / "beach_icon.png", (10, 10, 10))
    make_image(root / ".hidden" / "dog_secret.png", (10, 10, 10))
    make_image(root / ".hidden_file.png", (10, 10, 10))
    (root / "notes.txt").write_text("not an image", encoding="utf-8")
    (root / "broken.png").write_bytes(b"not a real png")
    return root


def make_service(tmp_path: Path, root: Path) -> tuple[VisionService, FakeEmbedder]:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(root)],
        batch_size=2,
        scan_interval_seconds=3600,
        max_images=100,
        max_file_mb=10,
    )
    embedder = FakeEmbedder()
    return VisionService(settings, embedder=embedder), embedder


def test_scan_indexes_images_and_skips_noise(tmp_path: Path, library: Path) -> None:
    service, embedder = make_service(tmp_path, library)

    status = service.scan_once()

    assert status["indexed_images"] == 2
    assert status["added"] == 2
    assert status["scanned"] == 3
    assert status["phase"] == "idle"

    indexed = {path.name for path in embedder.image_calls}
    assert indexed == {"dog_park.jpg", "beach_vacation.png"}

    dog_results = service.search("a dog")
    beach_results = service.search("beach", limit=3)

    assert dog_results["ok"] is True
    assert dog_results["results"][0]["name"] == "dog_park.jpg"
    assert beach_results["results"][0]["name"] == "beach_vacation.png"


def test_skips_unity_library_and_package_caches(tmp_path: Path) -> None:
    root = tmp_path / "UnityProject"
    make_image(root / "Assets" / "dog_asset.png", (40, 200, 40))
    make_image(root / "Library" / "dog_library.png", (200, 40, 40))
    make_image(root / "PackageCache" / "dog_cache.png", (40, 40, 200))

    service, embedder = make_service(tmp_path, root)
    status = service.scan_once()

    assert status["indexed_images"] == 1
    assert {path.name for path in embedder.image_calls} == {"dog_asset.png"}


def test_search_suppresses_near_duplicate_images(
    tmp_path: Path,
    library: Path,
) -> None:
    make_image(library / "dog_copy.jpg", (200, 40, 40))
    service, _embedder = make_service(tmp_path, library)
    service.scan_once()

    result = service.search("a dog", limit=5)

    names = [item["name"] for item in result["results"]]
    assert names.count("dog_park.jpg") + names.count("dog_copy.jpg") == 1


def test_low_confidence_queries_return_no_unrelated_images(
    tmp_path: Path,
) -> None:
    root = tmp_path / "noise"
    for index in range(60):
        make_image(root / f"noise_{index}.png", (20, 20, 20))

    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(root)],
        batch_size=8,
        scan_interval_seconds=3600,
        max_images=100,
        max_file_mb=10,
        min_confidence_z=3.9,
    )
    embedder = FakeEmbedder()
    service = VisionService(settings, embedder=embedder)
    service.scan_once()

    result = service.search("a clownfish")

    assert result["confidence"] == "low"
    assert result["results"] == []
    assert "No confident visual match" in result["message"]


def test_exclude_paths_skips_noisy_subtrees(tmp_path: Path) -> None:
    root = tmp_path / "mix"
    make_image(root / "photos" / "dog_keep.png", (200, 40, 40))
    make_image(root / "game-assets" / "dog_noise.png", (40, 40, 200))

    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(root)],
        exclude_paths=["game-assets"],
        batch_size=2,
        scan_interval_seconds=3600,
        max_images=100,
        max_file_mb=10,
    )
    embedder = FakeEmbedder()
    service = VisionService(settings, embedder=embedder)
    service.scan_once()

    result = service.search("a dog", limit=5)
    names = [item["name"] for item in result["results"]]
    assert "dog_keep.png" in names
    assert "dog_noise.png" not in names


def test_rescan_is_incremental_and_prunes_deleted_files(
    tmp_path: Path,
    library: Path,
) -> None:
    service, embedder = make_service(tmp_path, library)
    service.scan_once()
    assert len(embedder.image_calls) == 2

    status = service.scan_once()
    assert status["added"] == 0
    assert status["updated"] == 0
    assert status["unchanged"] == 2
    assert len(embedder.image_calls) == 2

    beach = library / "beach_vacation.png"
    changed = time.time() + 10
    os.utime(beach, (changed, changed))

    status = service.scan_once()
    assert status["updated"] == 1
    assert status["added"] == 0
    assert len(embedder.image_calls) == 3

    (library / "dog_park.jpg").unlink()

    status = service.scan_once()
    assert status["removed"] == 1
    assert status["indexed_images"] == 1


def test_search_on_empty_index_does_not_load_model(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    service, embedder = make_service(tmp_path, root)

    result = service.search("a dog")

    assert result["ok"] is True
    assert result["results"] == []
    assert result["indexed_images"] == 0
    assert embedder.text_calls == []


def test_search_clamps_limit_and_rejects_blank_query(
    tmp_path: Path,
    library: Path,
) -> None:
    service, _embedder = make_service(tmp_path, library)
    service.scan_once()

    assert service.search("dog", limit=99)["limit"] == 10
    assert service.search("dog", limit=0)["limit"] == 1
    assert service.search("   ")["ok"] is False


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 5) -> dict[str, object]:
        self.calls.append((query, limit))
        return {"ok": True, "query": query, "results": []}


def test_executor_dispatches_semantic_image_search_without_approval() -> None:
    vision = FakeVision()
    executor = ToolExecutor(ToolSettings(), vision=vision)

    assert executor.requires_confirmation("semantic_image_search") is False

    result = executor.execute(
        "semantic_image_search",
        {"query": "a red car", "limit": 3},
    )

    assert result["ok"] is True
    assert vision.calls == [("a red car", 3)]

    # Hermes sometimes invents search-directory arguments; ignore them.
    result = executor.execute(
        "semantic_image_search",
        {"query": "a dog", "all_drives": True, "root": "C:\\"},
    )

    assert result["ok"] is True
    assert vision.calls[-1] == ("a dog", 5)


def test_executor_reports_missing_vision_service() -> None:
    executor = ToolExecutor(ToolSettings())

    result = executor.execute("semantic_image_search", {"query": "dog"})

    assert result["ok"] is False
    assert "not available" in result["error"]


class ScreenVision:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def analyze_screen(self, user_question: str) -> dict[str, object]:
        self.questions.append(user_question)
        return {"ok": True, "answer": "a browser window"}


def test_executor_dispatches_screen_analysis() -> None:
    vision = ScreenVision()
    executor = ToolExecutor(ToolSettings(), vision=vision)  # type: ignore[arg-type]

    result = executor.execute(
        "analyze_screen",
        {"user_question": "What is on my screen?"},
    )

    assert result["ok"] is True
    assert vision.questions == ["What is on my screen?"]


def test_analyze_screen_uses_capture_and_local_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(tmp_path)],
        screen_model="test-vision",
    )
    service = VisionService(settings, embedder=FakeEmbedder())
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        service,
        "_capture_screen",
        lambda: ("BASE64IMAGE", 800, 600),
    )

    def fake_ask(question: str, image: str) -> str:
        captured["question"] = question
        captured["image"] = image
        return "A browser window with a form."

    monkeypatch.setattr(service, "_ask_vision_model", fake_ask)

    result = service.analyze_screen("What is on my screen?")

    assert result["ok"] is True
    assert result["answer"] == "A browser window with a form."
    assert result["model"] == "test-vision"
    assert result["screen"] == {"width": 800, "height": 600}
    assert captured == {
        "question": "What is on my screen?",
        "image": "BASE64IMAGE",
    }


def test_analyze_screen_explains_mllama_incompatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(tmp_path)],
        screen_model="llama3.2-vision",
    )
    service = VisionService(settings, embedder=FakeEmbedder())

    monkeypatch.setattr(
        service,
        "_capture_screen",
        lambda: ("BASE64IMAGE", 800, 600),
    )
    monkeypatch.setattr(
        service,
        "_ask_vision_model",
        lambda question, image: (_ for _ in ()).throw(
            RuntimeError("unknown model architecture: 'mllama'")
        ),
    )

    result = service.analyze_screen("What is on my screen?")

    assert result["ok"] is False
    assert "qwen3-vl" in result["error"]
    assert "0.30" in result["error"]


def test_ask_vision_model_sends_concise_bounded_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(tmp_path)],
        screen_model="test-vision",
        screen_max_tokens=321,
    )
    service = VisionService(settings, embedder=FakeEmbedder())
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"a window"}}]}'

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "tools.vision_tools.urllib.request.urlopen",
        fake_urlopen,
    )

    answer = service._ask_vision_model("What is visible?", "BASE64")

    assert answer == "a window"
    assert captured["timeout"] == settings.screen_timeout_seconds

    import json

    request = captured["request"]
    payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload["model"] == "test-vision"
    assert payload["max_tokens"] == 321
    assert payload["messages"][0]["role"] == "system"
    assert "without chain-of-thought" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_analyze_screen_explains_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(tmp_path)],
        screen_model="qwen3-vl:8b",
    )
    service = VisionService(settings, embedder=FakeEmbedder())

    monkeypatch.setattr(
        service,
        "_capture_screen",
        lambda: ("BASE64IMAGE", 800, 600),
    )
    monkeypatch.setattr(
        service,
        "_ask_vision_model",
        lambda question, image: (_ for _ in ()).throw(
            RuntimeError("Ollama vision request timed out after 120 seconds")
        ),
    )

    result = service.analyze_screen("What is on my screen?")

    assert result["ok"] is False
    assert "did not finish" in result["error"]
    assert "qwen3-vl:8b-instruct" in result["error"]
    assert "screen_max_tokens" in result["error"]


def test_analyze_screen_rejects_empty_question(
    tmp_path: Path,
) -> None:
    settings = VisionSettings(
        enabled=False,
        embed_model="fake-clip",
        index_path=tmp_path / "vision.sqlite3",
        scan_roots=[str(tmp_path)],
    )
    service = VisionService(settings, embedder=FakeEmbedder())

    result = service.analyze_screen("   ")

    assert result["ok"] is False
    assert "required" in result["error"]
