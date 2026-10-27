"""Asynchronous GGUF downloads with live progress and SHA256 verification.

Each model is a small set of files (weights plus an optional vision projector).
Downloads are resumable: a partial ``.part`` file keeps its bytes, and a
checksum mismatch deletes the file instead of leaving a model that would crash
the loader later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.catalog import ModelEntry, ModelFile

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_PROGRESS_INTERVAL = 0.25
# Finished install jobs stay queryable for the UI, but the registry must not
# grow for the lifetime of the process.
_MAX_FINISHED_JOBS = 20


@dataclass
class InstallJob:
    id: str
    model_id: str
    state: str = "queued"  # queued | downloading | verifying | done | error | cancelled
    downloaded: int = 0
    total: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    file: str = ""
    message: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_id": self.model_id,
            "state": self.state,
            "downloaded": self.downloaded,
            "total": self.total,
            "speed_bps": round(self.speed_bps, 1),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds else None,
            "file": self.file,
            "message": self.message,
            "error": self.error,
            "percent": round(100 * self.downloaded / self.total, 1)
            if self.total
            else 0.0,
        }


class ModelStore:
    """Install, verify, and track GGUF models under the data folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "models.json"
        self._lock = threading.RLock()
        self._jobs: dict[str, InstallJob] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._state = self._load_state()

    # ------------------------------------------------------------- state
    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("models", {})
        return data

    def _save_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def installed_ids(self) -> set[str]:
        with self._lock:
            return set(self._state.get("models", {}))

    def is_installed(self, entry: ModelEntry) -> bool:
        with self._lock:
            record = self._state.get("models", {}).get(entry.id)
        if not record:
            return False
        for item in entry.files:
            target = self._file_path(entry, item)
            if not target.is_file():
                return False
        return True

    def record(self, entry: ModelEntry) -> dict[str, Any] | None:
        with self._lock:
            return self._state.get("models", {}).get(entry.id)

    def model_path(self, entry: ModelEntry) -> Path:
        item = entry.file("model")
        assert item is not None
        return self._file_path(entry, item)

    def mmproj_path(self, entry: ModelEntry) -> Path | None:
        item = entry.file("mmproj")
        return self._file_path(entry, item) if item else None

    def _file_path(self, entry: ModelEntry, item: ModelFile) -> Path:
        return self.models_dir / entry.id / item.filename

    # ----------------------------------------------------------- install
    def start_install(self, entry: ModelEntry) -> InstallJob:
        with self._lock:
            for job in self._jobs.values():
                if job.model_id == entry.id and job.state in {
                    "queued",
                    "downloading",
                    "verifying",
                }:
                    return job
            job = InstallJob(
                id=f"{entry.id}-{int(time.time() * 1000)}",
                model_id=entry.id,
                total=entry.total_bytes,
            )
            self._jobs[job.id] = job
            thread = threading.Thread(
                target=self._run_install,
                args=(entry, job),
                name=f"install-{entry.id}",
                daemon=True,
            )
            self._threads[job.id] = thread
            thread.start()
            return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancel.set()
        return True

    def job(self, job_id: str) -> InstallJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_jobs(self) -> list[InstallJob]:
        with self._lock:
            return [
                job
                for job in self._jobs.values()
                if job.state in {"queued", "downloading", "verifying"}
            ]

    def _run_install(self, entry: ModelEntry, job: InstallJob) -> None:
        try:
            job.state = "downloading"
            for item in entry.files:
                if job.cancel.is_set():
                    job.state = "cancelled"
                    job.message = "Cancelled"
                    return
                self._fetch(entry, item, job)
            job.state = "verifying"
            job.message = "Verifying"
            for item in entry.files:
                path = self._file_path(entry, item)
                digest = _sha256(path)
                if digest != item.sha256:
                    path.unlink(missing_ok=True)
                    raise ValueError(
                        f"Checksum mismatch for {item.filename}; the file was "
                        "deleted. Retry the download."
                    )
            with self._lock:
                self._state.setdefault("models", {})[entry.id] = {
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "bytes": entry.total_bytes,
                    "files": {
                        item.filename: str(self._file_path(entry, item))
                        for item in entry.files
                    },
                }
                self._save_state()
            job.state = "done"
            job.downloaded = job.total
            job.eta_seconds = 0
            job.message = "Installed"
        except _Cancelled:
            # Cancel can arrive mid-file; that is a normal stop, not a failure.
            job.state = "cancelled"
            job.error = ""
            job.message = "Cancelled"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            logger.warning("Model install failed for %s: %s", entry.id, exc)
            job.state = "error"
            job.error = str(exc)
            job.message = "Install failed"
        finally:
            self._prune_finished_jobs()

    def _prune_finished_jobs(self) -> None:
        """Keep only the newest finished jobs so the registry cannot grow forever.

        The frontend polls the job right after it finishes, so the most recent
        ones must stay available; older finished entries carry no state the UI
        can still reach.
        """
        with self._lock:
            finished = [
                job_id
                for job_id, job in self._jobs.items()
                if job.state in {"done", "error", "cancelled"}
            ]
            excess = len(finished) - _MAX_FINISHED_JOBS
            for job_id in finished[:excess] if excess > 0 else []:
                self._jobs.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _fetch(self, entry: ModelEntry, item: ModelFile, job: InstallJob) -> None:
        target = self._file_path(entry, item)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        job.file = item.filename

        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item.size:
            partial.unlink()
            offset = 0

        completed_before = sum(
            other.size
            for other in entry.files
            if other is not item and self._file_path(entry, other).is_file()
        )
        job.downloaded = completed_before + offset

        headers = {"User-Agent": "Compa/1.0 (+local model manager)"}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        # A stale .part whose offset no longer matches the remote file makes
        # the server answer 416. Reset the partial file and retry exactly once;
        # recursing made a misbehaving server able to exhaust the call stack.
        retried = False
        while True:
            request = urllib.request.Request(item.url, headers=headers)
            try:
                response = urllib.request.urlopen(request, timeout=120)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and partial.exists() and not retried:
                    retried = True
                    partial.unlink()
                    offset = 0
                    job.downloaded = completed_before
                    headers.pop("Range", None)
                    continue
                raise

        with response:
            if offset and response.status != 206:
                offset = 0
                partial.unlink(missing_ok=True)
            mode = "ab" if offset else "wb"
            last = time.monotonic()
            last_bytes = offset
            with partial.open(mode) as handle:
                while True:
                    if job.cancel.is_set():
                        raise _Cancelled()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    offset += len(chunk)
                    job.downloaded = completed_before + offset
                    now = time.monotonic()
                    if now - last >= _PROGRESS_INTERVAL:
                        speed = (offset - last_bytes) / max(now - last, 0.001)
                        job.speed_bps = speed
                        remaining = max(job.total - job.downloaded, 0)
                        job.eta_seconds = remaining / speed if speed > 0 else None
                        last, last_bytes = now, offset

        if offset != item.size:
            raise ValueError(
                f"{item.filename} is {offset} bytes but {item.size} were expected."
            )

        job.state = "verifying"
        job.message = f"Verifying {item.filename}"
        digest = _sha256(partial)
        if digest != item.sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(
                f"Checksum mismatch for {item.filename}; the download was deleted."
            )
        partial.replace(target)
        job.state = "downloading"
        job.message = ""

    def delete(self, entry: ModelEntry) -> bool:
        with self._lock:
            record = self._state.get("models", {}).pop(entry.id, None)
            if record is not None:
                self._save_state()
        directory = self.models_dir / entry.id
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        return record is not None


class _Cancelled(Exception):
    pass


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
