"""Facade tying hardware, catalog, downloads, and llama-server together."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.catalog import CATALOG, VISION, ModelEntry, get_entry, models_for
from engine.client import LlamaServerClient
from engine.downloads import InstallJob, ModelStore
from engine.gguf import read_gguf
from engine.hardware import HardwareProfile, LayerPlan, detect_hardware, plan_gpu_layers
from engine.runtime import RuntimeManager
from engine.server import LlamaServerManager, ServerSpec
from settings import DEFAULT_CONFIG_PATH, EngineSettings

logger = logging.getLogger(__name__)


@dataclass
class RuntimeJob:
    id: str
    state: str = "queued"
    downloaded: int = 0
    total: int = 0
    error: str = ""


class EngineService:
    """Own the local inference runtime and the selected models."""

    def __init__(
        self,
        settings: EngineSettings,
        root: Path,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.root = Path(root)
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime = RuntimeManager(self.root / "runtime")
        self.store = ModelStore(self.root)
        self.servers = LlamaServerManager(
            self.runtime,
            self.root / "logs",
            ports={"text": 8123, "vision": 8124},
            start_timeout=settings.start_timeout_seconds,
        )
        self.hardware: HardwareProfile = detect_hardware(settings.vram_override_gb)
        self._state_path = self.root / "engine.json"
        self._state = self._load_state()
        self._lock = threading.RLock()
        self._runtime_job: RuntimeJob | None = None
        self._text_client: LlamaServerClient | None = None
        self._text_client_model: str | None = None

    # ---------------------------------------------------------------- state
    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        active = data.get("active")
        if not isinstance(active, dict):
            active = {}
        active.setdefault("text", self.settings.text_model)
        active.setdefault("vision", self.settings.vision_model)
        data["active"] = active
        return data

    def _save_state(self) -> None:
        temporary = self._state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temporary.replace(self._state_path)

    def active_id(self, task: str) -> str:
        return str(self._state["active"].get(task) or "")

    def model_label(self, task: str = "text", *, ollama_model: str = "") -> str:
        """A human label for the model that is actually serving the task."""
        if self.settings.type != "llamacpp":
            return f"Ollama Â· {ollama_model}".strip(" Â·")
        model_id = self.active_id(task)
        entry = next((item for item in CATALOG if item.id == model_id), None)
        if entry is None:
            return model_id or "no model"
        server = self.servers.status(task)
        layers = server.get("n_gpu_layers")
        if isinstance(layers, int) and layers > 0:
            hardware = f"{layers} GPU layers"
            if self.hardware.gpu_name:
                hardware += f" on {self.hardware.gpu_name}"
        else:
            hardware = "CPU"
        return f"{entry.name} ({entry.quantization}) Â· llama.cpp Â· {hardware}"

    def set_active(self, task: str, model_id: str) -> None:
        self._state["active"][task] = model_id
        self._save_state()

    # --------------------------------------------------------- engine switch
    def set_type(self, new_type: str) -> dict[str, Any]:
        """Switch the inference transport at runtime and persist the choice."""
        if new_type not in {"ollama", "llamacpp"}:
            raise ValueError("engine type must be 'ollama' or 'llamacpp'.")

        with self._lock:
            if new_type == "llamacpp":
                if not self.runtime.status().installed:
                    raise ValueError(
                        "Install the llama.cpp runtime before switching engines."
                    )
                model_id = self.active_id("text")
                entry = next((item for item in CATALOG if item.id == model_id), None)
                if entry is None or not self.store.is_installed(entry):
                    raise ValueError(
                        "Install a text model before switching to llama.cpp."
                    )

            self.settings.type = new_type
            self._persist_type(new_type)
            self._text_client = None
            self._text_client_model = None
            if new_type == "ollama":
                self.servers.stop_all()

            logger.info("Inference transport switched to %s", new_type)
            return {
                "engine": new_type,
                "active": {
                    "text": self.active_id("text"),
                    "vision": self.active_id("vision"),
                },
                "runtime": self.runtime.status().as_dict(),
                "servers": {
                    role: self.servers.status(role)
                    for role in ("text", "vision")
                },
            }

    def _persist_type(self, new_type: str) -> None:
        """Write engine.type into the active settings file without losing keys."""
        try:
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            engine_block = data.get("engine")
            if not isinstance(engine_block, dict):
                engine_block = {}
            engine_block["type"] = new_type
            data["engine"] = engine_block
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(self.config_path)
        except OSError as exc:
            logger.warning("Could not persist the engine type: %s", exc)

    # -------------------------------------------------------------- catalog
    def catalog(self) -> list[dict[str, Any]]:
        installed = self.store.installed_ids()
        active_text = self.active_id("text")
        active_vision = self.active_id("vision")
        entries: list[dict[str, Any]] = []
        for entry in CATALOG:
            data = entry.as_dict()
            data["installed"] = entry.id in installed
            data["active"] = entry.id in {active_text, active_vision}
            data["fit"] = self._fit_label(entry)
            data["likely_fits_vram"] = (
                self.hardware.vram_gb >= entry.min_vram_gb
                if self.hardware.vram_gb
                else False
            )
            entries.append(data)
        return entries

    def _fit_label(self, entry: ModelEntry) -> str:
        if not self.hardware.vram_gb:
            return "cpu"
        if self.hardware.vram_gb + 1e-6 >= entry.min_vram_gb:
            return "gpu"
        return "partial"

    def plan(self, entry: ModelEntry, context: int) -> LayerPlan:
        model_path = self.store.model_path(entry)
        if model_path.is_file():
            info = read_gguf(model_path)
        else:
            # Not installed yet: approximate with the catalog metadata. This is
            # only used for the UI preview; the real plan is computed on switch.
            from engine.gguf import GgufInfo

            info = GgufInfo(
                path=model_path,
                file_size=entry.file("model").size if entry.file("model") else 0,
                block_count=40,
                embedding_length=4096,
                head_count=32,
                head_count_kv=[8],
            )
        return plan_gpu_layers(
            self.hardware,
            info,
            context,
            reserve_gb=self.settings.vram_reserve_gb,
            override=self.settings.gpu_layers,
        )

    # --------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        runtime = self.runtime.status()
        active_text = self.active_id("text")
        active_vision = self.active_id("vision")
        text_entry = next((item for item in CATALOG if item.id == active_text), None)
        vision_entry = next(
            (item for item in CATALOG if item.id == active_vision), None
        )
        text_plan = None
        if text_entry is not None:
            text_plan = self.plan(text_entry, self._context_for(text_entry)).as_dict()
        return {
            "engine": self.settings.type,
            "hardware": self.hardware.as_dict(),
            "runtime": runtime.as_dict(),
            "active": {"text": active_text, "vision": active_vision},
            "installed": sorted(self.store.installed_ids()),
            "text_model": text_entry.name if text_entry else None,
            "vision_model": vision_entry.name if vision_entry else None,
            "text_plan": text_plan,
            "servers": {
                role: self.servers.status(role) for role in ("text", "vision")
            },
            "runtime_job": self._runtime_job_as_dict(),
        }

    def _runtime_job_as_dict(self) -> dict[str, Any] | None:
        job = self._runtime_job
        if job is None:
            return None
        return {
            "id": job.id,
            "state": job.state,
            "downloaded": job.downloaded,
            "total": job.total,
            "percent": round(100 * job.downloaded / job.total, 1) if job.total else 0,
            "error": job.error,
        }

    def _context_for(self, entry: ModelEntry) -> int:
        if self.settings.context_window:
            return int(self.settings.context_window)
        return entry.context

    def _device(self) -> str:
        """Vulkan device for llama-server; empty means CPU/auto."""
        configured = (self.settings.device or "").strip()
        if configured.casefold() == "cpu":
            return ""
        if configured:
            return configured
        return "Vulkan0" if self.hardware.vulkan else ""

    # -------------------------------------------------------------- runtime
    def runtime_status(self) -> dict[str, Any]:
        return self.runtime.status().as_dict()

    def install_runtime(self) -> RuntimeJob:
        with self._lock:
            if self._runtime_job is not None and self._runtime_job.state in {
                "queued",
                "downloading",
            }:
                return self._runtime_job
            job = RuntimeJob(
                id=f"runtime-{int(time.time() * 1000)}",
                total=self.runtime.status().size_bytes or 37_051_563,
            )
            self._runtime_job = job
        thread = threading.Thread(
            target=self._run_runtime_install, args=(job,), daemon=True
        )
        thread.start()
        return job

    def _run_runtime_install(self, job: RuntimeJob) -> None:
        job.state = "downloading"

        def progress(done: int, total: int) -> None:
            job.downloaded = done
            job.total = total

        try:
            self.runtime.install(progress)
            job.state = "done"
            job.downloaded = job.total
            logger.info("llama.cpp runtime installed at %s", self.runtime.directory)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            job.state = "error"
            job.error = str(exc)
            logger.warning("Runtime install failed: %s", exc)

    def runtime_job(self, job_id: str) -> RuntimeJob | None:
        job = self._runtime_job
        return job if job is not None and job.id == job_id else None

    # --------------------------------------------------------------- models
    def install_model(self, model_id: str) -> InstallJob:
        entry = get_entry(model_id)
        return self.store.start_install(entry)

    def job(self, job_id: str) -> InstallJob | None:
        return self.store.job(job_id)

    def delete_model(self, model_id: str) -> bool:
        entry = get_entry(model_id)
        if self.active_id("text") == model_id:
            self.servers.stop("text")
        if self.active_id("vision") == model_id:
            self.servers.stop("vision")
        return self.store.delete(entry)

    def switch(self, task: str, model_id: str) -> dict[str, Any]:
        """Hot-swap the active model, flushing VRAM first."""
        entry = get_entry(model_id)
        if entry.task != task:
            raise ValueError(f"{entry.name} is a {entry.task} model, not {task}.")
        if not self.store.is_installed(entry):
            raise ValueError(f"{entry.name} is not installed yet.")

        with self._lock:
            previous_id = self.active_id(task)
            self.set_active(task, model_id)
            if task == "text":
                self._text_client = None
                self._text_client_model = None
            self.servers.stop(task)

            context = self._context_for(entry)
            plan = self.plan(entry, context)
            if task != "text":
                # Vision stays unloaded until a screen task needs it
                # (keep_alive: 0); selecting it only flushes the old server.
                return {
                    "task": task,
                    "model_id": model_id,
                    "port": self.servers.port_for(task),
                    "n_gpu_layers": plan.n_gpu_layers,
                    "plan": plan.as_dict(),
                    "vram_flushed": True,
                    "started": False,
                }

            spec = ServerSpec(
                role=task,
                model_id=entry.id,
                model_path=self.store.model_path(entry),
                mmproj_path=None,
                context=context,
                n_gpu_layers=plan.n_gpu_layers,
                port=self.servers.port_for(task),
                device=self._device(),
                threads=self.settings.cpu_threads or 0,
                priority=self.settings.process_priority,
                kv_cache=self.settings.kv_cache,
            )
            try:
                running = self.servers.ensure(spec, force=True)
            except Exception:
                # The switch stopped the previous server before the new one was
                # known to work. Keep the persisted state matching the model the
                # user actually had, so the next turn can start it again.
                if previous_id and previous_id != model_id:
                    try:
                        self.set_active(task, previous_id)
                    except OSError:
                        logger.warning(
                            "Could not restore the previous active model",
                            exc_info=True,
                        )
                raise
            return {
                "task": task,
                "model_id": model_id,
                "port": running.spec.port,
                "n_gpu_layers": plan.n_gpu_layers,
                "plan": plan.as_dict(),
                "vram_flushed": True,
                "started": True,
                "startup_seconds": round(time.time() - running.started_at, 2),
            }

    # --------------------------------------------------------------- clients
    def text_client(self) -> LlamaServerClient | None:
        """Return a client for the active text model, starting it if needed."""
        if self.settings.type != "llamacpp":
            return None
        model_id = self.active_id("text")
        if not model_id:
            return None
        entry = next((item for item in CATALOG if item.id == model_id), None)
        if entry is None or not self.store.is_installed(entry):
            logger.info("llama.cpp engine selected but %s is not installed", model_id)
            return None

        with self._lock:
            if (
                self._text_client is not None
                and self._text_client_model == f"{model_id}:{self.hardware.vram_bytes}"
                and self.servers.status("text").get("running")
            ):
                return self._text_client

            context = self._context_for(entry)
            plan = self.plan(entry, context)
            spec = ServerSpec(
                role="text",
                model_id=entry.id,
                model_path=self.store.model_path(entry),
                mmproj_path=None,
                context=context,
                n_gpu_layers=plan.n_gpu_layers,
                port=self.servers.port_for("text"),
                device=self._device(),
                threads=self.settings.cpu_threads or 0,
                priority=self.settings.process_priority,
            kv_cache=self.settings.kv_cache,
            )
            running = self.servers.ensure(spec)
            client = LlamaServerClient(
                self.servers.base_url("text"),
                timeout=180.0,
                model_label=entry.name,
            )
            self._text_client = client
            self._text_client_model = f"{model_id}:{self.hardware.vram_bytes}"
            logger.info(
                "llama.cpp text engine ready: %s on port %s with %s/%s GPU layers",
                entry.name,
                running.spec.port,
                plan.n_gpu_layers,
                plan.total_layers,
            )
            return client

    def ensure_vision(self) -> str:
        """Start the active vision model and return its OpenAI base URL."""
        model_id = self.active_id("vision")
        entry = next((item for item in CATALOG if item.id == model_id), None)
        if entry is None or not self.store.is_installed(entry):
            raise ValueError("No vision model is installed and selected.")
        context = self._context_for(entry)
        plan = self.plan(entry, context)
        spec = ServerSpec(
            role="vision",
            model_id=entry.id,
            model_path=self.store.model_path(entry),
            mmproj_path=self.store.mmproj_path(entry),
            context=context,
            n_gpu_layers=plan.n_gpu_layers,
            port=self.servers.port_for("vision"),
            device=self._device(),
            threads=self.settings.cpu_threads or 0,
            priority=self.settings.process_priority,
            kv_cache=self.settings.kv_cache,
        )
        self.servers.ensure(spec)
        return self.servers.base_url("vision")

    def release_vision(self) -> bool:
        """Enforce immediate unload after a vision task (keep_alive: 0)."""
        return self.servers.release("vision")

    def shutdown(self) -> None:
        self.servers.stop_all()
