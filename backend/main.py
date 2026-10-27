"""Personal Agent API entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_serializer
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from agent.approvals import ApprovalError, ApprovalNotFound
from agent.chat import ChatService, ConfirmationRequired, LLMError
from agent.ollama_client import NativeOllamaClient
from agent.prompt import load_personality
from engine import EngineService, get_entry
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import DATA_ROOT, DEFAULT_CONFIG_PATH, Settings, load_settings
from tools.executor import ToolExecutor
from tools.vision_tools import VisionService

logger = logging.getLogger(__name__)


def _unload_ollama(settings: Settings) -> None:
    """Ask Ollama to unload its chat model so llama.cpp gets the VRAM.

    Both engines can be installed, and an Ollama runner holding 8.4 GB turns an
    8B Vulkan model into a slow partially-offloaded one. Unloading is best
    effort: a missing Ollama must never block the native engine.
    """
    if settings.engine.type != "llamacpp":
        return
    base = settings.ollama.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    payload = json.dumps(
        {
            "model": settings.ollama.model,
            "messages": [],
            "keep_alive": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
        logger.info("Unloaded Ollama model %s to free VRAM", settings.ollama.model)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.debug("Could not unload Ollama: %s", exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _noisy_logger in (
    "httpx",
    "httpx2",
    "huggingface_hub",
    "sentence_transformers",
    "urllib3",
):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: UUID | None = None


class EditableSettings(BaseModel):
    """The subset of settings the UI may change, with validation."""

    agent_name: str | None = Field(default=None, min_length=1, max_length=60)
    language: Literal["auto", "de", "en", "zh"] | None = None
    communication_style: Literal["concise", "conversational", "technical"] | None = None
    autonomy: Literal["cautious", "balanced", "autonomous"] | None = None
    context_window: int | None = Field(default=None, ge=2048, le=131072)
    keep_alive_seconds: int | None = Field(default=None, ge=-1, le=86_400)
    cpu_threads: int | None = Field(default=None, ge=1, le=256)
    process_priority: Literal["eco", "normal", "high"] | None = None
    gpu_layers: int | None = Field(default=None, ge=0, le=512)
    vram_override_gb: float | None = Field(default=None, gt=0, le=512)
    vision_max_width: int | None = Field(default=None, ge=640, le=3840)
    uia_depth: int | None = Field(default=None, ge=2, le=60)
    allow_foreground_fallback: bool | None = None


class KnowledgeContent(BaseModel):
    content: str = Field(default="", max_length=200_000)


def _editable_snapshot(settings: Settings, config_file: Path) -> dict[str, Any]:
    return {
        "agent_name": settings.agent_name,
        "language": settings.agent.language,
        "communication_style": settings.agent.communication_style,
        "autonomy": settings.agent.autonomy,
        "context_window": settings.engine.context_window or settings.ollama.context_window,
        "keep_alive_seconds": settings.ollama.keep_alive_seconds,
        "cpu_threads": settings.engine.cpu_threads,
        "process_priority": settings.engine.process_priority,
        "gpu_layers": settings.engine.gpu_layers,
        "vram_override_gb": settings.engine.vram_override_gb,
        "vision_max_width": settings.automation.screenshot_max_width,
        "uia_depth": settings.automation.max_element_depth,
        "allow_foreground_fallback": settings.automation.allow_foreground_fallback,
        "config_path": str(config_file),
        "engine_type": settings.engine.type,
        "runtime_model": settings.runtime_model,
    }


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


class DisplayChoiceRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    # None cancels the screen analysis.
    display: int | None = Field(default=None, ge=1, le=64)


class InterruptRequest(BaseModel):
    conversation_id: UUID


class ModelAction(BaseModel):
    model_id: str = Field(min_length=1, max_length=120)


class ActiveModelRequest(ModelAction):
    task: Literal["text", "vision"]


class EngineTypeRequest(BaseModel):
    type: Literal["ollama", "llamacpp"]


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: str | None = None
    thinking: str | None = None
    thinking_ms: float | None = None
    thinking_tokens: int | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_thinking(self, handler: Any) -> dict[str, Any]:
        """Drop the reasoning fields when a turn did not reason."""
        data = handler(self)
        for key in ("thinking", "thinking_ms", "thinking_tokens"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class HistoryResponse(BaseModel):
    conversation_id: str
    messages: list[HistoryMessage]
    diagnosis: dict[str, Any] | None = None
    searches: list[dict[str, Any]] = []


class IdentityResponse(BaseModel):
    name: str
    personality: str
    model: str


def _sse(event: dict[str, Any]) -> str:
    return (
        f"event: {event.get('type', 'message')}\n"
        f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
    )


def create_app(
    settings: Settings | None = None,
    store: ConversationStore | None = None,
    chat_service: ChatService | None = None,
    vision_service: VisionService | None = None,
    engine_service: EngineService | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    config_file = config_path or DEFAULT_CONFIG_PATH
    store = store or ConversationStore(
        max_conversations=settings.memory.max_conversations,
    )
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    knowledge.ensure_file()
    owned_chat_service = chat_service is None
    engine = engine_service or EngineService(
        settings.engine, DATA_ROOT / "engine-data"
    )
    vision = vision_service or VisionService(
        settings.vision,
        ollama_base_url=settings.ollama.base_url,
        keep_alive_seconds=settings.ollama.keep_alive_seconds,
        context_window=settings.ollama.context_window,
        vision_engine=engine,
    )
    vision.set_engine(engine)

    ollama_client = NativeOllamaClient(
        settings.ollama.base_url,
        timeout=settings.ollama.timeout_seconds,
        num_ctx=settings.ollama.context_window,
        keep_alive=settings.ollama.keep_alive_seconds,
        max_retries=settings.ollama.max_retries,
    )
    if chat_service is None:
        engine_client = None
        if settings.engine.type == "llamacpp":
            _unload_ollama(settings)
            try:
                engine_client = engine.text_client()
            except Exception as exc:  # noqa: BLE001 - fall back, never brick chat
                logger.warning(
                    "llama.cpp engine unavailable (%s); falling back to Ollama",
                    exc,
                )
        settings.runtime_model = (
            engine.model_label("text")
            if engine_client is not None
            else f"Ollama · {settings.ollama.model}"
        )
        chat_service = ChatService(
            settings,
            store,
            knowledge,
            ToolExecutor(
                settings.tools,
                vision=vision,
                automation=settings.automation,
            ),
            client=engine_client or ollama_client,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if settings.vision.enabled:
            vision.start()
        if owned_chat_service and settings.ollama.warmup_on_startup:
            # Load the model and prefill the stable system/tool prefix in the
            # background so the first real message does not pay an 8-12 second
            # cold-start penalty.
            threading.Thread(
                target=chat_service.warm_up,
                name="model-warmup",
                daemon=True,
            ).start()
        try:
            yield
        finally:
            shutdown_chat = getattr(chat_service, "shutdown", None)
            if callable(shutdown_chat):
                await run_in_threadpool(shutdown_chat)
            await run_in_threadpool(vision.stop)
            await run_in_threadpool(engine.shutdown)

    app = FastAPI(
        title="Personal Agent API",
        description="Local backend for the personal desktop agent.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.vision = vision
    app.state.engine = engine

    # The Tauri webview and Vite dev server are the only allowed origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Conversation-ID"],
    )

    def stream_response(
        events: Iterator[dict[str, Any]],
        conversation_id: str,
        learn_message: str | None = None,
    ) -> StreamingResponse:
        state = {"final_seen": False}

        async def generate() -> AsyncIterator[str]:
            try:
                async for event in iterate_in_threadpool(events):
                    if event.get("type") == "final":
                        state["final_seen"] = True
                    yield _sse(event)
            except Exception:  # noqa: BLE001 - headers are already sent
                logger.exception(
                    "Agent stream failed",
                    extra={"conversation_id": conversation_id},
                )
                yield _sse(
                    {
                        "type": "error",
                        "conversation_id": conversation_id,
                        "message": "The agent stream failed.",
                        "status": 500,
                    }
                )
            finally:
                # Close the sync generator even when the client disconnects so
                # its conversation lock is released and the partial turn rolls
                # back instead of leaking a held lock.
                close = getattr(events, "close", None)
                if close is not None:
                    await run_in_threadpool(close)

        def learn_if_completed() -> None:
            # Runs after the response finishes, in Starlette's threadpool.
            if state["final_seen"] and learn_message:
                schedule = getattr(chat_service, "schedule_learning", None)
                if schedule is not None:
                    schedule(conversation_id, learn_message)
                else:
                    chat_service.learn_from_exchange(learn_message)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Conversation-ID": conversation_id,
            },
            background=BackgroundTask(learn_if_completed),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the backend is running."""
        return {
            "status": "ok",
            "service": "personal-agent-backend",
            "version": app.version,
        }

    @app.get("/identity", response_model=IdentityResponse)
    async def identity() -> IdentityResponse:
        """Expose the configured assistant identity to the UI."""
        personality = await run_in_threadpool(load_personality, settings)
        model = settings.runtime_model or settings.ollama.model
        return IdentityResponse(
            name=settings.agent_name,
            personality=personality,
            model=model,
        )

    @app.get("/settings/editable")
    async def get_editable_settings() -> dict[str, Any]:
        """Return the settings the UI is allowed to change."""
        return {"ok": True, "settings": _editable_snapshot(settings, config_file)}

    @app.patch("/settings/editable")
    async def patch_editable_settings(payload: EditableSettings) -> dict[str, Any]:
        """Apply and persist the editable settings.

        Engine-related values need a server reload, so the response reports
        ``restart_required`` instead of pretending they are live.
        """
        # exclude_unset keeps an explicit null meaningful: it clears an
        # optional override (GPU layers, VRAM, CPU threads).
        changes = payload.model_dump(exclude_unset=True)
        restart_fields = {
            "context_window",
            "keep_alive_seconds",
            "cpu_threads",
            "process_priority",
            "gpu_layers",
            "vram_override_gb",
        }
        restart_required = bool(restart_fields & set(changes))

        if changes.get("agent_name") is not None:
            settings.agent_name = str(changes["agent_name"]).strip()
        if changes.get("language") is not None:
            settings.agent.language = changes["language"]
        if changes.get("communication_style") is not None:
            settings.agent.communication_style = changes["communication_style"]
        if changes.get("autonomy") is not None:
            settings.agent.autonomy = changes["autonomy"]
        if changes.get("context_window") is not None:
            settings.ollama.context_window = int(changes["context_window"])
            settings.engine.context_window = int(changes["context_window"])
        if changes.get("keep_alive_seconds") is not None:
            settings.ollama.keep_alive_seconds = int(changes["keep_alive_seconds"])
        if "cpu_threads" in changes:
            value = changes["cpu_threads"]
            settings.engine.cpu_threads = None if value is None else int(value)
        if changes.get("process_priority") is not None:
            settings.engine.process_priority = changes["process_priority"]
        if "gpu_layers" in changes:
            value = changes["gpu_layers"]
            settings.engine.gpu_layers = None if value is None else int(value)
        if "vram_override_gb" in changes:
            value = changes["vram_override_gb"]
            settings.engine.vram_override_gb = (
                None if value is None else float(value)
            )
        if changes.get("vision_max_width") is not None:
            value = int(changes["vision_max_width"])
            settings.automation.screenshot_max_width = value
            settings.vision.screen_max_width = value
        if changes.get("uia_depth") is not None:
            settings.automation.max_element_depth = int(changes["uia_depth"])
        if "allow_foreground_fallback" in changes:
            settings.automation.allow_foreground_fallback = bool(
                changes["allow_foreground_fallback"]
            )

        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(
                json.dumps(settings.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - report a readable write error
            raise HTTPException(
                status_code=500,
                detail=f"Could not save the settings file: {exc}",
            ) from exc

        return {
            "ok": True,
            "settings": _editable_snapshot(settings, config_file),
            "restart_required": restart_required,
            "message": (
                "Saved. Restart the engine to apply the inference changes."
                if restart_required
                else "Saved."
            ),
        }

    @app.get("/knowledge")
    async def get_knowledge() -> dict[str, Any]:
        content = knowledge.read()
        return {
            "ok": True,
            "path": str(knowledge.path),
            "content": content,
            "chars": len(content),
        }

    @app.put("/knowledge")
    async def put_knowledge(payload: KnowledgeContent) -> dict[str, Any]:
        path = knowledge.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.with_suffix(path.suffix + ".bak").write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            path.write_text(payload.content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Could not save the knowledge base: {exc}"
            ) from exc
        return {"ok": True, "chars": len(payload.content), "path": str(path)}

    @app.delete("/knowledge")
    async def clear_knowledge() -> dict[str, Any]:
        path = knowledge.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.with_suffix(path.suffix + ".bak").write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8"
                )
                path.unlink()
            knowledge.ensure_file()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"Could not reset the knowledge base: {exc}"
            ) from exc
        return {"ok": True, "content": knowledge.read(), "path": str(path)}

    @app.get("/diagnostics/config")
    async def diagnostics_config() -> dict[str, Any]:
        """Expose non-secret runtime knobs for local evaluation harnesses."""
        return {
            "agent_name": settings.agent_name,
            "model": settings.ollama.model,
            "thinking": settings.ollama.thinking,
            "thinking_min_chars": settings.ollama.thinking_min_chars,
            "ollama_base_url": settings.ollama.base_url,
            "keep_alive_seconds": settings.ollama.keep_alive_seconds,
            "context_window": settings.ollama.context_window,
            "dynamic_tools": settings.agent.dynamic_tools,
            "vision_enabled": settings.vision.enabled,
            "engine": settings.engine.type,
            "engine_text_model": engine.active_id("text"),
            "engine_vision_model": engine.active_id("vision"),
            "engine_vram_gb": engine.hardware.vram_gb,
            "runtime_model": settings.runtime_model,
            "data_root": str(DATA_ROOT),
            "log_level": logging.getLevelName(logging.getLogger().level),
        }

    # ------------------------------------------------------- Phase 22 engine
    @app.get("/engine/status")
    async def engine_status() -> dict[str, Any]:
        """Hardware profile, runtime, active models, and live servers."""
        return await run_in_threadpool(engine.status)

    @app.get("/models/catalog")
    async def models_catalog() -> dict[str, Any]:
        return {
            "models": await run_in_threadpool(engine.catalog),
            "hardware": engine.hardware.as_dict(),
            "engine": settings.engine.type,
        }

    @app.post("/models/install")
    async def models_install(payload: ModelAction) -> dict[str, Any]:
        try:
            job = engine.install_model(payload.model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return job.as_dict()

    @app.get("/models/progress/{job_id}")
    async def models_progress(job_id: str) -> StreamingResponse:
        """SSE stream of bytes, speed, and ETA for one install job."""

        async def events() -> AsyncIterator[str]:
            while True:
                job = engine.job(job_id)
                if job is None:
                    yield _sse({"type": "error", "detail": "unknown job"})
                    return
                yield _sse({"type": "progress", **job.as_dict()})
                if job.state in {"done", "error", "cancelled"}:
                    return
                await asyncio.sleep(0.3)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/models/cancel")
    async def models_cancel(payload: dict[str, str]) -> dict[str, bool]:
        job_id = str(payload.get("job_id") or "")
        return {"cancelled": engine.store.cancel(job_id)}

    @app.post("/models/active")
    async def models_active(payload: ActiveModelRequest) -> dict[str, Any]:
        """Hot-swap the active model with a hard VRAM flush."""
        try:
            result = await run_in_threadpool(
                engine.switch, payload.task, payload.model_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if payload.task == "text" and hasattr(chat_service, "set_client"):
            client = None
            if settings.engine.type == "llamacpp":
                _unload_ollama(settings)
                try:
                    client = await run_in_threadpool(engine.text_client)
                except Exception as exc:  # noqa: BLE001 - keep chat usable
                    logger.warning("Could not bind the llama.cpp client: %s", exc)
            chat_service.set_client(client or ollama_client)
            settings.runtime_model = (
                engine.model_label("text")
                if client is not None
                else f"Ollama · {settings.ollama.model}"
            )
        return result

    @app.delete("/models/{model_id}")
    async def models_delete(model_id: str) -> dict[str, bool]:
        try:
            entry = get_entry(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": await run_in_threadpool(engine.delete_model, entry.id)}

    @app.post("/engine/runtime/install")
    async def engine_runtime_install() -> dict[str, Any]:
        job = engine.install_runtime()
        return {
            "id": job.id,
            "state": job.state,
            "downloaded": job.downloaded,
            "total": job.total,
        }

    @app.post("/engine/type")
    async def engine_type(payload: EngineTypeRequest) -> dict[str, Any]:
        """Switch between Ollama and llama.cpp without restarting the app."""
        try:
            result = await run_in_threadpool(engine.set_type, payload.type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if hasattr(chat_service, "set_client"):
            client = None
            if payload.type == "llamacpp":
                _unload_ollama(settings)
                try:
                    client = await run_in_threadpool(engine.text_client)
                except Exception as exc:  # noqa: BLE001 - keep chat usable
                    logger.warning("Could not bind the llama.cpp client: %s", exc)
            chat_service.set_client(client or ollama_client)
            settings.runtime_model = (
                engine.model_label("text")
                if client is not None
                else f"Ollama · {settings.ollama.model}"
            )
        return result

    @app.post("/engine/restart")
    async def engine_restart() -> dict[str, Any]:
        """Reload the inference process so saved tuning takes effect.

        Threads, context, layer count and priority are server arguments, so the
        running llama-server is stopped and the text client is rebound.
        """
        engine.servers.stop_all()
        try:
            result = await run_in_threadpool(engine.set_type, settings.engine.type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if hasattr(chat_service, "set_client"):
            client = None
            if settings.engine.type == "llamacpp":
                try:
                    client = await run_in_threadpool(engine.text_client)
                except Exception as exc:  # noqa: BLE001 - keep chat usable
                    logger.warning("Could not reload the llama.cpp client: %s", exc)
            chat_service.set_client(client or ollama_client)
            settings.runtime_model = (
                engine.model_label("text")
                if client is not None
                else f"Ollama - {settings.ollama.model}"
            )
        return result

    @app.get("/engine/runtime/progress/{job_id}")
    async def engine_runtime_progress(job_id: str) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            while True:
                job = engine.runtime_job(job_id)
                if job is None:
                    yield _sse({"type": "error", "detail": "unknown job"})
                    return
                percent = (
                    round(100 * job.downloaded / job.total, 1) if job.total else 0
                )
                yield _sse(
                    {
                        "type": "progress",
                        "id": job.id,
                        "state": job.state,
                        "downloaded": job.downloaded,
                        "total": job.total,
                        "percent": percent,
                        "error": job.error,
                    }
                )
                if job.state in {"done", "error"}:
                    return
                await asyncio.sleep(0.3)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/vision/status")
    async def vision_status() -> dict[str, Any]:
        """Report global image-index progress and crawl statistics."""
        return await run_in_threadpool(vision.status)

    @app.post("/chat/stream")
    async def chat_stream(payload: ChatRequest) -> StreamingResponse:
        """Stream a user turn, pausing when approval is required."""
        conversation_id = str(payload.conversation_id or uuid4())
        events = chat_service.start_turn(conversation_id, payload.message)
        return stream_response(
            events,
            conversation_id,
            learn_message=payload.message,
        )

    @app.post("/chat/approve")
    async def chat_approve(payload: ApprovalRequest) -> StreamingResponse:
        """Approve or deny a pending action and stream the continuation."""
        try:
            pending = chat_service.get_pending(payload.approval_id)
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if pending.kind == "display":
            raise HTTPException(
                status_code=409,
                detail=(
                    "This card is a display choice; use POST /chat/display."
                ),
            )

        events = chat_service.resume(payload.approval_id, payload.approved)
        return stream_response(
            events,
            pending.conversation_id,
            learn_message=pending.user_message,
        )

    @app.post("/chat/display")
    async def chat_display(payload: DisplayChoiceRequest) -> StreamingResponse:
        """Continue a paused screen analysis with the chosen display."""
        try:
            pending = chat_service.get_pending(payload.approval_id)
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            events = chat_service.resume_display(
                payload.approval_id,
                payload.display,
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return stream_response(
            events,
            pending.conversation_id,
            learn_message=pending.user_message,
        )

    @app.post("/chat/interrupt", status_code=204)
    async def chat_interrupt(payload: InterruptRequest) -> Response:
        """Ask the active turn to stop after the current operation."""
        await run_in_threadpool(
            chat_service.interrupt,
            str(payload.conversation_id),
        )
        return Response(status_code=204)

    @app.get("/chat/pending/{conversation_id}")
    async def get_pending(conversation_id: UUID) -> dict[str, Any]:
        """Return a pending approval for a conversation, if any."""
        pending = chat_service.get_pending_for_conversation(str(conversation_id))
        return {"pending": pending.to_public() if pending is not None else None}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        payload: ChatRequest,
        background_tasks: BackgroundTasks,
    ) -> ChatResponse:
        """Non-streaming compatibility endpoint for simple chats."""
        conversation_id = str(payload.conversation_id or uuid4())

        try:
            reply = await run_in_threadpool(
                chat_service.generate_reply,
                conversation_id,
                payload.message,
            )
        except ConfirmationRequired as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "User approval is required.",
                    "action": exc.pending.to_public(),
                },
            )
        except LLMError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        schedule = getattr(chat_service, "schedule_learning", None)
        if schedule is not None:
            background_tasks.add_task(
                schedule,
                conversation_id,
                payload.message,
            )
        else:
            background_tasks.add_task(
                chat_service.learn_from_exchange,
                payload.message,
            )
        return ChatResponse(reply=reply, conversation_id=conversation_id)

    @app.get("/chat/history/{conversation_id}", response_model=HistoryResponse)
    async def get_history(conversation_id: UUID) -> HistoryResponse:
        """Return the user-visible chat history for a conversation."""
        normalized = str(conversation_id)
        stored = await run_in_threadpool(store.load, normalized)
        messages: list[dict[str, Any]] = []
        for message in stored:
            if message.get("role") not in {"user", "assistant"}:
                continue
            if not str(message.get("content", "")).strip():
                continue
            item: dict[str, Any] = {
                "role": message.get("role"),
                "content": message.get("content", ""),
                "created_at": message.get("created_at"),
            }
            # Only present when the model actually reasoned, so the payload of
            # a normal turn is unchanged.
            if message.get("thinking"):
                item["thinking"] = message["thinking"]
                if message.get("thinking_ms") is not None:
                    item["thinking_ms"] = message["thinking_ms"]
                if message.get("thinking_tokens") is not None:
                    item["thinking_tokens"] = message["thinking_tokens"]
            messages.append(item)
        # Attach the diagnosis card only to the latest turn so an older report
        # never appears after a newer, unrelated answer.
        turn_start = 0
        for index, message in enumerate(stored):
            if message.get("role") == "user":
                turn_start = index

        diagnosis: dict[str, Any] | None = None
        for message in stored[turn_start + 1 :]:
            if message.get("role") != "tool":
                continue
            if message.get("name") != "system_diagnosis":
                continue
            try:
                parsed = json.loads(message.get("content") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict) and parsed.get("ok"):
                diagnosis = parsed
                break
        return HistoryResponse(
            conversation_id=normalized,
            messages=messages,
            diagnosis=diagnosis,
            searches=(
                chat_service.search_cards(normalized)
                if hasattr(chat_service, "search_cards")
                else []
            ),
        )

    @app.delete("/chat/history/{conversation_id}", status_code=204)
    async def delete_history(conversation_id: UUID) -> Response:
        """Delete a conversation from memory and discard pending approvals."""
        normalized = str(conversation_id)
        await run_in_threadpool(chat_service.discard_pending, normalized)
        if hasattr(chat_service, "clear_search_cards"):
            await run_in_threadpool(chat_service.clear_search_cards, normalized)
        await run_in_threadpool(store.clear, normalized)
        return Response(status_code=204)

    return app


app = create_app()
