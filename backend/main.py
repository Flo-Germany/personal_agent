"""Personal Agent API entrypoint."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent.chat import ChatService, LLMError
from memory.knowledge import KnowledgeBase
from memory.store import ConversationStore
from settings import Settings, load_settings

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


class HistoryMessage(BaseModel):
    role: str
    content: str
    created_at: str | None = None


class HistoryResponse(BaseModel):
    conversation_id: str
    messages: list[HistoryMessage]


class IdentityResponse(BaseModel):
    name: str
    personality: str
    model: str


def create_app(
    settings: Settings | None = None,
    store: ConversationStore | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or ConversationStore(
        max_conversations=settings.memory.max_conversations,
    )
    knowledge = KnowledgeBase(settings.memory.resolved_knowledge_path())
    knowledge.ensure_file()
    chat_service = chat_service or ChatService(settings, store, knowledge)

    app = FastAPI(
        title="Personal Agent API",
        description="Local backend for the personal desktop agent.",
        version="0.1.0",
    )

    # The Tauri webview and Vite dev server are the only allowed origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Report that the backend is running."""
        return {
            "status": "ok",
            "service": "personal-agent-backend",
            "version": app.version,
        }

    @app.get("/identity", response_model=IdentityResponse)
    def identity() -> IdentityResponse:
        """Expose the configured assistant identity to the UI."""
        return IdentityResponse(
            name=settings.agent_name,
            personality=settings.personality,
            model=settings.ollama.model,
        )

    @app.post("/chat", response_model=ChatResponse)
    def chat(payload: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
        """Send a message to the local LLM and remember the exchange."""
        conversation_id = str(payload.conversation_id or uuid4())

        try:
            reply = chat_service.generate_reply(conversation_id, payload.message)
        except LLMError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        # Learn durable facts without delaying the visible reply.
        background_tasks.add_task(
            chat_service.learn_from_exchange,
            payload.message,
        )

        return ChatResponse(reply=reply, conversation_id=conversation_id)

    @app.get("/chat/history/{conversation_id}", response_model=HistoryResponse)
    def get_history(conversation_id: UUID) -> HistoryResponse:
        """Return the in-memory chat history for a conversation."""
        normalized = str(conversation_id)
        messages = store.load(normalized)
        return HistoryResponse(conversation_id=normalized, messages=messages)

    @app.delete("/chat/history/{conversation_id}", status_code=204)
    def delete_history(conversation_id: UUID) -> Response:
        """Delete a conversation from memory."""
        store.clear(str(conversation_id))
        return Response(status_code=204)

    return app


app = create_app()
