"""Pinned llama.cpp Vulkan runtime (prebuilt, no compiler required).

The archive ships ``llama-server.exe`` plus the Vulkan and CPU backends. The
same build runs on machines without a Vulkan device because llama.cpp falls
back to its CPU kernels automatically.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

RUNTIME_TAG = "b11900"
RUNTIME_ASSET = f"llama-{RUNTIME_TAG}-bin-win-vulkan-x64.zip"
RUNTIME_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{RUNTIME_TAG}/{RUNTIME_ASSET}"
)
RUNTIME_SHA256 = (
    "baf0183b1906d7cc0cfe9a0bd3fae34ceb4048acecc371ff3b7a91456783d051"
)
RUNTIME_SIZE = 37_051_563
RUNTIME_DIR_NAME = f"llama-{RUNTIME_TAG}-vulkan"

ProgressCallback = Callable[[int, int], None]


class RuntimeError_(RuntimeError):
    """Raised when the llama.cpp runtime cannot be installed or located."""


@dataclass
class RuntimeStatus:
    installed: bool
    version: str
    directory: str
    server: str
    size_bytes: int

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "version": self.version,
            "directory": self.directory,
            "server": self.server,
            "size_bytes": self.size_bytes,
        }


class RuntimeManager:
    """Locate or install the pinned llama.cpp build."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / RUNTIME_DIR_NAME
        self.archive = self.root / RUNTIME_ASSET
        self.server_path = self.directory / "llama-server.exe"

    def status(self) -> RuntimeStatus:
        installed = self.server_path.is_file()
        size = 0
        if installed:
            size = sum(
                item.stat().st_size
                for item in self.directory.rglob("*")
                if item.is_file()
            )
        return RuntimeStatus(
            installed=installed,
            version=RUNTIME_TAG,
            directory=str(self.directory),
            server=str(self.server_path),
            size_bytes=size,
        )

    def ensure(self) -> Path:
        if not self.server_path.is_file():
            raise RuntimeError_(
                "The llama.cpp runtime is not installed yet. Install it from "
                "Settings > Models before selecting a llama.cpp engine."
            )
        return self.server_path

    def install(self, progress: ProgressCallback | None = None) -> RuntimeStatus:
        self.root.mkdir(parents=True, exist_ok=True)
        self._download(progress)
        self._extract()
        status = self.status()
        if not status.installed:
            raise RuntimeError_(
                "The llama.cpp archive did not contain llama-server.exe."
            )
        return status

    def _download(self, progress: ProgressCallback | None) -> None:
        if self.archive.is_file():
            if _sha256(self.archive) == RUNTIME_SHA256:
                return
            self.archive.unlink()

        logger.info("Downloading llama.cpp %s from %s", RUNTIME_TAG, RUNTIME_URL)
        request = urllib.request.Request(
            RUNTIME_URL,
            headers={"User-Agent": "Compa/1.0 (+local model manager)"},
        )
        hasher = hashlib.sha256()
        done = 0
        with urllib.request.urlopen(request, timeout=120) as response, self.archive.open(
            "wb"
        ) as handle:
            total = int(response.headers.get("Content-Length") or RUNTIME_SIZE)
            while True:
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)

        digest = hasher.hexdigest()
        if digest != RUNTIME_SHA256:
            self.archive.unlink(missing_ok=True)
            raise RuntimeError_(
                "The llama.cpp runtime checksum did not match. The download was "
                "discarded; check the network or proxy and retry."
            )

    def _extract(self) -> None:
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.archive) as archive:
            for member in archive.infolist():
                target = (self.directory / member.filename).resolve()
                if not str(target).startswith(str(self.directory.resolve())):
                    raise RuntimeError_("The runtime archive contains unsafe paths.")
            archive.extractall(self.directory)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
