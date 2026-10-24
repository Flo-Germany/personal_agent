# Personal Agent

A local, private desktop AI assistant. A Tauri v2 desktop shell talks to a
FastAPI backend, which chats with a local Ollama model (`qwen3-vl:8b-instruct`
by default), remembers the
current chat, keeps a persistent knowledge base, and can work with the Windows
file system and applications through a Human-in-the-Loop approval flow.

## Features

- Local chat through Ollama (`qwen3-vl:8b-instruct`) — no cloud requests
- Full chat history kept in memory and shown in the UI; the model receives the
  latest 10 messages plus the system prompt for fast responses
- Persistent knowledge base across chats and restarts, stored as an editable
  Markdown file
- Agentic OS tools: search the file system, read files, create folders, copy,
  move, delete (Recycle Bin), unzip archives, open files in a chosen editor, and
  launch any installed program
- Universal PowerShell access: read-only inspections run immediately; every
  state-changing command requires explicit approval with a description and the
  exact command shown first
- Window awareness and UI Automation text reading, plus local screen analysis
  with `qwen3-vl:8b-instruct`
- Human-in-the-Loop approval once per request: the card lists the first step and
  approving lets the agent finish the remaining steps in order automatically
- Multi-step and sequence requests ("first do this, then that") run to completion
  without a second approval card
- Compact in-place tool status: one animated row per task, with expandable
  stderr/exit-code details when an action fails
- Real-time SSE status updates: thinking, tool started, tool result, approval
  needed, final answer
- Protected Windows locations and Recycle Bin deletion for destructive actions
- Configurable identity: name in `settings.json`, personality in
  `config/personality.md`, and a base system prompt; the agent knows its own
  name, what it is, and which machine it runs on
- Follows the language of the user's message and switches when asked; no
  language is hardcoded
- Graceful error messages when Ollama is offline, slow, or missing the model
- System tray with show/hide, quit, and close-to-tray behavior
- Dark, minimal chat UI with Markdown rendering (GFM tables, task lists, and
  syntax-highlighted code blocks with copy buttons)
- Rich local-file rendering: image thumbnail galleries plus interactive file
  cards with open, show-in-folder, and copy-path actions
- Frameless, transparent window with rounded corners, a custom drag region, and
  a hide-to-tray window button

## Structure

```
frontend/            Tauri v2 + React + TypeScript + Tailwind CSS
  src/App.tsx        App shell: gradient frame, chat layout, empty state
  src/components/    Header, messages, Markdown, file cards, approval card, composer
  src/hooks/         Streaming chat state and approval flow
  src/lib/           API/SSE client, local-file parsing, quick prompts, Tauri actions
  src-tauri/         Rust shell, system tray, and Tauri configuration
backend/             FastAPI backend
  main.py            API entrypoint (SSE, approvals, history)
  settings.py        Typed settings loader
  agent/chat.py      Agent loop, prompt assembly, HITL pauses, native Ollama calls
  agent/ollama_client.py Native /api/chat client that sets the context window
  agent/approvals.py Pending approval store with TTL and idempotency
  agent/prompt.py    Identity + personality + memory + tool/safety prompt
  agent/learning.py  Extracts durable user facts from each exchange
  agent/language.py  Detects an explicitly requested reply language
  memory/store.py    In-memory conversation history (including tool messages)
  memory/knowledge.py File-backed, user-editable knowledge base
  tools/os_tools.py  Full-filesystem Windows operations and safety rules
  tools/window_tools.py Window listing and UI Automation text extraction
  tools/vision_tools.py Global CLIP image crawler, SQLite vector store, screen vision
  tools/schemas.py   OpenAI-compatible tool definitions
  tools/executor.py  Validated tool dispatch and confirmation flags
  tests/             pytest suite
  .venv/             Local virtual environment (gitignored)
config/settings.json   Backend, Ollama, identity, memory, vision, and tool settings
config/personality.md  Compa's editable personality and character sheet
memory/                Knowledge base, image index, and local data (gitignored)
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- Rust toolchain and VS Build Tools with the C++ workload (required by Tauri)
- [Ollama](https://ollama.com) with the recommended model:

```powershell
ollama pull qwen3-vl:8b-instruct
ollama serve
```

`qwen3-vl:8b-instruct` is already required for screen awareness, so using it for
chat keeps just one model loaded. The backend also supports Hermes 3
(`ollama pull hermes3`) by changing `ollama.model`; the Hermes template needs a
different system-prompt delivery, which the backend handles automatically.

The first semantic image search downloads the `clip-ViT-B-32` model (about
600 MB) into the Hugging Face cache. After that, indexing and search run fully
offline. Screen awareness uses the local `qwen3-vl:8b-instruct` model in Ollama; no
screenshot leaves the machine.

> **Vision model compatibility:** Ollama 0.30 and newer no longer load the
> `mllama` architecture used by `llama3.2-vision`, even when the model is
> downloaded successfully. `qwen3-vl` is supported by current Ollama releases.
> If you must use `llama3.2-vision`, install an Ollama version older than
> 0.30.0. Do not re-pull the model to fix `unknown model architecture: 'mllama'`;
> the manifest is valid, but the runtime no longer has a loader for it.

## Configuration

Settings live in `config/settings.json`. The backend reads this file at
startup, so restart uvicorn after changing it.

```json
{
  "agent_name": "Compa",
  "personality_file": "config/personality.md",
  "system_prompt": "Be concise but thorough. Ask clarifying questions. Never invent facts.",
  "ollama": {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen3-vl:8b-instruct",
    "timeout_seconds": 120,
    "temperature": 0.5,
    "max_retries": 1,
    "keep_alive_seconds": 1800,
    "warmup_on_startup": true,
    "model_quantization": "q4_K_M",
    "context_window": 16384,
    "system_prompt_mode": "auto"
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
    "max_unzip_bytes": 209715200,
    "redact_secrets": true,
    "powershell_timeout_seconds": 15,
    "powershell_max_output_chars": 12000
  },
  "vision": {
    "enabled": true,
    "embed_model": "clip-ViT-B-32",
    "index_path": "memory/vision_index.sqlite3",
    "scan_roots": ["~"],
    "batch_size": 8,
    "scan_interval_seconds": 600,
    "max_images": 150000,
    "max_file_mb": 50,
    "exclude_paths": [],
    "min_confidence_z": 3.9,
    "min_result_z": 3.5,
    "dedupe_threshold": 0.995,
    "screen_model": "qwen3-vl:8b-instruct",
    "screen_timeout_seconds": 180,
    "screen_max_width": 1600,
    "screen_max_tokens": 700
  }
}
```

The system message sent to the local model is composed from:

- `agent_name` — who the assistant says it is
- `config/personality.md` — tone, character, values, voice, and examples
- `system_prompt` — base behavior rules and constraints
- the knowledge base file — long-term facts about the user
- the tool rules: read-only vs. approval-required actions, protected locations,
  and the instruction to verify with search/read before modifying

Compa's personality is deliberately not stored in `settings.json`. Edit
`config/personality.md` instead: it is plain Markdown and is read on every chat.
It supports the placeholder `{{agent_name}}`, so renaming the agent only needs a
change to `agent_name` in `config/settings.json`. The inline `personality` field
still exists as a fallback for older configs and is used only when the file is
missing or empty.

`ollama.context_window` sets `num_ctx` for every request. The identity,
personality, knowledge base, and tool schemas need far more than Ollama's small
default context, so lowering this will truncate the prompt and the agent can
lose its identity or instructions. Raise it for long conversations; lower it if
the model no longer fits in VRAM. `ollama.system_prompt_mode` controls how the
system prompt is delivered: `"auto"` (default) folds it into the latest user
message for Hermes models, `"system"` always uses the system role, and `"user"`
always folds it.

`ollama.keep_alive_seconds` is sent as Ollama's `keep_alive` on every chat and
vision request. The default is `1800` (30 minutes): the model stays in VRAM
while the app is in use, avoiding an 8+ second cold load on every message. Chat
and screen analysis use the same `qwen3-vl` model, so keeping it loaded does not
make two different models compete for VRAM. Set `0` if you want strict
immediate unloading (every request then pays the load and prefill cost again).

`ollama.warmup_on_startup` (enabled in `config/settings.json`) preloads the model
and the stable system-prompt/tool-schema prefix in a background thread when the
backend starts. The first real message then only evaluates its own user text.
The volatile clock is deliberately placed in the newest user turn rather than
the system prompt: Ollama caches the longest identical token prefix, and a
minute-precision timestamp in the system prompt used to invalidate the whole
cache every minute. `ollama.model_quantization` documents the preferred variant
(`q4_K_M` by default). The backend logs a warning when the model tag advertises
`q5`/`q6`/`q8`/`fp16`, which can consume much more VRAM than necessary.

The reply language is chosen only from the latest user message, or from an
explicit request in the current conversation. Standing language-preference lines
in the knowledge base are filtered out before the prompt is built, so a stored
"always answer in German" fact cannot override an English message. Explicit
requests such as "answer in German" or "auf Deutsch bitte" still work.

### Why the backend does not use Ollama's OpenAI endpoint

Two Ollama behaviors break a long identity prompt:

1. The `hermes3` template renders the tool preamble when tools are supplied and
   skips `.System` entirely (`{{- if .Tools }} ... {{- else if .System }}`). The
   agent always supplies tools, so a system-role prompt would silently never
   reach the model. With no system prompt, Hermes falls back to its trained
   identity and introduces itself as "Hermes" instead of Compa. The backend
   therefore folds the prompt into the latest user turn for Hermes models.
2. Ollama's OpenAI-compatible `/v1/chat/completions` endpoint ignores `num_ctx`
   (ollama/ollama#16814). It truncates long prompts to the model default, which
   cuts off the beginning of the identity and knowledge base. The backend calls
   the native `/api/chat` endpoint through `agent/ollama_client.py` so
   `options.num_ctx` is honored.

`memory.max_conversations` bounds how many chats are kept in RAM.
`memory.learn_from_chats` enables automatic knowledge-base updates.
`agent.max_iterations` bounds tool rounds per segment.
`agent.approval_ttl_seconds` controls how long a pending approval remains valid.
`tools.*` caps file reads, searches, and zip extraction. `tools.redact_secrets`
(default `true`) removes high-confidence API keys, tokens, and private keys from
`read_file_content` results before they reach the model or the chat. Set it to
`false` only if you need the agent to read credentials verbatim.
`tools.powershell_timeout_seconds` (default 15) bounds every PowerShell command
and kills the process tree on timeout. `tools.powershell_max_output_chars`
(default 12000) truncates stdout/stderr before they reach the model.
`vision.*` controls the global CLIP image index: `scan_roots` defaults to the
current user profile (`~`); add more folders or a drive root such as `C:\` to
widen it. `scan_interval_seconds` controls how often the crawler rescans for
new, changed, and deleted images. `max_images` and `max_file_mb` bound the work.
`exclude_paths` accepts case-insensitive path fragments to skip folders such as
game asset packs or build caches. `min_confidence_z` and `min_result_z` control
when semantic search reports "no confident match" instead of returning weak
matches; `dedupe_threshold` suppresses near-identical copies.
`screen_model` selects the local Ollama model used by `analyze_screen`;
`screen_timeout_seconds` and `screen_max_width` bound the screenshot request.
`screen_max_tokens` caps the model's output so a runaway response cannot hold
the request open indefinitely. Prefer non-thinking vision models for screen
analysis: a thinking model can spend the entire timeout on hidden reasoning.

## Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload
```

Endpoints:

- `GET /health` — backend status
- `GET /identity` — configured name, personality, and model
- `GET /vision/status` — image-index phase, indexed count, crawl counters, and
  the last error
- `POST /chat/stream` — send a message; returns `text/event-stream`
- `POST /chat/approve` — `{"approval_id": "...", "approved": true|false}`;
  resumes the paused agent and returns a stream. Approval also covers the
  remaining steps of that request, so the agent does not ask again between steps.
- `GET /chat/pending/{conversation_id}` — restores a pending approval after a UI
  reload
- `POST /chat` — non-streaming compatibility endpoint for simple chats
- `GET /chat/history/{conversation_id}` — user-visible messages (tool internals
  are hidden)
- `DELETE /chat/history/{conversation_id}` — clear the chat and any pending
  approval

### Concurrency and resources

FastAPI endpoints are async. Blocking work (Ollama calls, file scans, CLIP
inference, SQLite queries) runs in Starlette's threadpool, so the event loop
stays responsive. Streaming generators are also iterated in the threadpool and
are explicitly closed when a client disconnects, releasing the per-conversation
lock and rolling back the partial turn.

The native Ollama client reuses one keep-alive HTTP connection per worker thread.
The vision index keeps a single CLIP embedder and caches one SQLite connection
per worker thread; connections are closed during application shutdown. Every
tool execution is logged with its name, success flag, and duration.

### Inference tuning

The backend loads `PROJECT_ROOT/.env` at startup (existing OS variables win) and
sets `OLLAMA_FLASH_ATTENTION=1` by default. Flash attention is a **server-side**
Ollama setting, so the Ollama server must be restarted after changing it. On
Windows:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
# Restart the Ollama tray app or run: $env:OLLAMA_FLASH_ATTENTION=1; ollama serve
```

The server log should show `OLLAMA_FLASH_ATTENTION:true`. The backend keeps the
model warm for `keep_alive_seconds` and warms the prompt prefix at startup. See
`.env.example` for the tracked example.

Measured on this machine (RX 9070 XT, `qwen3-vl:8b-instruct`, 16k context), the
same request after the change:

| Scenario | Time to first token |
| --- | --- |
| Old: cold load + clock-in-system prompt | ~11.7 s |
| Warm model, identical prompt prefix | ~0.6–0.9 s |
| Prompt cache broken by the clock | ~3.3 s extra |
| Model cold load | ~8.5 s extra |

The prompt is about 8,250 tokens (system prompt plus 16 tool schemas). With a
stable prefix Ollama reuses the cached tokens and only evaluates the new user
turn; prompt evaluation dropped from about 3.5 s to about 0.08 s. Background
fact-learning still uses the same warm model and does not evict the chat prefix.

### SSE event types

```text
status           thinking, generating, tool_started, approval
tool_result      tool name, ok flag, human-readable summary, raw result
action_required  approval_id, conversation_id, action descriptions, plan,
                 request, auto_approve_remaining
assistant_start  a streamed assistant text block begins
assistant_delta  incremental text; emitted as soon as tokens arrive
assistant_end    the full text of the block that just streamed
assistant_reset  discard the provisional block (raw tool-call JSON, retry)
metrics          live and final tokens/sec, token counts, timing, model
final            final natural-language reply (with a streamed flag)
error            readable error and HTTP-like status
```

The SSE stream ends after `final`, `error`, or `action_required`. The backend
uses Ollama's native `/api/chat` with `stream: true`, so text is forwarded token
by token rather than buffered into one message. `metrics` events are emitted
about twice per second while text streams (`live: true`) and once at the end
with Ollama's exact `eval_count`/`eval_duration`. The UI shows the live rate
under the growing answer and keeps a final footer with tokens/sec, generated
and prompt token counts, prompt evaluation, model load, and total time.

### Agent loop and HITL

1. The user message is appended and the system prompt plus the latest 10
   history messages and tool schemas are sent to the local model.
2. Read-only tools (`search_system`, `read_file_content`,
   `semantic_image_search`, `get_open_windows`, `read_window_content`,
   `analyze_screen`) run immediately and stream `tool_started` / `tool_result`
   events.
3. The first modifying tool of a request pauses the loop. The assistant tool-call
   message is stored, a pending approval is created, and an `action_required`
   event is streamed. The HTTP stream then closes; no thread waits for a click.
   The card lists the exact actions that will run now and the planned sequence
   (from `propose_plan`, the model's numbered plan, or an explicit
   "first … then …" request), and is marked `auto_approve_remaining`.
4. `POST /chat/approve` either executes the pending actions and appends
   `role: "tool"` results, or appends an “Action cancelled by user” result.
   After an approval, every remaining modifying step of the same request runs
   automatically, in the model's order, until the final answer. A denial does
   not grant that permission. An identical action repeated in one request is
   skipped, and a request is capped at 25 automatic actions.
5. Pending approvals are one per conversation, expire after the configured TTL,
   and are consumed exactly once. Unknown or reused IDs return 404.
6. If the user closes the UI, `GET /chat/pending/{id}` restores the approval card.
   Tool messages stay internal; the history endpoint returns only user/assistant
   text.

If Ollama is not running, streams emit a readable error event; the legacy
`POST /chat` returns HTTP 503. Timeouts map to 504 and other upstream failures to
502.

## Tools and security

The agent can use seventeen tools:

| Tool | Confirmation | Purpose |
|---|---|---|
| `propose_plan` | No | Record the ordered steps of a multi-step request so the approval card can show the full sequence before anything runs |
| `run_powershell_query` | No | Read-only PowerShell inspection: processes/RAM/CPU, services, registry, disks, network, event logs, files. Rejects state-changing commands |
| `run_powershell_action` | Yes | State-changing PowerShell after approval: kill/stop processes, change services or settings, registry writes, file changes, installs |
| `search_system` | No | Recursive or direct-child search by name/glob, extension, size, modified time, hidden flag, or all drives; newest/oldest sorting; known-folder aliases |
| `semantic_image_search` | No | Search the whole PC for images by visual content with a local CLIP index; returns ranked paths and scores |
| `get_open_windows` | No | List visible top-level Windows applications, exact titles, and the active window |
| `read_window_content` | No | Read text from an open app via Windows UI Automation |
| `analyze_screen` | No | Capture the primary screen in memory and ask the local `qwen3-vl:8b-instruct` model a question |
| `read_file_content` | No | Read text files; binary/image files return metadata only |
| `create_folder` | Yes | Create a folder anywhere outside protected locations |
| `create_file` | Yes | Create a UTF-8 text file, without overwriting unless explicitly requested |
| `copy_file` | Yes | Copy a file; destination must not exist |
| `move_file_or_folder` | Yes | Move a file/folder, including across drives; no overwrite |
| `delete_file_or_folder` | Yes | Move a file/folder to the Windows Recycle Bin |
| `unzip_file` | Yes | Extract a `.zip` safely into any non-protected folder |
| `start_program` | Yes | Launch any installed program by name or executable path; no arguments, no shell |
| `open_file` | Yes | Open an existing file in an explicitly chosen editor/program |

Security boundaries:

- No sandbox. The tools can reach the whole file system, as requested.
- The first modifying action of a request requires explicit approval; read-only
  actions do not. After approval, the remaining modifying steps of that same
  request run automatically, in order, without another card.
- `run_powershell_query` is guarded as read-only: it rejects state-changing
  cmdlets and aliases, output redirection, file-writing parameters, mutating
  .NET calls, and dynamic code execution. If an inspection command is rejected,
  the agent must use `run_powershell_action`, which pauses for approval.
- `run_powershell_action` always requires the first approval of a request and a
  required description of exactly what will change. The approval card shows that
  description and the command before the user clicks **Approve all**; following
  PowerShell steps in the same request run automatically.
- If a PowerShell action fails, the backend forces one recovery round: the agent
  reads `stderr`, `exit_code`, and the failure hint, verifies the current state
  with `run_powershell_query`, and tries another safe approach. The recovery
  action runs under the same request approval.
- Every PowerShell command has a strict timeout and kills its process tree on
  timeout; large stdout/stderr is truncated before reaching the model.
- Critical locations (`Windows`, `Program Files`, `Program Files (x86)`,
  `ProgramData`, `Boot`, `Recovery`, drive roots, `C:\Users`, and the user
  profile root) are hard-denied for create/copy/move/delete/unzip even after
  approval.
- Deletes go to the Recycle Bin, not permanent deletion.
- Copy/move never overwrite an existing destination.
- Zip extraction rejects path escapes, caps entry count, and caps uncompressed
  size.
- Searches cap results, scanned files, and elapsed time.
- `start_program` can launch any installed program or executable. Its first use
  in a request requires approval. It passes no arguments and never uses a shell.
- `open_file` requires an explicit editor/program; the agent asks the user when
  none is named. Its first use in a request requires approval.
- Window awareness is read-only: it lists windows and reads UI Automation text,
  but does not click, type, or control applications.
- `analyze_screen` captures the primary monitor in memory, sends it only to the
  local Ollama endpoint, and never writes a screenshot to disk.
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

## Semantic image index

`semantic_image_search` finds pictures by what they show, not by filename. The
index is local and starts automatically with the FastAPI app when
`vision.enabled` is true:

- A daemon thread walks `vision.scan_roots` recursively. The default is the
  current user profile (`~`); add `C:\` or another drive to include more.
- It skips hidden and system folders and common noise such as `AppData`,
  `Windows`, `Program Files`, `node_modules`, `.git`, `.venv`, `target`, and
  caches. Offline OneDrive placeholders and files above `max_file_mb` are not
  hydrated or embedded.
- Images are embedded with `clip-ViT-B-32` in small batches and stored as
  normalized float32 vectors in `memory/vision_index.sqlite3`. Search is a
  chunked cosine scan in NumPy, so there is no Chroma or other vector service.
- Rescans compare path, size, and modification time, so unchanged files are not
  re-embedded. New files are added, changed files are updated, and deleted files
  are pruned. The thread sleeps for `scan_interval_seconds` between passes.
- Search queries are compared using several natural-language prompt variants,
  near-identical images are suppressed, and results must stand out from the
  index's score distribution. If nothing is confidently related, the tool
  returns an empty result with a `low` confidence value instead of five
  unrelated images. The tool result includes `confidence`, `best_score`,
  `score_gap`, and `z_score` for diagnosis.
- Filtering paths is available through `exclude_paths`, which is useful for
  large game asset packs, texture dumps, or development caches.
- The CLIP model is downloaded once from Hugging Face on first use (about
  600 MB) and then runs fully offline. The first full crawl can take a while on
  CPU; later passes are incremental. `clip-ViT-B-32` is fast; switching
  `embed_model` to a larger CLIP model improves difficult queries but requires
  deleting the index so it can be rebuilt.
- `GET /vision/status` reports the phase (`indexing`, `idle`, `error`), indexed
  count, crawl counters, roots, model, and last error. The tool result also says
  whether the index is still building.

Ask naturally, for example: *"Search my whole computer for a picture of a dog"*
or *"Find my beach vacation photos."* The tool returns up to 5 confident matches
by default (up to 10) with absolute paths, similarity scores, and modified times.

## Screen awareness

Three read-only tools cover the desktop:

- `get_open_windows` uses `pygetwindow` to list visible top-level windows,
  their exact titles, geometry, and which one is active. Minimized and hidden
  windows are omitted.
- `read_window_content` uses `pywinauto` UI Automation to walk the named
  window's control tree and collect readable text and document values. It tries
  the UIA backend first, then Win32. Some browsers and custom-rendered apps
  expose little text; the result says so and suggests `analyze_screen`.
- `analyze_screen` uses `mss` to capture the primary monitor, downscales it to
  `screen_max_width`, encodes it as an in-memory PNG, and sends a base64 image
  URL plus the user's question to the local Ollama
  `/v1/chat/completions` endpoint with the configured `screen_model`
  (`qwen3-vl:8b-instruct` by default). The screenshot is never written to disk, so
  there are no temporary files to clean up. Only the model's text answer is
  returned; the image is not exposed in the tool result.

Pull the vision model once:

```powershell
ollama pull qwen3-vl:8b-instruct
```

If Ollama reports `unknown model architecture`, the model file can be present
while the installed Ollama runtime cannot load it. This is not fixed by pulling
again. Check `ollama show <model>` and use a model supported by your Ollama
version; for `mllama`/`llama3.2-vision`, you would need Ollama older than 0.30.0.
If a thinking model times out, switch to its `-instruct` variant (for example
`qwen3-vl:8b-instruct`), lower `screen_max_tokens`, or raise
`screen_timeout_seconds`.

The agent is instructed to use window tools for "which apps are open" and
"read this document", and `analyze_screen` for "what is on my screen" or visual
questions. It does not click, type, or otherwise control applications. Screen
captures are only requested for clear screen-related requests and go only to the
local Ollama instance.

## Memory

Chat history is held strictly in memory per conversation ID. Every message is
kept, including internal tool-call/tool-result messages used by the model, so the
agent can continue a paused task. Restarting uvicorn or deleting the chat clears
it. It is never written to disk.

Only the system prompt plus the latest 10 history messages are sent to Ollama;
older messages stay in the backend store and in the UI. The sliding window moves
to a user-message boundary and preserves complete tool-call/tool-result groups,
so a truncated payload never contains an orphaned tool result.

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
HITL pause/approve/deny, one-approval multi-step execution, duplicate-action
protection, approval TTL/idempotency, SSE frames, rollback, and the
incremental CLIP image index (crawl, skip rules, updates, pruning, search, search
confidence), plus window enumeration and screen-analysis routing.
Ollama is not required for the tests; the vision tests use a fake embedder and
fake capture/model methods.

## Frontend

Ollama must be running. The desktop app starts the FastAPI backend itself as a
hidden child process, so no terminal is needed. In development:

```powershell
cd frontend
npm install
npm run tauri dev
```

For a terminal-free standalone window, build with the Tauri CLI. It runs the
frontend build and embeds `dist` into the binary, so Vite does not need to run:

```powershell
cd frontend
npm run tauri build -- --no-bundle
.\src-tauri\target\release\personal-agent.exe
```

Use `npm run tauri build` without `--no-bundle` to also create an installer.
Do not use a plain `cargo build --release` for the distributable app: that can
leave the development URL in the binary and show `ERR_CONNECTION_REFUSED` when
Vite is not running.

The app reuses a healthy backend if one is already listening on port 8000.
Otherwise it starts `backend\.venv\Scripts\python.exe -m uvicorn` with
`CREATE_NO_WINDOW`, redirects its output to
`%LOCALAPPDATA%\com.personalagent.desktop\logs`, and stops the whole backend
process tree when the app is quit. Launcher diagnostics are in `launcher.log` in
the same folder. Set `PERSONAL_AGENT_BACKEND` to the path of a packaged backend
executable to use it instead.

The app starts hidden in the system tray. Click the tray icon to show or hide the
chat window; right-click it for **Show / Hide** and **Quit**. Closing the window
hides it instead of quitting.

The window is frameless and transparent: the header is a Tauri drag region, and
the **Hide to tray** button in the top-right calls `getCurrentWindow().hide()`.
The rounded card is drawn in CSS, so the area outside its corners stays
transparent. Use the tray icon to bring the window back.

The chat UI consumes `POST /chat/stream`, renders intermediate statuses, shows an
**Approval required** card with **Approve all / Deny** for the first modifying
step of a request, and restores a pending approval after a reload. The card lists
the exact starting actions and the planned sequence. Approving runs the remaining
steps of that request without another card.

While a turn runs, a compact task card updates in place with every tool step:
spinner for the current step, checkmarks for completed tasks, and expandable
error details for failures. It collapses after completion to a summary such as
**All tasks completed**, and can be expanded again to review the full list,
tool names, and durations. Messages render GitHub-flavored Markdown with
syntax-highlighted code blocks, tables, task lists, and copy buttons. The dark
theme uses Inter and a subtle violet-to-emerald gradient frame. Set
`VITE_API_URL` to override the default backend URL `http://127.0.0.1:8000`.

File references are intercepted in Markdown links, `file:///` URLs, raw
`C:\...` paths, and bare paths in prose:

- Image references render as lazy-loaded thumbnails, grouped into a responsive
  gallery when several appear together. Hovering reveals a preview (with a
  full-size lightbox), open, reveal, and copy actions. The Tauri asset protocol
  (`convertFileSrc`) serves the files without exposing the whole webview to
  `file://` restrictions.
- Other files render as rounded cards with a type icon, truncated filename, and
  file type. Hovering reveals **Open file**, **Show in folder**, and **Copy
  path**. Opening uses `@tauri-apps/plugin-shell`, revealing uses
  `@tauri-apps/plugin-opener`, and copying uses
  `@tauri-apps/plugin-clipboard-manager`.
- Canonical files returned by tools are also collected from the structured
  `tool_result` events and rendered as an attachment panel when the model's
  reply omits them. This keeps paths correct even if the model prints a wrong
  link or invents an external image URL; external image embeds are stripped on
  local image-search turns.
- Paths inside inline code and fenced code blocks are left untouched.
- The system prompt asks the model to emit
  `[name.ext](file:///C:/absolute/path/name.ext)` links with `%20` for spaces.

The Tauri capabilities grant only `shell:allow-open`,
`opener:allow-reveal-item-in-dir`, `clipboard-manager:allow-write-text`, and the
window hide/drag permissions. The asset protocol is enabled in
`src-tauri/tauri.conf.json` with a local-file read scope.

## Known limitation

`qwen3-vl:8b-instruct` is the default because it follows the full identity,
knowledge-base, and tool instructions reliably in local testing. Hermes 3
remains supported through `ollama.model`, but its 8B variant is less consistent
at instruction following and can make unnecessary tool calls. With very long
conversations, raise `ollama.context_window`; if the prompt exceeds the window,
Ollama drops the oldest turns first. For complex chores, splitting the request
into smaller steps (search, create, move a file) gives the most reliable
results. A larger tool-calling model can use the same loop without backend
changes.
