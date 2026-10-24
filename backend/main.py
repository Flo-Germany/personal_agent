"""Personal Agent API entrypoint."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from agent.approvals import ApprovalNotFound
from agent.chat import ChatService, ConfirmationRequired, LLMError
from agent.prompt import load_personality
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings, load_settings
from tools.executor import ToolExecutor
from tools.vision_tools import VisionService

logger = logging.getLogger(__name__)

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


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str


class ApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: str | None = None


class HistoryResponse(BaseModel):
    conversation_id: str
    messages: list[HistoryMessage]


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
) -> FastAPI:
    settings = settings or load_settings()
    store = store or ConversationStore(
        max_conversations=settings.memory.max_conversations,
    )
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    knowledge.ensure_file()
    vision = vision_service or VisionService(
        settings.vision,
        ollama_base_url=settings.ollama.base_url,
        keep_alive_seconds=settings.ollama.keep_alive_seconds,
    )

    if chat_service is None:
        chat_service = ChatService(
            settings,
            store,
            knowledge,
            ToolExecutor(settings.tools, vision=vision),
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if settings.vision.enabled:
            vision.start()
        try:
            yield
        finally:
            await run_in_threadpool(vision.stop)

    app = FastAPI(
        title="Personal Agent API",
        description="Local backend for the personal desktop agent.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.vision = vision

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
        return IdentityResponse(
            name=settings.agent_name,
            personality=personality,
            model=settings.ollama.model,
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

        events = chat_service.resume(payload.approval_id, payload.approved)
        return stream_response(
            events,
            pending.conversation_id,
            learn_message=pending.user_message,
        )

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
        messages = [
            message
            for message in stored
            if message.get("role") in {"user", "assistant"}
            and str(message.get("content", "")).strip()
        ]
        return HistoryResponse(conversation_id=normalized, messages=messages)

    @app.delete("/chat/history/{conversation_id}", status_code=204)
    async def delete_history(conversation_id: UUID) -> Response:
        """Delete a conversation from memory and discard pending approvals."""
        normalized = str(conversation_id)
        await run_in_threadpool(chat_service.discard_pending, normalized)
        await run_in_threadpool(store.clear, normalized)
        return Response(status_code=204)

    return app


app = create_app()
