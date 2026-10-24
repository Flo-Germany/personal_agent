"""API tests for the local FastAPI backend."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_reports_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "personal-agent-backend",
        "version": app.version,
    }


def test_chat_echoes_message() -> None:
    response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    assert response.json() == {"reply": "Hello from FastAPI! You said: Hello"}


def test_chat_accepts_unicode_and_newlines() -> None:
    message = 'Grüße 🚀 日本\n"quoted" <b>'
    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json() == {"reply": f"Hello from FastAPI! You said: {message}"}


def test_chat_accepts_maximum_length_message() -> None:
    message = "a" * 8000
    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json()["reply"].endswith(message)


def test_chat_rejects_empty_message() -> None:
    response = client.post("/chat", json={"message": ""})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_short"


def test_chat_rejects_missing_message() -> None:
    response = client.post("/chat", json={})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "missing"


def test_chat_rejects_too_long_message() -> None:
    response = client.post("/chat", json={"message": "a" * 8001})

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"


def test_cors_allows_tauri_origin() -> None:
    response = client.options(
        "/chat",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"
