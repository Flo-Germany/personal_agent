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
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

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
    device: str = ""
    threads: int = 0
    priority: str = "normal"
    # "q8_0" cuts KV-cache bandwidth and measured ~50% faster decode at long
    # context; "q4_0" measured ~2x. "f16" is the llama.cpp default.
    kv_cache: str = "q8_0"

    def matches(self, other: "ServerSpec") -> bool:
        return (
            self.model_id == other.model_id
            and self.model_path == other.model_path
            and self.mmproj_path == other.mmproj_path
            and self.context == other.context
            and self.n_gpu_layers == other.n_gpu_layers
            and self.port == other.port
            and self.device == other.device
            and self.threads == other.threads
            and self.priority == other.priority
            and self.kv_cache == other.kv_cache
        )


@dataclass
class RunningServer:
    spec: ServerSpec
    process: subprocess.Popen[bytes]
    started_at: float
    log_path: Path


def server_arguments(server_path: Path, spec: ServerSpec, kv_cache: str) -> list[str]:
    """Build the llama-server command line for one role.

    Quantized KV cache is the largest measurable decode win at the prompt sizes
    the agent sends; `-fa auto` stays on because it was faster both at short and
    long context on Vulkan.
    """
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
    if kv_cache in {"q8_0", "q4_0"}:
        arguments.extend(["-ctk", kv_cache, "-ctv", kv_cache])
    if spec.mmproj_path is not None:
        arguments.extend(["--mmproj", str(spec.mmproj_path)])
    if spec.device:
        arguments.extend(["--device", spec.device])
    if spec.threads > 0:
        arguments.extend(["--threads", str(spec.threads)])
    return arguments


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
            props = _props(server.spec.port) or {}
            loaded = props.get("model_path")
            return {
                "role": role,
                "running": server.process.poll() is None,
                "model_id": server.spec.model_id,
                "port": server.spec.port,
                "n_gpu_layers": server.spec.n_gpu_layers,
                "context": server.spec.context,
                "loaded_model": loaded,
                "model_matches": _same_path(loaded, server.spec.model_path),
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
            and _serves_model(spec.port, spec.model_path)
        ):
            return existing

        if existing is not None:
            logger.info("Hot-swapping the %s engine", spec.role)
            self.stop(spec.role)

        try:
            return self._spawn(spec, kv_cache=spec.kv_cache)
        except (RuntimeError, TimeoutError) as exc:
            if not spec.kv_cache:
                raise
            # Never leave the app without a model because a build or driver
            # rejects the quantized KV type: retry once at the default f16.
            logger.warning(
                "llama-server %s rejected the %s KV cache (%s); retrying at f16",
                spec.role,
                spec.kv_cache,
                exc,
            )
            return self._spawn(spec, kv_cache="")

    def _spawn(self, spec: ServerSpec, *, kv_cache: str) -> RunningServer:
        server_path = self.runtime.ensure()
        self._reclaim_port(spec.port)
        self._wait_port_free(spec.port)
        arguments = server_arguments(server_path, spec, kv_cache)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"llama-{spec.role}-{stamp}.log"
        log_handle = log_path.open("ab")
        creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
        logger.info(
            "Starting %s llama-server: %s", spec.role, " ".join(arguments[1:])
        )
        try:
            process = subprocess.Popen(
                arguments,
                cwd=str(server_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        finally:
            # The child owns its duplicated handle. Keeping the parent's copy
            # open leaked one descriptor per model/engine swap.
            log_handle.close()
        if spec.priority != "normal":
            _apply_priority(getattr(process, "pid", 0), spec.priority)
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
            props = _props(spec.port)
            if props is not None and _same_path(
                props.get("model_path"), spec.model_path
            ):
                logger.info(
                    "llama-server %s is ready on port %s with %s",
                    spec.role,
                    spec.port,
                    spec.model_path.name,
                )
                return running
            time.sleep(0.5)

        served = _props(spec.port) or {}
        self.stop(spec.role)
        raise TimeoutError(
            f"llama-server did not serve {spec.model_path.name} within "
            f"{self.start_timeout:g}s (port {spec.port} served "
            f"{served.get('model_path') or 'nothing'}). "
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
    def _reclaim_port(self, port: int) -> None:
        """Remove stale llama-server processes left on our port.

        A dev harness or a killed backend can leave one *or more* llama-server
        processes behind (Windows lets several bind the same port when they set
        SO_REUSEADDR). Accepting the old server's ``/health`` made the app
        believe the new model was loaded while the old one kept answering.
        Every llama-server on the port is reclaimed; anything else is reported.
        """
        pids = _listening_pids(port)
        if not pids:
            return

        killed: list[psutil.Process] = []
        foreign: list[str] = []
        for pid in pids:
            try:
                process = psutil.Process(pid)
                name = (process.name() or "").casefold()
                exe = (process.exe() or "").casefold()
            except (psutil.Error, OSError):
                foreign.append(f"pid {pid}")
                continue

            if name == "llama-server.exe" or exe.endswith("llama-server.exe"):
                logger.warning(
                    "Reclaiming orphaned llama-server pid %s on port %s",
                    pid,
                    port,
                )
                children: list[psutil.Process] = []
                try:
                    children = process.children(recursive=True)
                except psutil.Error:
                    children = []
                for child in children:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                try:
                    process.kill()
                except psutil.Error:
                    pass
                killed.extend([process, *children])
            else:
                foreign.append(f"{name or exe} (pid {pid})")

        if foreign:
            raise RuntimeError(
                f"Port {port} is used by {', '.join(foreign)}. Close it, or "
                "change the engine port settings."
            )
        if killed:
            psutil.wait_procs(killed, timeout=10)
        self._wait_port_free(port)

    def _wait_port_free(self, port: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _listening_pids(port):
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
    return _props(port, timeout=timeout) is not None


def _apply_priority(pid: int, priority: str) -> None:
    """Set the llama-server process priority (eco/normal/high)."""
    if priority == "normal" or not pid:
        return
    try:
        import psutil

        process = psutil.Process(pid)
        if priority == "eco":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        elif priority == "high":
            process.nice(psutil.HIGH_PRIORITY_CLASS)
    except Exception:  # noqa: BLE001 - priority is best effort
        logger.debug("Could not set process priority %s", priority, exc_info=True)


def _props(port: int, timeout: float = 1.5) -> dict[str, Any] | None:
    """Return llama-server's /props (loaded model, context) or None."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/props", timeout=timeout
        ) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def _serves_model(port: int, model_path: Path) -> bool:
    props = _props(port)
    return bool(props) and _same_path(props.get("model_path"), model_path)


def _same_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )
    except (OSError, ValueError):
        return False


def _listening_pids(port: int) -> list[int]:
    pids: set[int] = set()
    try:
        for connection in psutil.net_connections(kind="inet"):
            if (
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and connection.laddr.port == port
                and connection.pid
            ):
                pids.add(int(connection.pid))
    except (psutil.Error, OSError):
        return []
    return sorted(pids)
