"""Managed llama-server processes with hard VRAM flushing and hot swapping.

The text role stays alive between turns for instant responses; the vision role
is stopped after each use so a 6 GB projector plus weights never sits idle in
VRAM. Stopping the process is the only reliable cross-vendor VRAM flush on
Windows; the driver reclaims the allocation when the process exits.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.runtime import RuntimeManager

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x0800_0000


@dataclass
class ServerSpec:
    role: str
    model_id: str
    model_path: Path
    mmproj_path: Path | None
    context: int
    n_gpu_layers: int
    port: int

    def matches(self, other: "ServerSpec") -> bool:
        return (
            self.model_id == other.model_id
            and self.model_path == other.model_path
            and self.mmproj_path == other.mmproj_path
            and self.context == other.context
            and self.n_gpu_layers == other.n_gpu_layers
            and self.port == other.port
        )


@dataclass
class RunningServer:
    spec: ServerSpec
    process: subprocess.Popen[bytes]
    started_at: float
    log_path: Path


class LlamaServerManager:
    """Own the llama-server processes for the text and vision roles."""

    def __init__(
        self,
        runtime: RuntimeManager,
        log_dir: Path,
        *,
        ports: dict[str, int] | None = None,
        start_timeout: float = 180.0,
    ) -> None:
        self.runtime = runtime
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ports = ports or {"text": 8123, "vision": 8124}
        self.start_timeout = start_timeout
        self._servers: dict[str, RunningServer] = {}

    # -------------------------------------------------------------- status
    def status(self, role: str | None = None) -> dict[str, Any]:
        if role is not None:
            server = self._servers.get(role)
            if server is None:
                return {"role": role, "running": False}
            return {
                "role": role,
                "running": server.process.poll() is None,
                "model_id": server.spec.model_id,
                "port": server.spec.port,
                "n_gpu_layers": server.spec.n_gpu_layers,
                "context": server.spec.context,
                "uptime_seconds": round(time.time() - server.started_at, 1),
                "log": str(server.log_path),
            }
        return {
            "roles": [self.status(name) for name in sorted(self._servers)],
            "ports": dict(self.ports),
        }

    def port_for(self, role: str) -> int:
        return self.ports.get(role, 8123)

    def base_url(self, role: str) -> str:
        return f"http://127.0.0.1:{self.port_for(role)}/v1"

    # --------------------------------------------------------------- start
    def ensure(self, spec: ServerSpec, *, force: bool = False) -> RunningServer:
        """Start or hot-swap the role so it matches ``spec``."""
        existing = self._servers.get(spec.role)
        if (
            not force
            and existing is not None
            and existing.process.poll() is None
            and existing.spec.matches(spec)
            and _healthy(spec.port)
        ):
            return existing

        if existing is not None:
            logger.info("Hot-swapping the %s engine", spec.role)
            self.stop(spec.role)

        server_path = self.runtime.ensure()
        self._wait_port_free(spec.port)
        arguments = [
            str(server_path),
            "-m",
            str(spec.model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(spec.port),
            "-c",
            str(spec.context),
            "-ngl",
            str(spec.n_gpu_layers),
            "--jinja",
            "--parallel",
            "1",
            "-fa",
            "auto",
        ]
        if spec.mmproj_path is not None:
            arguments.extend(["--mmproj", str(spec.mmproj_path)])

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"llama-{spec.role}-{stamp}.log"
        log_handle = log_path.open("ab")
        creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        logger.info(
            "Starting %s llama-server: %s", spec.role, " ".join(arguments[1:])
        )
        process = subprocess.Popen(
            arguments,
            cwd=str(server_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        running = RunningServer(
            spec=spec,
            process=process,
            started_at=time.time(),
            log_path=log_path,
        )
        self._servers[spec.role] = running

        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._servers.pop(spec.role, None)
                raise RuntimeError(
                    f"llama-server exited during startup with code "
                    f"{process.returncode}. {self._log_tail(log_path)}"
                )
            if _healthy(spec.port):
                logger.info("llama-server %s is ready on port %s", spec.role, spec.port)
                return running
            time.sleep(0.5)

        self.stop(spec.role)
        raise TimeoutError(
            f"llama-server did not become healthy within {self.start_timeout:g}s. "
            f"{self._log_tail(log_path)}"
        )

    # ---------------------------------------------------------------- stop
    def stop(self, role: str) -> bool:
        """Terminate the role and wait until its VRAM is released."""
        server = self._servers.pop(role, None)
        if server is None:
            return False
        process = server.process
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=_CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._wait_port_free(server.spec.port)
        logger.info("llama-server %s stopped; VRAM released", role)
        return True

    def stop_all(self) -> None:
        for role in list(self._servers):
            self.stop(role)

    def release(self, role: str) -> bool:
        """Unload a role after a bounded task (vision)."""
        return self.stop(role)

    # -------------------------------------------------------------- helpers
    def _wait_port_free(self, port: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return
            time.sleep(0.25)

    @staticmethod
    def _log_tail(path: Path, lines: int = 12) -> str:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])


def _healthy(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as response:
            body = response.read(4096)
            if response.status != 200:
                return False
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return True
            return str(data.get("status", "ok")).lower() in {"ok", "ready"}
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
