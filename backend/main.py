"""Personal Agent API entrypoint."""

from fastapi import FastAPI

app = FastAPI(
    title="Personal Agent API",
    description="Local backend for the personal desktop agent.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the backend is running."""
    return {
        "status": "ok",
        "service": "personal-agent-backend",
        "version": app.version,
    }
