"""Global CLIP image index with continuous background crawling.

The index lives in a small SQLite database next to the rest of the agent's local
data. Embeddings are stored as normalized float32 blobs and searched with a
chunked NumPy cosine scan, so no external vector service is required.

The crawler runs in a daemon thread started by the FastAPI lifespan. It scans
the configured roots (the user profile by default), skips caches/system folders
and cloud placeholders, embeds changed images in small batches, and periodically
rescans to pick up new or deleted files.
"""

from __future__ import annotations

import heapq
import logging
import os
import re
import sqlite3
import threading
import time
import warnings
import base64
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

import numpy as np

from settings import VisionSettings

logger = logging.getLogger(__name__)

# The HF cache falls back to copying files on Windows without symlink support.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
    }
)

_DEFAULT_SKIP_NAMES = frozenset(
    {
        "appdata",
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "node_modules",
        "bower_components",
        "packagecache",
        ".next",
        ".nuxt",
        ".git",
        ".cache",
        ".npm",
        ".gradle",
        ".cargo",
        ".rustup",
        ".vscode",
        ".idea",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "site-packages",
        "target",
        "anaconda3",
        "miniconda3",
        "$recycle.bin",
        "system volume information",
    }
)

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_MIN_IMAGE_DIMENSION = 96


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(vector: Any) -> np.ndarray | None:
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    if array.size == 0:
        return None

    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm <= 0:
        return None

    return array / norm


class Embedder(Protocol):
    def encode_images(self, paths: Sequence[Path]) -> dict[Path, np.ndarray]:
        """Return a normalized vector for each readable image."""

    def encode_text(self, query: str) -> np.ndarray:
        """Return a normalized vector for a text query."""


class ClipEmbedder:
    """Lazy CLIP wrapper. torch and sentence-transformers load on first use."""

    QUERY_TEMPLATES = (
        "{}",
        "a photo of {}",
        "a picture of {}",
        "a close-up photo of {}",
        "a photograph showing {}",
        "an image of {}",
    )

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        with self._load_lock:
            if self._model is None:
                import torch
                from sentence_transformers import SentenceTransformer

                try:
                    torch.set_num_threads(max(1, min(4, os.cpu_count() or 4)))
                except Exception:  # pragma: no cover - platform specific
                    pass

                logger.info("Loading CLIP model %s", self.model_name)
                self._model = SentenceTransformer(self.model_name)
                logger.info("CLIP model %s loaded", self.model_name)

        return self._model

    def encode_images(self, paths: Sequence[Path]) -> dict[Path, np.ndarray]:
        if not paths:
            return {}

        from PIL import Image, ImageOps

        model = self._ensure_model()
        images: list[Any] = []
        kept: list[Path] = []

        for path in paths:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    with Image.open(path) as image:
                        if (
                            image.width < _MIN_IMAGE_DIMENSION
                            or image.height < _MIN_IMAGE_DIMENSION
                        ):
                            continue
                        image = ImageOps.exif_transpose(image)
                        image = image.convert("RGB")
                        image.thumbnail((512, 512))
                        images.append(image.copy())
                        kept.append(path)
            except Exception:  # noqa: BLE001 - skip corrupt/unsupported files
                logger.debug("Skipping unreadable image %s", path, exc_info=True)

        if not images:
            return {}

        encoded: list[tuple[Path, Any]] = []

        with self._inference_lock:
            try:
                vectors = model.encode(
                    images,
                    batch_size=max(1, min(len(images), 8)),
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                matrix = np.asarray(vectors, dtype=np.float32)
                encoded = [
                    (path, matrix[index])
                    for index, path in enumerate(kept)
                ]
            except Exception:  # noqa: BLE001 - isolate a bad image
                logger.exception(
                    "CLIP batch failed; retrying images individually"
                )
                for image, path in zip(images, kept):
                    try:
                        vector = model.encode(
                            [image],
                            convert_to_numpy=True,
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        )[0]
                        encoded.append((path, vector))
                    except Exception:  # noqa: BLE001 - skip this image
                        logger.debug(
                            "Skipping image %s after encode failure",
                            path,
                            exc_info=True,
                        )

        result: dict[Path, np.ndarray] = {}
        for path, vector in encoded:
            normalized = _normalize(vector)
            if normalized is not None:
                result[path] = normalized
        return result

    def encode_text(self, query: str) -> np.ndarray:
        model = self._ensure_model()

        # Prompt ensembling: CLIP ranks more reliably when the query is phrased
        # as a natural photo description instead of a bare noun phrase.
        base = re.sub(
            r"^(?:a|an|the)?\s*(?:photo|picture|image|photograph)\s+"
            r"(?:of|showing)\s+",
            "",
            query.strip(),
            flags=re.IGNORECASE,
        )
        prompts = list(
            dict.fromkeys(
                [
                    query,
                    *(
                        template.format(base)
                        for template in self.QUERY_TEMPLATES
                    ),
                ]
            )
        )

        with self._inference_lock:
            vectors = model.encode(
                prompts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )

        matrix = np.asarray(vectors, dtype=np.float32)
        averaged = matrix.mean(axis=0)
        normalized = _normalize(averaged)
        if normalized is None:
            raise ValueError("The text encoder returned an empty vector.")
        return normalized


class ImageVectorStore:
    """Small local vector store: SQLite metadata plus float32 embeddings."""

    def __init__(
        self,
        db_path: Path,
        model_name: str,
        dedupe_threshold: float = 0.995,
    ) -> None:
        self.db_path = db_path
        self.model_name = model_name
        self.dedupe_threshold = dedupe_threshold
        self._ready = False
        self._init_lock = threading.Lock()
        # One SQLite connection per worker thread, reused across calls. This
        # avoids opening a new file handle for every status check and search.
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            check_same_thread=False,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def close(self) -> None:
        """Close cached per-thread connections."""
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()

        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                logger.debug(
                    "Could not close a vision index connection",
                    exc_info=True,
                )

        self._local = threading.local()
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return

        with self._init_lock:
            if self._ready:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            connection = self._connection()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS images (
                    path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    model TEXT NOT NULL,
                    indexed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS images_model_idx
                    ON images(model);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'model'"
            ).fetchone()

            if row is not None and row[0] != self.model_name:
                logger.info(
                    "Vision model changed from %s to %s; resetting index",
                    row[0],
                    self.model_name,
                )
                connection.execute("DELETE FROM images")

            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('model', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.model_name,),
            )
            connection.commit()

            self._ready = True

    def count(self) -> int:
        self.initialize()
        row = self._connection().execute("SELECT COUNT(*) FROM images").fetchone()
        return int(row[0]) if row else 0

    def known_entries(self) -> dict[str, tuple[int, int]]:
        self.initialize()
        rows = self._connection().execute(
            "SELECT path, mtime_ns, size FROM images"
        ).fetchall()
        return {
            str(path): (int(mtime_ns), int(size))
            for path, mtime_ns, size in rows
        }

    def upsert_many(self, rows: Sequence[tuple[str, int, int, bytes]]) -> None:
        if not rows:
            return

        self.initialize()
        now = time.time()
        connection = self._connection()
        connection.executemany(
            """
            INSERT INTO images
                (path, mtime_ns, size, embedding, model, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                embedding = excluded.embedding,
                model = excluded.model,
                indexed_at = excluded.indexed_at
            """,
            [
                (path, mtime_ns, size, embedding, self.model_name, now)
                for path, mtime_ns, size, embedding in rows
            ],
        )
        connection.commit()

    def prune_missing(self, seen_paths: set[str]) -> int:
        self.initialize()
        connection = self._connection()
        stored = [
            str(row[0])
            for row in connection.execute("SELECT path FROM images")
        ]
        stale = [path for path in stored if path not in seen_paths]

        if stale:
            connection.executemany(
                "DELETE FROM images WHERE path = ?",
                [(path,) for path in stale],
            )
            connection.commit()

        return len(stale)

    def search(
        self,
        query_vector: np.ndarray,
        limit: int,
    ) -> tuple[list[tuple[str, float]], float, float, float]:
        """Return ranked unique matches plus mean, std, and best-gap scores."""
        query = _normalize(query_vector)
        if query is None or limit <= 0:
            return [], 0.0, 0.0, 0.0

        self.initialize()
        candidate_limit = max(limit * 8, 20)
        candidates: list[tuple[float, str, bytes]] = []
        score_sum = 0.0
        score_square_sum = 0.0
        score_count = 0

        cursor = self._connection().execute("SELECT path, embedding FROM images")

        while True:
            rows = cursor.fetchmany(1024)
            if not rows:
                break

            valid = [
                (str(path), blob)
                for path, blob in rows
                if blob is not None and len(blob) == query.size * 4
            ]
            if not valid:
                continue

            matrix = np.vstack(
                [
                    np.frombuffer(blob, dtype=np.float32)
                    for _, blob in valid
                ]
            )
            scores = matrix @ query
            score_sum += float(scores.sum())
            score_square_sum += float(np.square(scores).sum())
            score_count += len(scores)
            take = min(candidate_limit, len(valid))

            for index in np.argpartition(scores, -take)[-take:]:
                heapq.heappush(
                    candidates,
                    (
                        float(scores[index]),
                        valid[index][0],
                        valid[index][1],
                    ),
                )
                if len(candidates) > candidate_limit:
                    heapq.heappop(candidates)

        if score_count == 0:
            return [], 0.0, 0.0, 0.0

        mean = score_sum / score_count
        variance = max(0.0, score_square_sum / score_count - mean * mean)
        std = float(np.sqrt(variance))

        selected: list[tuple[str, float]] = []
        selected_vectors: list[np.ndarray] = []

        for score, path, blob in sorted(candidates, reverse=True):
            vector = np.frombuffer(blob, dtype=np.float32)

            if any(
                float(np.dot(vector, existing)) >= self.dedupe_threshold
                for existing in selected_vectors
            ):
                continue

            selected.append((path, score))
            selected_vectors.append(vector)

            if len(selected) >= limit:
                break

        best = selected[0][1] if selected else 0.0
        second = selected[1][1] if len(selected) > 1 else best
        gap = max(0.0, best - second)
        return selected, mean, std, gap


class VisionService:
    """Background crawler, index status, and semantic image search."""

    def __init__(
        self,
        settings: VisionSettings,
        embedder: Embedder | None = None,
        ollama_base_url: str = "http://127.0.0.1:11434/v1",
    ) -> None:
        self.settings = settings
        self.embedder = embedder or ClipEmbedder(settings.embed_model)
        self.ollama_base_url = ollama_base_url
        self.store = ImageVectorStore(
            settings.resolved_index_path(),
            settings.embed_model,
            dedupe_threshold=settings.dedupe_threshold,
        )
        self.roots = settings.resolved_scan_roots()
        self._skip_names = {
            name.casefold()
            for name in (*_DEFAULT_SKIP_NAMES, *settings.skip_directory_names)
        }
        self._excluded_fragments = tuple(
            fragment.casefold()
            for fragment in settings.exclude_paths
            if fragment.strip()
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "phase": "disabled" if not settings.enabled else "stopped",
            "indexed_images": 0,
            "scanned": 0,
            "added": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "removed": 0,
            "last_scan_started": None,
            "last_scan_finished": None,
            "last_error": None,
        }

    def _set_state(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)

    def _state_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def start(self) -> None:
        """Start the background crawler once. Safe to call repeatedly."""
        if not self.settings.enabled:
            self._set_state(phase="disabled")
            return

        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="vision-indexer",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Ask the crawler to stop and wait briefly for the current batch."""
        self._stop.set()
        thread = self._thread

        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

        if thread is None or not thread.is_alive():
            self.store.close()
            self._thread = None
        else:
            logger.warning(
                "Vision indexer did not stop within 5 seconds; "
                "leaving its SQLite connections open until process exit"
            )

        self._set_state(phase="stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                logger.exception("Vision indexing failed")
                self._set_state(
                    phase="error",
                    last_error=str(exc),
                    last_scan_finished=_now_iso(),
                )

            if self._stop.wait(self.settings.scan_interval_seconds):
                break

    def _is_excluded_path(self, path: Path) -> bool:
        if not self._excluded_fragments:
            return False

        value = str(path).casefold()
        return any(fragment in value for fragment in self._excluded_fragments)

    def _skip_directory(self, entry: os.DirEntry[str]) -> bool:
        if entry.name.startswith("."):
            return True

        name = entry.name.casefold()
        if name in self._skip_names:
            return True

        if self._is_excluded_path(Path(entry.path)):
            return True

        # Unity projects keep generated assets and thousands of icons in Library.
        if name == "library":
            project = Path(entry.path).parent
            if (project / "Assets").is_dir() or (
                project / "ProjectSettings"
            ).is_dir():
                return True

        try:
            is_junction = getattr(entry, "is_junction", None)
            if entry.is_symlink() or (
                is_junction is not None and is_junction()
            ):
                return True

            attributes = getattr(
                entry.stat(follow_symlinks=False),
                "st_file_attributes",
                0,
            )
        except OSError:
            return True

        return bool(
            attributes & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)
        )

    def _skip_file(self, stat_result: os.stat_result) -> bool:
        attributes = getattr(stat_result, "st_file_attributes", 0)

        if attributes & (
            _FILE_ATTRIBUTE_HIDDEN
            | _FILE_ATTRIBUTE_SYSTEM
            | _FILE_ATTRIBUTE_OFFLINE
            | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        ):
            return True

        if stat_result.st_size <= 0:
            return True

        return stat_result.st_size > self.settings.max_file_mb * 1024 * 1024

    def _iter_images(
        self,
        root: Path,
        seen: set[str],
    ) -> Iterator[tuple[Path, os.stat_result]]:
        stack = [root]

        while stack and not self._stop.is_set():
            current = stack.pop()

            try:
                entries = list(os.scandir(current))
            except OSError:
                continue

            for entry in entries:
                if self._stop.is_set():
                    return

                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not self._skip_directory(entry):
                            stack.append(Path(entry.path))
                        continue

                    if not entry.is_file(follow_symlinks=False):
                        continue

                    path = Path(entry.path)
                    if path.name.startswith("."):
                        continue

                    if self._is_excluded_path(path):
                        continue

                    if path.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue

                    stat_result = entry.stat(follow_symlinks=False)
                    if self._skip_file(stat_result):
                        continue

                    key = str(path)
                    if key in seen:
                        continue

                    yield path, stat_result
                except OSError:
                    continue

    def _flush_batch(
        self,
        batch: Sequence[tuple[Path, os.stat_result]],
        known: dict[str, tuple[int, int]],
        counters: dict[str, Any],
    ) -> None:
        vectors = self.embedder.encode_images(
            [path for path, _ in batch]
        )

        rows: list[tuple[str, int, int, bytes]] = []

        for path, stat_result in batch:
            vector = vectors.get(path)
            if vector is None:
                counters["skipped"] += 1
                continue

            normalized = _normalize(vector)
            if normalized is None:
                counters["skipped"] += 1
                continue

            key = str(path)
            was_indexed = key in known
            rows.append(
                (
                    key,
                    int(stat_result.st_mtime_ns),
                    int(stat_result.st_size),
                    normalized.tobytes(),
                )
            )
            known[key] = (int(stat_result.st_mtime_ns), int(stat_result.st_size))
            counters["updated" if was_indexed else "added"] += 1

        self.store.upsert_many(rows)
        self._set_state(
            scanned=counters["scanned"],
            added=counters["added"],
            updated=counters["updated"],
            unchanged=counters["unchanged"],
            skipped=counters["skipped"],
            removed=counters["removed"],
        )

    def scan_once(self) -> dict[str, Any]:
        """Run one full incremental scan. Exposed for tests and diagnostics."""
        self.store.initialize()
        known = self.store.known_entries()

        counters: dict[str, Any] = {
            "scanned": 0,
            "added": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "removed": 0,
        }
        seen: set[str] = set()
        batch: list[tuple[Path, os.stat_result]] = []
        all_roots_available = bool(self.roots) and all(
            root.is_dir() for root in self.roots
        )
        capacity = max(0, self.settings.max_images - len(known))
        truncated = False

        self._set_state(
            phase="indexing",
            last_scan_started=_now_iso(),
            last_error=None,
            **counters,
        )

        try:
            for root in self.roots:
                if self._stop.is_set():
                    break

                for path, stat_result in self._iter_images(root, seen):
                    key = str(path)
                    seen.add(key)
                    counters["scanned"] += 1

                    prior = known.get(key)
                    if prior == (
                        int(stat_result.st_mtime_ns),
                        int(stat_result.st_size),
                    ):
                        counters["unchanged"] += 1
                        continue

                    if prior is None and counters["added"] >= capacity:
                        truncated = True
                        break

                    batch.append((path, stat_result))

                    if len(batch) >= self.settings.batch_size:
                        self._flush_batch(batch, known, counters)
                        batch.clear()
                        time.sleep(0.05)

                if truncated or self._stop.is_set():
                    break

            if batch:
                self._flush_batch(batch, known, counters)

            if (
                all_roots_available
                and not truncated
                and not self._stop.is_set()
            ):
                counters["removed"] = self.store.prune_missing(seen)

            phase = "stopped" if self._stop.is_set() else "idle"
            self._set_state(
                phase=phase,
                indexed_images=self.store.count(),
                last_scan_finished=_now_iso(),
                **counters,
            )
        except Exception as exc:
            self._set_state(
                phase="error",
                last_error=str(exc),
                last_scan_finished=_now_iso(),
                **counters,
            )
            raise

        return self.status()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Search the global index for images matching a text description."""
        text = str(query or "").strip()
        if not text:
            return {
                "ok": False,
                "error": "The image search query must not be empty.",
            }

        try:
            requested = int(limit)
        except (TypeError, ValueError):
            requested = 5
        effective_limit = max(1, min(requested, 10))

        try:
            indexed = self.store.count()
        except Exception as exc:  # noqa: BLE001 - index may not exist yet
            return {
                "ok": False,
                "error": f"The image index is unavailable: {exc}",
            }

        state = self._state_snapshot()
        indexing = state.get("phase") == "indexing"

        if indexed == 0:
            return {
                "ok": True,
                "query": text,
                "limit": effective_limit,
                "results": [],
                "indexed_images": 0,
                "indexing": indexing,
                "confidence": "none",
                "best_score": 0.0,
                "mean_score": 0.0,
                "score_gap": 0.0,
                "z_score": 0.0,
                "message": (
                    "No images have been indexed yet. The background image "
                    "indexer is still starting up; try again shortly."
                    if self.settings.enabled
                    else "The semantic image index is disabled."
                ),
            }

        try:
            vector = self.embedder.encode_text(text)
            matches, mean_score, std_score, score_gap = self.store.search(
                vector,
                effective_limit,
            )
        except Exception as exc:  # noqa: BLE001 - report model/index failures
            logger.exception("Semantic image search failed")
            return {
                "ok": False,
                "error": (
                    "Semantic image search is not ready: "
                    f"{str(exc)[:300]}. The CLIP model may still be "
                    "downloading on first use."
                ),
            }

        best_score = matches[0][1] if matches else 0.0
        z_score = (
            (best_score - mean_score) / std_score
            if std_score > 0
            else 0.0
        )
        confident = (
            indexed < 50
            or z_score >= self.settings.min_confidence_z
        )
        confidence = (
            "none"
            if not matches
            else "high"
            if z_score >= max(4.2, self.settings.min_confidence_z)
            else "medium"
            if confident
            else "low"
        )

        results: list[dict[str, Any]] = []

        if confident:
            for path_value, score in matches:
                item_z = (
                    (float(score) - mean_score) / std_score
                    if std_score > 0
                    else 0.0
                )
                if indexed >= 50 and (
                    std_score <= 0
                    or item_z < self.settings.min_result_z
                ):
                    continue

                path = Path(path_value)

                try:
                    if not path.is_file():
                        continue
                    stat_result = path.stat()
                except OSError:
                    continue

                results.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "score": round(float(score), 4),
                        "size": int(stat_result.st_size),
                        "modified": datetime.fromtimestamp(
                            stat_result.st_mtime,
                            tz=timezone.utc,
                        ).isoformat(timespec="seconds"),
                    }
                )

                if len(results) >= effective_limit:
                    break

        if results:
            message = (
                f'Found {len(results)} image(s) matching "{text}" '
                f"({confidence} confidence)."
            )
        elif matches and not confident:
            message = (
                f'No confident visual match for "{text}". The best candidate '
                "was too close to the general background of the index, so it "
                "was not returned. Try a more specific English description, "
                "or check that the image is indexed and not an unsupported or "
                "very small file."
            )
        else:
            message = f'No indexed images matched "{text}".'

        if indexing:
            message += (
                f" The index is still building ({indexed} image(s) indexed so "
                "far); more results may appear later."
            )

        return {
            "ok": True,
            "query": text,
            "limit": effective_limit,
            "results": results,
            "indexed_images": indexed,
            "indexing": indexing,
            "confidence": confidence,
            "best_score": round(float(best_score), 4),
            "mean_score": round(float(mean_score), 4),
            "score_gap": round(float(score_gap), 4),
            "z_score": round(float(z_score), 2),
            "message": message,
        }

    def _capture_screen(self) -> tuple[str, int, int]:
        """Capture the primary screen to an in-memory PNG (never a temp file)."""
        import mss
        from PIL import Image

        with mss.mss() as capture:
            monitors = capture.monitors
            if not monitors:
                raise RuntimeError("No screen monitor is available.")
            monitor = monitors[1] if len(monitors) > 1 else monitors[0]
            shot = capture.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)

        if image.width > self.settings.screen_max_width:
            ratio = self.settings.screen_max_width / image.width
            image = image.resize(
                (
                    self.settings.screen_max_width,
                    max(1, int(image.height * ratio)),
                ),
                Image.Resampling.LANCZOS,
            )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return encoded, image.width, image.height

    def _ask_vision_model(self, question: str, image_b64: str) -> str:
        endpoint = self.ollama_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.screen_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a concise screen-analysis assistant. Answer in "
                        "the language of the user's question. Give only the final "
                        "answer, without chain-of-thought or reasoning text."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"
                            },
                        },
                    ],
                },
            ],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": self.settings.screen_max_tokens,
        }

        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.screen_timeout_seconds,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise RuntimeError(
                "Ollama vision request timed out after "
                f"{self.settings.screen_timeout_seconds:.0f} seconds"
            ) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"Ollama vision request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach the local Ollama vision endpoint at "
                f"{endpoint}: {exc.reason}"
            ) from exc

        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("Ollama returned no vision response.")

        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )

        text = str(content or "").strip()
        if not text:
            raise RuntimeError("Ollama returned an empty vision response.")
        return text

    def analyze_screen(self, user_question: str) -> dict[str, Any]:
        """Capture the primary screen and ask the local vision model about it."""
        question = str(user_question or "").strip()
        if not question:
            return {
                "ok": False,
                "error": "A question about the screen is required.",
            }

        try:
            image_b64, width, height = self._capture_screen()
        except Exception as exc:  # noqa: BLE001 - desktop capture failures
            logger.exception("Could not capture the screen")
            return {
                "ok": False,
                "error": f"Could not capture the screen: {exc}",
            }

        try:
            answer = self._ask_vision_model(question, image_b64)
        except Exception as exc:  # noqa: BLE001 - model/network failures
            logger.exception("Screen vision request failed")
            detail = str(exc)
            lowered = detail.casefold()

            if "timed out" in lowered:
                hint = (
                    f"The local model did not finish within "
                    f"{self.settings.screen_timeout_seconds:.0f} seconds. "
                    "Thinking models such as 'qwen3-vl:8b' can spend the whole "
                    "budget on hidden reasoning before writing an answer. Use a "
                    "non-thinking model such as 'qwen3-vl:8b-instruct', lower "
                    "vision.screen_max_tokens, or raise "
                    "vision.screen_timeout_seconds."
                )
            elif (
                "mllama" in lowered
                or "unknown model architecture" in lowered
            ):
                hint = (
                    "llama3.2-vision uses the mllama architecture, which "
                    "Ollama 0.30 and newer no longer loads. Pull a supported "
                    "vision model (for example `ollama pull qwen3-vl:8b`) and "
                    "set vision.screen_model accordingly, or use an Ollama "
                    "version older than 0.30.0 if you must use "
                    "llama3.2-vision."
                )
            else:
                hint = (
                    f"Make sure '{self.settings.screen_model}' is available "
                    f"in Ollama with `ollama pull {self.settings.screen_model}`."
                )

            return {
                "ok": False,
                "error": f"Screen analysis failed: {detail[:500]}. {hint}",
            }

        return {
            "ok": True,
            "question": question,
            "answer": answer,
            "model": self.settings.screen_model,
            "screen": {"width": width, "height": height},
            "message": "Analyzed the primary screen with the local vision model.",
        }

    def status(self) -> dict[str, Any]:
        state = self._state_snapshot()

        try:
            indexed = self.store.count()
        except Exception:  # noqa: BLE001 - status must never fail the API
            indexed = int(state.get("indexed_images", 0))

        return {
            "enabled": self.settings.enabled,
            "phase": state.get("phase", "stopped"),
            "indexed_images": indexed,
            "model": self.settings.embed_model,
            "model_loaded": bool(
                getattr(self.embedder, "is_loaded", False)
            ),
            "roots": [str(root) for root in self.roots],
            "database": str(self.settings.resolved_index_path()),
            "batch_size": self.settings.batch_size,
            "scan_interval_seconds": self.settings.scan_interval_seconds,
            "max_images": self.settings.max_images,
            "scanned": state.get("scanned", 0),
            "added": state.get("added", 0),
            "updated": state.get("updated", 0),
            "unchanged": state.get("unchanged", 0),
            "skipped": state.get("skipped", 0),
            "removed": state.get("removed", 0),
            "last_scan_started": state.get("last_scan_started"),
            "last_scan_finished": state.get("last_scan_finished"),
            "last_error": state.get("last_error"),
        }
