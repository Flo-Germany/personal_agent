# Personal Agent

A local, private desktop AI assistant. A Tauri v2 desktop shell talks to a
FastAPI backend, which will eventually drive tool calls and a local Ollama
model (Hermes 3).

## Structure

```
frontend/            Tauri v2 + React + TypeScript + Tailwind CSS
  src/               Chat UI
  src-tauri/         Rust shell, system tray, and Tauri configuration
backend/             FastAPI backend
  main.py            API entrypoint (/health, /chat)
  agent/             ReAct loop (not implemented yet)
  tools/             Permissioned tools (allowlist placeholder)
  .venv/             Local virtual environment (gitignored)
config/settings.json Local backend, Ollama, and agent settings
memory/              Local conversations/databases (gitignored)
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Rust toolchain and VS Build Tools with the C++ workload (required by Tauri)
- Ollama with the `hermes3` model for later stages

## Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload
```

- Health check: <http://127.0.0.1:8000/health>
- Chat (mock): `POST http://127.0.0.1:8000/chat` with `{"message": "Hello"}`

To recreate the environment:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Frontend

Start the backend first, then:

```powershell
cd frontend
npm install
npm run tauri dev
```

The app starts hidden in the system tray. Click the tray icon to show or hide
the chat window; right-click it for **Show / Hide** and **Quit**. Closing the
window hides it instead of quitting.

The UI calls the local FastAPI `/chat` endpoint and currently displays its mock
reply. Set `VITE_API_URL` to override the default `http://127.0.0.1:8000`.
