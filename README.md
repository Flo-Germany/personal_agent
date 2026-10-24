# Personal Agent

A local, private desktop AI assistant. A Tauri v2 desktop shell talks to a
FastAPI backend, which will drive tool calls and a local Ollama model (Hermes 3).

## Structure

```
frontend/            Tauri v2 + React + TypeScript + Tailwind CSS
  src/               Chat shell UI
  src-tauri/         Rust desktop shell and Tauri configuration
backend/             FastAPI backend
  main.py            API entrypoint
  agent/             ReAct loop (not implemented yet)
  tools/             Permissioned tools (allowlist placeholder)
  .venv/             Local virtual environment (gitignored)
config/settings.json Local backend, Ollama, and agent settings
memory/              Local conversations/databases (gitignored)
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Rust toolchain (required by Tauri): https://rustup.rs
- Ollama with the `hermes3` model for later stages

## Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload
```

Health check: <http://127.0.0.1:8000/health>

To recreate the environment:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Frontend

```powershell
cd frontend
npm install
npm run tauri dev
```

The UI is currently a static chat shell; backend chat integration, tray behavior,
and the tool loop are intentionally not implemented yet.
