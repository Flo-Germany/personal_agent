"""Personal Agent API entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Personal Agent API",
    description="Local backend for the personal desktop agent.",
    version="0.1.0",
)

# The Tauri webview and Vite dev server are the only allowed origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the backend is running."""
    return {
        "status": "ok",
        "service": "personal-agent-backend",
        "version": app.version,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """Return a static reply until the Ollama agent loop is implemented."""
    return ChatResponse(reply=f"Hello from FastAPI! You said: {payload.message}")
