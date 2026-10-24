# Personal Agent

A local, private desktop AI assistant. A Tauri v2 desktop shell talks to a
FastAPI backend, which chats with a local Ollama model (Hermes 3), remembers the
current chat, and keeps a persistent, user-editable knowledge base about you.

## Features

- Local chat through Ollama (`hermes3`) — no cloud requests
- Full chat history in memory: the whole conversation is sent with every request
- Deleting a chat forgets that conversation
- Persistent knowledge base across chats and restarts, stored as an editable
  Markdown file
- The agent learns durable facts about you after each exchange (name, interests,
  projects, preferences, habits, behavior)
- Configurable identity: name, personality, and base system prompt
- Follows the language of the user's message and switches immediately when asked
  (for example Spanish, Chinese, English, or German), remembering the request
  for the rest of the conversation
- Graceful error messages when Ollama is offline, slow, or missing the model
- System tray with show/hide, quit, and close-to-tray behavior

## Structure

```
frontend/            Tauri v2 + React + TypeScript + Tailwind CSS
  src/               Chat UI
  src-tauri/         Rust shell, system tray, and Tauri configuration
backend/             FastAPI backend
  main.py            API entrypoint (/health, /identity, /chat, history)
  settings.py        Typed settings loader
  agent/chat.py      Ollama client, prompt assembly, and chat orchestration
  agent/prompt.py    System prompt built from identity + knowledge base
  agent/learning.py  Extracts durable user facts from each exchange
  memory/store.py    In-memory conversation history
  memory/knowledge.py File-backed, user-editable knowledge base
  tools/             Permissioned tools (allowlist placeholder)
  tests/             pytest suite
  .venv/             Local virtual environment (gitignored)
config/settings.json Backend, Ollama, identity, and memory settings
memory/              Knowledge base and local runtime data (gitignored)
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Rust toolchain and VS Build Tools with the C++ workload (required by Tauri)
- [Ollama](https://ollama.com) with the Hermes 3 model:

```powershell
ollama pull hermes3
ollama serve
```

## Configuration

Settings live in `config/settings.json`. The backend reads this file at
startup, so restart uvicorn after changing it.

```json
{
  "agent_name": "Nova",
  "personality": "Warm, calm, and practical.",
  "system_prompt": "Be concise but thorough. Ask clarifying questions. Never invent facts.",
  "ollama": {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "hermes3",
    "timeout_seconds": 120,
    "temperature": 0.7,
    "max_retries": 1
  },
  "agent": {
    "max_iterations": 5
  },
  "memory": {
    "knowledge_base_path": "memory/knowledge_base.md",
    "max_conversations": 100,
    "learn_from_chats": true
  }
}
```

The system message sent to Hermes 3 is composed from:

- `agent_name` — who the assistant says it is
- `personality` — tone and character
- `system_prompt` — base behavior rules and constraints
- the knowledge base file — long-term facts about the user

`memory.max_conversations` bounds how many chats are kept in RAM at once.
`memory.learn_from_chats` enables automatic knowledge-base updates.

## Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload
```

Endpoints:

- `GET /health` — backend status
- `GET /identity` — configured name, personality, and model
- `POST /chat` — `{"message": "...", "conversation_id": "optional-uuid"}`
- `GET /chat/history/{conversation_id}` — in-memory messages
- `DELETE /chat/history/{conversation_id}` — clear one conversation

For each `/chat` request the backend:

1. Uses the system prompt and knowledge base as the first message.
2. Appends the user message and sends the **entire** chat history to Hermes 3.
3. Appends the assistant reply and returns it.
4. In the background, asks Hermes 3 for new durable facts and adds them to the
   knowledge base (the reply is not delayed).

If Ollama is not running, `/chat` returns HTTP 503 with a readable message;
timeouts return 504, and other upstream failures return 502. The UI renders
that text in the chat instead of breaking.

To recreate the environment:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Memory

There are two separate kinds of memory:

**Chat history** is held strictly in memory per conversation ID. Every message
is kept, so the model can recall the whole chat. Restarting uvicorn or deleting
the chat clears it. It is never written to disk.

**The knowledge base** is `memory/knowledge_base.md`, a plain Markdown file that
persists across chats and restarts. The agent reads it on every request and
writes new durable facts into the matching section: identity/name, interests,
projects, preferences, habits and routines, or communication style. Facts that
do not fit a category go to `## Learned notes`. When something changes, the old
fact is replaced: name, age, location, and occupation are single-valued (the
newest value wins), and explicit corrections or “no longer” statements remove
the outdated fact before the new one is added. You can open and edit the file
at any time; edits apply to the next message without a restart. Deleting a chat
does not delete knowledge already learned from it, so remove facts from the
file if you want them forgotten.

If the knowledge base contains a language note under `## Communication style`,
it is context only. The assistant is not locked to a language: it replies in the
language of the current message and switches immediately when the user asks for
another language (for example Spanish, Chinese, English, or German). An explicit
request is remembered for the rest of the conversation until the user asks for
another language. No language is hardcoded or enforced. Temporary language
choices are not stored as durable knowledge-base facts.

The file starts with sections for identity/name, interests, projects,
preferences, habits and routines, communication style, and uncategorized notes.
The folder is gitignored, so personal data never lands in version control.

## Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest
```

The suite covers the API, identity endpoint, validation, full in-memory chat
history, the knowledge base, fact extraction and deduplication, prompt
composition, context assembly, and Ollama error mapping. Ollama is not required
for the tests.

## Frontend

Start Ollama and the backend first, then:

```powershell
cd frontend
npm install
npm run tauri dev
```

The app starts hidden in the system tray. Click the tray icon to show or hide
the chat window; right-click it for **Show / Hide** and **Quit**. Closing the
window hides it instead of quitting.

Set `VITE_API_URL` to override the default backend URL
`http://127.0.0.1:8000`.
