# Personal Agent

A local, private desktop AI assistant. A Tauri v2 desktop shell talks to a
FastAPI backend, which chats with a local Ollama model (Hermes 3), remembers the
current chat, keeps a persistent knowledge base, and can work with the Windows
file system and applications through a Human-in-the-Loop approval flow.

## Features

- Local chat through Ollama (`hermes3`) — no cloud requests
- Full chat history in memory: the whole conversation is sent with every request
- Persistent knowledge base across chats and restarts, stored as an editable
  Markdown file
- Agentic OS tools: search the file system, read files, create folders, copy,
  move, delete (Recycle Bin), unzip archives, and launch allowlisted apps
- Human-in-the-Loop approvals for every modifying action
- Real-time SSE status updates: thinking, tool started, tool result, approval
  needed, final answer
- Protected Windows locations and Recycle Bin deletion for destructive actions
- Configurable identity: name, personality, and base system prompt
- Follows the language of the user's message and switches when asked; no
  language is hardcoded
- Graceful error messages when Ollama is offline, slow, or missing the model
- System tray with show/hide, quit, and close-to-tray behavior

## Structure

```
frontend/            Tauri v2 + React + TypeScript + Tailwind CSS
  src/App.tsx        Streaming chat UI, status timeline, approval card
  src-tauri/         Rust shell, system tray, and Tauri configuration
backend/             FastAPI backend
  main.py            API entrypoint (SSE, approvals, history)
  settings.py        Typed settings loader
  agent/chat.py      Ollama client, resumable agent loop, HITL pauses
  agent/approvals.py Pending approval store with TTL and idempotency
  agent/prompt.py    Identity + memory + tool/safety prompt
  agent/learning.py  Extracts durable user facts from each exchange
  agent/language.py  Detects an explicitly requested reply language
  memory/store.py    In-memory conversation history (including tool messages)
  memory/knowledge.py File-backed, user-editable knowledge base
  tools/os_tools.py  Full-filesystem Windows operations and safety rules
  tools/schemas.py   OpenAI-compatible tool definitions
  tools/executor.py  Validated tool dispatch and confirmation flags
  tests/             pytest suite
  .venv/             Local virtual environment (gitignored)
config/settings.json Backend, Ollama, identity, memory, and tool settings
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
    "max_iterations": 5,
    "approval_ttl_seconds": 600
  },
  "memory": {
    "knowledge_base_path": "memory/knowledge_base.md",
    "max_conversations": 100,
    "learn_from_chats": true
  },
  "tools": {
    "max_read_bytes": 1000000,
    "max_search_results": 50,
    "max_search_files": 20000,
    "max_search_seconds": 30,
    "max_unzip_bytes": 209715200
  }
}
```

The system message sent to Hermes 3 is composed from:

- `agent_name` — who the assistant says it is
- `personality` — tone and character
- `system_prompt` — base behavior rules and constraints
- the knowledge base file — long-term facts about the user
- the tool rules: read-only vs. approval-required actions, protected locations,
  and the instruction to verify with search/read before modifying

`memory.max_conversations` bounds how many chats are kept in RAM.
`memory.learn_from_chats` enables automatic knowledge-base updates.
`agent.max_iterations` bounds tool rounds per segment.
`agent.approval_ttl_seconds` controls how long a pending approval remains valid.
`tools.*` caps file reads, searches, and zip extraction.

## Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload
```

Endpoints:

- `GET /health` — backend status
- `GET /identity` — configured name, personality, and model
- `POST /chat/stream` — send a message; returns `text/event-stream`
- `POST /chat/approve` — `{"approval_id": "...", "approved": true|false}`;
  resumes the paused agent and returns a stream
- `GET /chat/pending/{conversation_id}` — restores a pending approval after a UI
  reload
- `POST /chat` — non-streaming compatibility endpoint for simple chats
- `GET /chat/history/{conversation_id}` — user-visible messages (tool internals
  are hidden)
- `DELETE /chat/history/{conversation_id}` — clear the chat and any pending
  approval

### SSE event types

```text
status          thinking, tool_started, approval
tool_result     tool name, ok flag, human-readable summary, raw result
action_required approval_id, conversation_id, action descriptions
assistant       partial assistant text before a tool call (when present)
final           final natural-language reply
error           readable error and HTTP-like status
```

The SSE stream ends after `final`, `error`, or `action_required`.

### Agent loop and HITL

1. The user message is appended and the entire history plus tool schemas is sent
   to Hermes 3.
2. Read-only tools (`search_system`, `read_file_content`) run immediately and
   stream `tool_started` / `tool_result` events.
3. Any modifying tool pauses the loop. The assistant tool-call message is stored,
   a pending approval is created, and an `action_required` event is streamed.
   The HTTP stream then closes; no thread waits for a click.
4. `POST /chat/approve` either executes the pending actions and appends
   `role: "tool"` results, or appends an “Action cancelled by user” result.
   It then continues the agent loop and streams the next steps.
5. Pending approvals are one per conversation, expire after the configured TTL,
   and are consumed exactly once. Unknown or reused IDs return 404.
6. If the user closes the UI, `GET /chat/pending/{id}` restores the approval card.
   Tool messages stay internal; the history endpoint returns only user/assistant
   text.

If Ollama is not running, streams emit a readable error event; the legacy
`POST /chat` returns HTTP 503. Timeouts map to 504 and other upstream failures to
502.

## Tools and security

The agent can use nine tools:

| Tool | Confirmation | Purpose |
|---|---|---|
| `search_system` | No | Recursive or direct-child search by name/glob, extension, size, modified time, hidden flag, or all drives; newest/oldest sorting; known-folder aliases |
| `read_file_content` | No | Read text files; binary/image files return metadata only |
| `create_folder` | Yes | Create a folder anywhere outside protected locations |
| `create_file` | Yes | Create a UTF-8 text file, without overwriting unless explicitly requested |
| `copy_file` | Yes | Copy a file; destination must not exist |
| `move_file_or_folder` | Yes | Move a file/folder, including across drives; no overwrite |
| `delete_file_or_folder` | Yes | Move a file/folder to the Windows Recycle Bin |
| `unzip_file` | Yes | Extract a `.zip` safely into any non-protected folder |
| `start_program` | Yes | Launch only `calculator`, `notepad`, `explorer`, or `paint` |

Security boundaries:

- No sandbox. The tools can reach the whole file system, as requested.
- Modifying actions always require explicit approval; read-only actions do not.
- Critical locations (`Windows`, `Program Files`, `Program Files (x86)`,
  `ProgramData`, `Boot`, `Recovery`, drive roots, `C:\Users`, and the user
  profile root) are hard-denied for create/copy/move/delete/unzip even after
  approval.
- Deletes go to the Recycle Bin, not permanent deletion.
- Copy/move never overwrite an existing destination.
- Zip extraction rejects path escapes, caps entry count, and caps uncompressed
  size.
- Searches cap results, scanned files, and elapsed time.
- `start_program` uses fixed executable paths, no shell, no arguments, and no
  arbitrary programs.
- The agent has no shell command, arbitrary write-content, or registry tool.

Search resolves the real Windows known folders for `Documents`, `Downloads`,
`Desktop`, `Pictures`, `Music`, and `Videos`, including OneDrive redirection, so
a guessed `C:\Users\<name>\Documents` still reaches the actual Documents folder.
It supports recursive or direct-child searches, newest/oldest and size/name
sorting, and a requested result count. Zero/empty optional filters are treated
as unset, a provided root is searched before other drives, and for “last N
files” questions the backend removes invented filters, searches direct children
of the named folder, and sorts by modification time. Malformed tool-call JSON
from the model is either executed or rejected; it is never shown to the user.

Tool errors are fed back to the model so it can explain the problem. Tool side
effects are not undone if a later model call fails, but the failed chat turn is
removed from memory.

## Memory

Chat history is held strictly in memory per conversation ID. Every message is
kept, including internal tool-call/tool-result messages used by the model, so the
agent can continue a paused task. Restarting uvicorn or deleting the chat clears
it. It is never written to disk.

The knowledge base is `memory/knowledge_base.md`, a plain Markdown file that
persists across chats and restarts. The agent reads it on every request and
writes new durable facts into the matching section: identity/name, interests,
projects, preferences, habits and routines, or communication style. Facts that
do not fit a category go to `## Learned notes`. When something changes, the old
fact is replaced: name, age, location, and occupation are single-valued (the
newest value wins), and explicit corrections or “no longer” statements remove
the outdated fact before the new one is added. You can open and edit the file at
any time; edits apply to the next message without a restart. Deleting a chat does
not delete knowledge already learned from it, so remove facts from the file if
you want them forgotten.

If the knowledge base contains a language note under `## Communication style`,
it is context only. The assistant is not locked to a language: it replies in the
language of the current message and switches immediately when the user asks for
another language. An explicit request is remembered for the rest of the
conversation. Temporary language choices are not stored as durable facts.

## Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest
```

The suite covers the API, identity endpoint, validation, full in-memory chat
history, the knowledge base, fact extraction and deduplication, prompt
composition, context assembly, Ollama error mapping, full-filesystem tool
behavior, protected paths, Recycle Bin deletion, tool schemas, dispatch,
HITL pause/approve/deny, approval TTL/idempotency, SSE frames, and rollback.
Ollama is not required for the tests.

## Frontend

Start Ollama and the backend first, then:

```powershell
cd frontend
npm install
npm run tauri dev
```

The app starts hidden in the system tray. Click the tray icon to show or hide the
chat window; right-click it for **Show / Hide** and **Quit**. Closing the window
hides it instead of quitting.

The chat UI consumes `POST /chat/stream`, renders intermediate statuses, shows an
**Action Required** card with **Approve/Deny** for modifying tools, and restores a
pending approval after a reload. Set `VITE_API_URL` to override the default
backend URL `http://127.0.0.1:8000`.

## Known limitation

Hermes 3 (8B) can stall on a long, multi-file plan before issuing the first tool
call. The pipeline itself is validated with exact single-action and multi-step
tests; for complex chores, splitting the request into smaller steps (search,
create, move a file) gives more reliable results. A larger tool-calling model can
use the same loop without backend changes.
