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
  with `qwen3-vl:8b-instruct`; with multiple monitors the agent asks which
  display to analyze and shows a live preview of each one
- App-aware GUI control: per-application shortcuts and workflows (Outlook,
  Explorer, browsers, Notepad, VS Code, Office), post-action window/focus
  feedback, duplicate-click loop breaking, and prevention of duplicate app
  launches
- Comprehensive read-only system diagnosis: hardware, live CPU/memory/GPU,
  top processes, disk/SSD health, services, startup apps, security, event-log
  errors, and CPU/memory benchmarks with matplotlib charts
- Human-in-the-Loop approval once per request: the card lists the first step and
  approving lets the agent finish the remaining steps in order automatically
- Multi-step and sequence requests ("first do this, then that") run to completion
  without a second approval card
- Compact in-place tool status: one animated row per task, with expandable
  stderr/exit-code details when an action fails
- Stop button while the agent is working; interrupted turns keep the partial
  answer and task overview and become “Interrupted by user” in history
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
  tools/app_knowledge.py Per-app shortcuts/workflows and secondary-window hints
  tools/vision_tools.py Global CLIP image crawler, SQLite vector store, screen vision
  tools/system_diagnosis.py Read-only system health/performance diagnosis and charts
  tools/schemas.py   OpenAI-compatible tool definitions
  tools/executor.py  Validated tool dispatch and confirmation flags
  tests/             pytest suite and opt-in live real-app harness
  .venv/             Local virtual environment (gitignored)
config/settings.json   Backend, Ollama, identity, memory, vision, and tool settings
config/personality.md  Compa's editable personality and character sheet
memory/                Knowledge base, image index, and local data (gitignored)
scripts/build_windows.ps1  Stages the Python runtime and builds the click-to-run app
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
screenshot leaves the machine. The UIA fallback uses RapidOCR with bundled
ONNX models, so it also runs offline and does not need Tesseract.

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
    "context_window": 24576,
    "system_prompt_mode": "auto"
  },
  "agent": {
    "max_iterations": 0,
    "max_auto_approved_actions": 0,
    "approval_ttl_seconds": 600,
    "dynamic_tools": true
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
    "min_confidence_z": 3.0,
    "min_result_z": 2.4,
    "dedupe_threshold": 0.995,
    "screen_model": "qwen3-vl:8b-instruct",
    "ground_model": "",
    "ground_context_window": 8192,
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
the model no longer fits in VRAM. Screen analysis uses the same `num_ctx`, which
keeps the chat runner loaded and its prompt cache reusable.
`ollama.system_prompt_mode` controls how the
system prompt is delivered: `"auto"` (default) folds it into the latest user
message for Hermes models, `"system"` always uses the system role, and `"user"`
always folds it.

`ollama.keep_alive_seconds` is sent as Ollama's `keep_alive` on every chat and
vision request. The default is `1800` (30 minutes): the model stays in VRAM
while the app is in use, avoiding an 8+ second cold load on every message. Chat
and screen analysis use the same `qwen3-vl` model, so keeping it loaded does not
make two different models compete for VRAM. Set `0` if you want strict
immediate unloading (every request then pays the load and prefill cost again).
For interactive use, leave it above zero: screen analysis also relies on the
loaded runner and on Ollama's prompt cache surviving between turns.

`ollama.warmup_on_startup` (enabled in `config/settings.json`) preloads the model
and the stable system-prompt/tool-schema prefix in a background thread when the
backend starts. The first real message then only evaluates its own user text.
The volatile clock is deliberately placed in the newest user turn rather than
the system prompt: Ollama caches the longest identical token prefix, and a
minute-precision timestamp in the system prompt used to invalidate the whole
cache every minute. `ollama.model_quantization` documents the preferred variant
(`q4_K_M` by default). The backend logs a warning when the model tag advertises
`q5`/`q6`/`q8`/`fp16`, which can consume much more VRAM than necessary.

`agent.dynamic_tools` (default `true`) sends only the tool schemas the request
plausibly needs. Base and GUI tools are always included, so a chat turn and a GUI
turn share one cached prompt prefix; file, image-search, and diagnosis schemas
are added only when the message mentions them. This removes roughly nine schemas
(~2.3k tokens), which measured as **2.96 s → 1.52 s** for the same GUI decision
step (about 22 → 35 generated tokens/s). Switching tool lists forces Ollama to
re-evaluate the whole prompt, so the always-on base covers the common
chat↔GUI transitions. Set it to `false` to always send the full schema list.

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
`agent.max_iterations` bounds tool rounds per segment. The default `0` means no
hard limit: the agent keeps working until the task is done, however many steps
that takes. Set a positive value only if you want an explicit safety cap; when a
cap is reached, the agent summarizes the evidence it already has instead of
ending with a tool-limit error.
Independent of that cap, three consecutive identical read-only calls with the
same arguments are treated as a loop: the third call is not executed and the
agent is asked to answer from the results it already has. File bodies sent to
the model are capped at 12,000 characters (the chat still shows the full file),
so a single large read cannot overflow the context window and hide its own
result.
`agent.max_auto_approved_actions` works the same way for actions that run after
one approval: `0` means no cap.
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
when semantic search reports a confident match; when the score gap still shows a
clear best candidate, the tool returns a few tentative matches labelled
`low` confidence instead of an empty result. `dedupe_threshold` suppresses
near-identical copies.
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
- `POST /chat/display` — `{"approval_id": "...", "display": 1}`; resumes a paused
  screen analysis with the chosen 1-based display. Send `display: null` to
  cancel the analysis.
- `POST /chat/interrupt` — `{"conversation_id": "..."}`; asks the active turn to
  stop after the current operation and returns 204.
- `GET /chat/pending/{conversation_id}` — restores a pending approval or display
  choice after a UI reload
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

Measured on this machine (RX 9070 XT, `qwen3-vl:8b-instruct`, 16k context),
after the warm-model and prompt-cache work plus the latest performance pass:

| Scenario | Result |
| --- | --- |
| Warm simple reply, first token | 0.16–0.20 s |
| Warm prompt evaluation | ~92 ms |
| Immediate follow-up, first token | ~0.45 s (cache settling) |
| Read-only tool turn, total | ~2.9 s, tool itself ~0.18 s |
| Long answer generation | ~15–21 tokens/s |
| UI long tasks while streaming | 0 |
| Cold model load | ~8.5 s (avoided while warm) |
| One-time prompt refresh after a learned fact | ~2.0 s |

The prompt is about 8,250 tokens (system prompt plus 17 tool schemas). With a
stable prefix Ollama reuses the cached tokens and only evaluates the new user
turn; prompt evaluation is about 0.09 s. The knowledge block sits at the end of
the stable system prompt, and the volatile clock stays in the newest user turn.
A newly learned fact causes one prompt-cache refresh; subsequent turns are fast
again.

Background fact-learning is deferred for three seconds of idle time and
coalesced across rapid messages. Command-only messages without personal cues
skip the extraction call entirely, so they don't compete with the next turn;
first-person and personal messages are still learned.

### SSE event types

```text
status           thinking, generating, tool_started, approval
tool_result      tool name, ok flag, human-readable summary, raw result
action_required  approval_id, conversation_id, action descriptions, plan,
                 request, auto_approve_remaining
display_required approval_id, conversation_id, displays (index, resolution,
                 primary flag, position, live preview), request, question
assistant_start  a streamed assistant text block begins
assistant_delta  incremental text; emitted as soon as tokens arrive
assistant_end    the full text of the block that just streamed
assistant_reset  discard the provisional block (raw tool-call JSON, retry)
metrics          live and final tokens/sec, token counts, timing, model
cancelled        the turn was interrupted by the user
final            final natural-language reply (with a streamed flag)
error            readable error and HTTP-like status
```

The SSE stream ends after `final`, `error`, `action_required`, or
`display_required`. The backend
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
   `semantic_image_search`, `system_diagnosis`, `get_open_windows`,
   `read_window_content`, `analyze_screen`, `run_powershell_query`) run
   immediately and stream `tool_started` / `tool_result` events.
3. The first modifying tool of a request pauses the loop. The assistant tool-call
   message is stored, a pending approval is created, and an `action_required`
   event is streamed. The HTTP stream then closes; no thread waits for a click.
   The card lists the exact actions that will run now and the planned sequence
   (from `propose_plan`, the model's numbered plan, or an explicit sequence such
   as "first … then …", a comma-separated action list, "X and Y", or "X und Y"),
   and is marked `auto_approve_remaining`.
   An `analyze_screen` call pauses the same way when more than one display is
   connected: a `display_required` event streams the display list and the UI asks
   the user to choose before the screenshot is taken.
4. `POST /chat/approve` either executes the pending actions and appends
   `role: "tool"` results, or appends an “Action cancelled by user” result.
   After an approval, every remaining modifying step of the same request runs
   automatically, in the model's order, until the final answer. A denial does
   not grant that permission. An identical action repeated in one request is
   skipped, and a request is capped at 25 automatic actions.
   `POST /chat/display` does the same for a display choice: it executes
   `analyze_screen` with the chosen index and continues the loop.
5. Pending approvals and display choices are one per conversation, expire after
   the configured TTL, and are consumed exactly once. Unknown or reused IDs
   return 404; an unavailable display number returns 400 and keeps the choice
   pending.
6. If the user closes the UI, `GET /chat/pending/{id}` restores the approval or
   display card. Tool messages stay internal; the history endpoint returns only
   user/assistant text.

If Ollama is not running, streams emit a readable error event; the legacy
`POST /chat` returns HTTP 503. Timeouts map to 504 and other upstream failures to
502.

## Tools and security

The agent can use eighteen tools:

| Tool | Confirmation | Purpose |
|---|---|---|
| `propose_plan` | No | Record the ordered steps of a multi-step request so the approval card can show the full sequence before anything runs |
| `run_powershell_query` | No | Read-only PowerShell inspection: processes/RAM/CPU, services, registry, disks, network, event logs, files. Rejects state-changing commands |
| `run_powershell_action` | Yes | State-changing PowerShell after approval: kill/stop processes, change services or settings, registry writes, file changes, installs |
| `search_system` | No | Recursive or direct-child search by name/glob, extension, size, modified time, hidden flag, or all drives; newest/oldest sorting; known-folder aliases |
| `semantic_image_search` | No | Search the whole PC for images by visual content with a local CLIP index; returns ranked paths and scores |
| `system_diagnosis` | No | Full read-only health/performance check: hardware, live CPU/memory/GPU, top processes, disk/SSD health, services, startup, security, event errors, benchmarks, and matplotlib charts |
| `get_open_windows` | No | List visible top-level Windows applications, exact titles, and the active window |
| `read_window_content` | No | Read text from an open app via Windows UI Automation |
| `analyze_screen` | No | Capture one display in memory and ask the local `qwen3-vl:8b-instruct` model a question; with several displays the user picks which one first |
| `get_desktop_context` | No | Foreground window, process/parent chain, visible/minimized windows, dialogs, and the focused UI control |
| `analyze_screen_ui` | No | UIA-verified Set-of-Mark map: element IDs/names/centers, verification source, app guide and shortcuts, numbered screenshot, local name match or vision suggestion, focused-control and window state |
| `move_cursor_to` | Yes | Highlight, then glide the physical cursor to a detected element |
| `click_element` | Yes | Highlight, glide, and click a detected element; physical input aborts instantly |
| `keyboard_type_text` | Yes | Type into the focused field with synthetic keys or layout-safe clipboard paste |
| `keyboard_press_keys` | Yes | Press Enter, Tab, Escape, arrows, or a key combination such as Ctrl+S |
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
- `analyze_screen` captures the chosen monitor in memory, sends it only to the
  local Ollama endpoint, and never writes a screenshot to disk. When several
  displays are connected, the user must choose one before any capture happens.
- `system_diagnosis` is read-only. Its only writes are its own PNG charts and
  `report.json` under `memory/diagnostics`; it does not change system settings.
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
- Search queries are compared using CLIP text embeddings, near-identical
  images are suppressed, and results must stand out from the index's score
  distribution. A confident match needs both a high z-score and a clear gap
  between the best and second-best image; when only the gap is convincing, the
  tool returns up to three tentative candidates with a `low` confidence value.
  The model is told to present those as approximate possibilities and to
  sanity-check the file names. If nothing stands out at all, the tool returns
  an empty result. The tool result includes `confidence`, `best_score`,
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
or *"Find my beach vacation photos."* The tool returns up to 5 matches by
default (up to 10) with absolute paths, similarity scores, confidence, and
modified times. Low-confidence matches are labelled as tentative rather than
presented as certain results.

## Screen awareness

Three read-only tools cover the desktop:

- `get_open_windows` uses `pygetwindow` to list visible top-level windows,
  their exact titles, geometry, and which one is active. Minimized and hidden
  windows are omitted.
- `read_window_content` uses `pywinauto` UI Automation to walk the named
  window's control tree and collect readable text and document values. It tries
  the UIA backend first, then Win32. Some browsers and custom-rendered apps
  expose little text; the result says so and suggests `analyze_screen`.
- `analyze_screen` uses `mss` to capture a display, downscales it to
  `screen_max_width`, encodes it as an in-memory PNG, and sends a base64 image
  URL plus the user's question to the local Ollama
  `/v1/chat/completions` endpoint with the configured `screen_model`
  (`qwen3-vl:8b-instruct` by default). The screenshot is never written to disk, so
  there are no temporary files to clean up. Only the model's text answer is
  returned; the image is not exposed in the tool result.
- When more than one display is connected, `analyze_screen` does not capture
  anything immediately. The backend pauses the turn and streams a
  `display_required` event with every display's index, resolution, position,
  primary flag, and a small live preview. The UI shows a **Which display should
  I analyze?** card; choosing one calls `POST /chat/display` and the analysis
  continues with exactly that monitor. The selection can also be cancelled.
  This always happens when several displays are present, so a guessed monitor is
  never captured.
- The vision request deliberately uses the same `ollama.context_window`, system
  prompt, and focused tool profile as chat. Ollama therefore reuses the loaded
  runner and the cached chat prefix instead of reloading the model at a different
  context size and re-evaluating the ~8k-token prompt. Screen analysis now takes
  a couple of seconds and does not slow down the next answer. If
  `vision.screen_model` differs from `ollama.model`, the prompt-prefix reuse is
  skipped because the models cannot share a runner.
- `vision.ground_model` (empty by default) can point Set-of-Mark element
  grounding at a separate model; `vision.ground_context_window` then bounds its
  small context. A `qwen3-vl:4b-instruct` grounding model was measured on the
  reference machine and was **slower** than the warm 8B model (5.7 s vs 0.8 s)
  because it writes far longer JSON answers, so the default stays with
  `qwen3-vl:8b-instruct`.

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
questions. Screen captures are only requested for clear screen-related requests
and go only to the local Ollama instance. Clicking and typing are separate,
approval-gated GUI tools described below.

## GUI automation

The GUI layer is **UIA-grounded**, with a strict OCR fallback for apps that
block accessibility:

> **Phase 20:** pixel-only contour guessing was removed from the primary path.
> Every box in the normal path is a verified Windows UIAutomation control; the
> OCR fallback is used only when the accessibility tree exposes nothing usable.

- `analyze_screen_ui` walks the target window's Windows UI Automation tree with
  `pywinauto`. Every marked element is a real accessibility node: a definitive
  control type (Button, SplitButton, MenuItem, Hyperlink, Edit, ComboBox,
  CheckBox, RadioButton, TabItem, ListItem, TreeItem, DataItem, Slider, Spinner,
  Document, RichEdit), or a custom node that exposes the UIA **Invoke** pattern
  or is **keyboard focusable** and has a plausible control-sized rectangle.
  Plain `Text`/`Image` nodes are accepted only with Invoke; structural
  containers (`ToolBar`, `Tree`, `Tab`, `MenuBar`, editor panes, separators) are
  rejected. Nested duplicates are removed, keeping the more semantic control.
  Nothing is guessed from pixels in UIA mode.
- Each result reports `detection`: `uia`, `ocr`, `uia+ocr`, or `none`, plus
  `uia_verified` and `uia_scanned`. The full list holds up to
  `automation.max_elements` (140) controls; the screenshot labels at most
  `automation.max_annotated_elements` (60), prioritizing input fields, lists,
  and tabs. A control without a numbered box is still clickable by name or ID.
- If UIA exposes no usable controls (`automation.uia_min_elements`, default 1),
  the fallback runs **RapidOCR** on the raw window image and keeps only short
  labels: 1-4 words, confidence >= `ocr_min_confidence`, height <=
  `ocr_max_height`, and a natural aspect ratio (0.5-7.5). Paragraphs, long
  lines, banners, and oversized regions are discarded. OCR is skipped when the
  target window is not foreground, because the screen capture would show a
  different app. Disable the fallback with `automation.visual_fallback: false`.
- Each snapshot writes an annotated PNG under `memory/screenshots/` with a
  semi-transparent colored box and a numbered chip over every element. The tool
  returns the screenshot path (shown in the chat), the full element list, a
  compact `coordinate_map` of `ID -> {x, y, name, type}`, a `snapshot_id`, and a
  `suggested_id`. The suggestion comes from an exact name match when one control
  is unambiguous (no model call), and from `qwen3-vl` otherwise. IDs are only
  valid for that snapshot. The parser stores a short-lived snapshot in memory;
  stale IDs are rejected.
- A full Set-of-Mark pass captures only the target window (not the whole 4K
  display), annotates it, and saves the PNG with `compress_level=1`. When the
  window content is unchanged within `automation.snapshot_reuse_seconds`, the
  previous snapshot is reused after a cheap pixel-fingerprint check instead of
  running UIA, annotation, and grounding again. A fresh pass measured ~0.56 s
  and an unchanged reuse ~0.05 s.
- `move_cursor_to` and `click_element` accept `snapshot_id`, `element_id`, and
  `element_name`. The name is checked against the snapshot, so a stale or
  hallucinated ID cannot silently click the wrong control. The pointer shows a
  click-through red highlight for `highlight_seconds` while it glides over a
  distance-adaptive duration (0.12 s for a short hop, capped by
  `move_duration_seconds`), checking the killswitch between steps. The highlight
  never blocks the glide and is withdrawn immediately before the click.
- `keyboard_type_text` types into the already-focused field. Simple layout-safe
  text uses synthetic key events; text containing `@`, `:`, `!`, umlauts, or
  other layout-sensitive characters, or longer than 48 characters, uses a
  clipboard paste and restores the previous clipboard text. Long form values
  therefore take one paste instead of one keystroke per character.
  `keyboard_press_keys` handles Enter, Tab, Escape, arrows, and combinations
  such as Ctrl+S.
- Every input tool requires explicit HITL approval. The approval card says:
  *"Requesting cursor control to click #7 'New Email'. Please take your hands
  off the mouse and keyboard."* The agent is told to prefer CLI commands and
  shortcuts when they can accomplish the task, and to stop before Send, Delete,
  or other irreversible actions unless the user explicitly asked for them.

### Physical-input killswitch

`pynput` installs global low-level mouse and keyboard listeners. On Windows the
`win32_event_filter` inspects the hook's injected flag, so synthetic events from
`pyautogui` are ignored while a real mouse move, click, wheel, or key press
aborts the action. The guard ignores the first `arm_delay_seconds` (default 0.4)
to absorb the click on the approval button, ignores mouse movement below
`move_threshold_pixels` (default 4), and is checked between every interpolation
step. An abort raises `AutomationAbortedException`, stops the current action, and
sets a turn-level halt; every later GUI tool in the same turn is refused. Only a
new user message clears the halt. The agent is instructed to report the abort and
ask how to continue, never to retry silently.

Configuration lives under `automation` in `config/settings.json`:
`enabled`, `move_duration_seconds` (0.4 cap), `highlight_seconds` (0.3),
`arm_delay_seconds`, `move_threshold_pixels`, `max_elements` (140),
`max_annotated_elements` (60), `type_interval_seconds`,
`action_settle_seconds` (0.9), `verify_foreground`, `screenshot_max_width`,
`screenshot_keep`, `local_grounding`, `snapshot_reuse_seconds`,
`snapshot_reuse_threshold`, `visual_fallback`, `uia_min_elements`,
`ocr_min_confidence`, `ocr_max_words`, `ocr_max_height`, `pre_action_validation`,
and `validation_tolerance_px`.

### Context-aware pre-action validation

`get_desktop_context` synthesizes the foreground window, its owning process and
parent chain, every visible/minimized top-level window, dialog-like pop-ups, and
the focused UI Automation control. `analyze_screen_ui` includes the same context
with every snapshot.

Immediately before a click or move, the backend re-checks the live state against
the snapshot:

- window handle still exists, visible, and not minimized
- window title and owning process ID match
- window rectangle is unchanged within `validation_tolerance_px`
- the target pixel actually belongs to the target window (so a topmost mock can
  be clicked even if it is not foreground, but an occluded element is blocked)
- the owner window is enabled (a modal dialog disables its owner)
- the element still exists and has not moved beyond tolerance

`keyboard_type_text` additionally reads the live focused UIA element and checks
that it is still an Edit/Document control in the approved process/window. If the
focus moved, the tool refuses to type and asks the agent to click the field
again.

Failures are explicit: `context_changed`, `element_missing`, or
`focus_changed`, plus `fresh_context` and the last annotated screenshot. The
agent is told to re-run `analyze_screen_ui` and never retry the stale ID.

### GUI step latency

A GUI step used to cost several serialized passes: a full-display capture,
UIA discovery (OCR only when needed), an annotated PNG, a `qwen3-vl` grounding
call, then a 0.5 s highlight plus a 0.9 s glide. The current pipeline removes
most of that:

- the capture is cropped to the target window and the PNG uses
  `compress_level=1` instead of `optimize=True`
- UIA control names drive grounding, so the vision model runs only for
  unlabeled/visual-only elements
- an unchanged screen reuses the previous snapshot after a pixel-fingerprint
  check instead of re-running UIA, annotation, and grounding
- the red highlight is click-through and overlaps a distance-adaptive glide
- long text is pasted instead of typed character by character
- the model receives ~5.5 KB of element data instead of ~18 KB, and the focused
  tool profile is smaller, so each decision round is roughly twice as fast

`analyze_screen_ui` measured **4.7 s → 0.56 s** for a fresh pass and **0.05 s**
for a reuse on the reference machine. A live smoke test (one chat turn, then a
read-only GUI analysis of VS Code) completed in 2.5 s and 4.3 s.

### Application awareness and state feedback

The agent no longer treats the desktop as anonymous pixels. `analyze_screen_ui`
returns a compact **app guide** (`app`) with the app's shortcuts and workflow,
`app_windows` (other windows of the same process), the preferred window, the
recent action log, `window_foreground`, and the live focused control. Known
profiles cover Outlook, File Explorer, Chrome/Edge/Firefox, Notepad, VS Code,
Word/Excel/PowerPoint, and a generic Windows fallback. The guides encode
human knowledge such as *Outlook's Ctrl+N opens a separate compose window*,
*Explorer uses Ctrl+L instead of the breadcrumb address bar*, and *a browser
window title is not the page heading*.

Every `click_element`, `keyboard_type_text`, and `keyboard_press_keys` result
contains an `outcome`:

```text
window_opened   new top-level windows (title, process, handle, active)
window_closed   windows that disappeared
title_changed   same window, new title (navigation, save, modified marker)
focus           foreground window
focused_control name/type of the live focused control
ui_changed      whether anything changed
```

That feedback drives the next step: a newly opened compose window becomes the
preferred window, and the agent is told not to click the control that opened it
again. Repeating a click that produced no state change returns
`loop_detected` with the recent actions, so a loop is broken instead of
repeated. Text fields are exempt from that guard (clicking a field again is how
a person refocuses it), and transient windows such as `Pop-upHost`, tooltips,
and menus never become the app target.

Keyboard input is now gated on a verified target: the analyzed window must be
foreground (the backend force-activates it, including the Alt/AttachThreadInput
unlock), and typing requires a text-capable control in that process and window.
A click on Explorer's address bar that fails to focus the edit is recovered
with Ctrl+L inside the same approved action. `start_program` returns
`already_running` with the existing windows instead of launching a duplicate
instance.

### Chat recipient safety

Messaging apps need one extra invariant: the active conversation, not just the
active window, must be the requested person. A real Discord task exposed the
failure mode. The agent clicked **New Message**, then treated the *existing*
`Nachricht an @Agent Notification` composer as the recipient field and started
typing a message intended for **Paul** into the bot DM.

`analyze_screen_ui` therefore reports `conversation` (read from the composer
label `Nachricht an …` / `Message …` or the window title) and a
`conversation_warning`. Notification and bot chats are flagged, and the
`next_step` tells the agent to open the person's existing DM entry or use the
New Message/Search flow instead of reusing the current composer.

The backend no longer relies on the prompt alone. `keyboard_type_text` accepts
an optional `recipient` and resolves the live focused composer plus its actual
window title before typing:

* a notification/bot chat is refused unconditionally (`notification_chat`);
* chat apps require `recipient='<exact name>'` (`needs_recipient`);
* a composer for a different person is refused (`wrong_recipient`) with the
  current label, so the agent must navigate first and retry.

Discord, WhatsApp, Telegram, Teams, and Slack profiles now share the chat
workflow and label patterns. In a live check, clicking `Paul (Direktnachricht)`
changed the title from `@Agent Notification - Discord` to `@Paul - Discord` and
the composer to `Nachricht an @Paul`; typing without a recipient or with a
mismatching one was rejected, and no message was sent.

### Real-application acceptance tests

`backend/tests/live_real_apps.py` is an opt-in harness that drives the real
agent, the real local model, real approvals, and real applications:

```powershell
backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario outlook
backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario browser
backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario explorer
backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario all
```

Measured on the reference machine with `qwen3-vl:8b-instruct`:

| Scenario | What it proves | Result |
| --- | --- | --- |
| `uia` | real Explorer UIA scan: 50 verified targets, no plain-text false positives | PASS |
| `outlook` | one compose window, To/Subject/Body filled exactly, no send, no loop | PASS (~60–120 s) |
| `browser` | isolated Chrome profile, Ctrl+L navigation, reads the real page heading | PASS (~11–16 s) |
| `explorer` | Ctrl+L navigation in a real Explorer window, then reads the new folder | PASS (~23–32 s) |

The harness denies Send/Delete/Format actions, launches Chrome with an isolated
`--user-data-dir`, discards Outlook drafts through the Outlook object model,
and closes only the windows it created. Notepad is a shared tabbed app on this
machine and games cannot be tested safely, so those are covered by the
deterministic mock/e2e suite instead.

### Self-improvement and durable lessons

`backend/agent/self_improvement.py` classifies tool failures and records a
successful recovery only when a later action proves that the alternative worked.
It also records optimized multi-step workflows. Lessons are written as bullets in
`memory/knowledge_base.md` under **Learned notes**, deduplicated by the existing
knowledge-base writer, and read back into every future system prompt.

Examples of learned facts:

- after a UI context shift, re-run `analyze_screen_ui` and use the new snapshot;
- after a window mismatch, call `get_open_windows` and pass the exact title;
- after a PowerShell failure, inspect stderr/exit_code, re-resolve the exact
  path/PID, then retry;
- a successful workflow such as `start_program -> get_open_windows ->
  analyze_screen_ui -> click_element -> keyboard_type_text`.

The reflector never learns from a user-triggered `aborted` result, never stores
raw screenshots, and redacts obvious credential patterns. Configuration:

```json
"agent": {
  "self_improvement_enabled": true,
  "self_improvement_workflow_min_tools": 3
}
```

### Phase 19 scenario matrix

`backend/tests/e2e_agent_tests.py` is the acceptance harness. It runs eight
scenarios:

| Scenario | What it proves |
| --- | --- |
| `filesystem_restructure` | Create a folder and move multiple real files; workflow fact saved |
| `ide_configuration` | Read, rewrite, and verify a JSON IDE settings file |
| `system_settings` | Create, read, remove, and verify a scoped `HKCU` test key |
| `ambiguous_request` | Ask for clarification without touching files |
| `gui_multi_app` | Type into a Notes mock, save it, then navigate an Outlook mock |
| `gui_popup` | Detect an unexpected modal pop-up; block a stale click; click OK |
| `gui_dynamic_resize` | Block stale coordinates after a resize; recover with a fresh snapshot |
| `gui_missing_element` | Block a missing element; relaunch/re-analyze; save a recovery fact |

Run all scenarios:

```powershell
backend/.venv/Scripts/python.exe backend/tests/e2e_agent_tests.py
```

Use `--skip-gui` on a headless machine or `--scenario NAME` for one case. The GUI
scenarios use real WinForms mocks (`mock_outlook.ps1`, `mock_notes.ps1`) and real
UI Automation/pyautogui, but never touch real documents or email. The last full
run passed **8/8 scenarios**.

### End-to-end Outlook scenario

`backend/tests/e2e_gui_outlook.py` launches a real WinForms mock of Outlook
(`backend/tests/mock_outlook.ps1`), then exercises the same services the agent
tools use:

1. detect the **New Email** button with Set-of-Mark,
2. click it with the highlight and smooth glide,
3. find and fill **To**, **Subject**, and **Body**,
4. click the mock **Send** and verify the captured fields,
5. in a separate phase, simulate a physical mouse twitch and assert the click is
   aborted and later GUI tools are halted.

It never sends a real email. Run it with:

```powershell
backend/.venv/Scripts/python.exe backend/tests/e2e_gui_outlook.py
```

A live agent run against the same mock also passed: the model called
`get_open_windows`, `analyze_screen_ui`, `click_element` for **New Email**, the
**To** and **Subject** fields, typed the values, clicked **Send**, and the mock
result contained `test@example.com` and `Phase 17 E2E`. The live Notepad dry run
exposed two real failures that are now fixed:

- `open_file` could open an executable as a document. It now rejects
  `.exe`/`.com`/`.lnk` and points to `start_program`.
- `analyze_screen_ui` could target whichever window was active instead of the
  app that was just launched. `start_program` now records the newly created
  window (excluding generic path words such as `Windows` and `System32`), and
  `analyze_screen_ui` prefers it. The agent is also told to call
  `get_open_windows` after a launch and to retry if the detected title is wrong.

`keyboard_type_text` additionally refuses to type until a text-capable element
was clicked, so moving the cursor cannot silently send keystrokes to the wrong
window.

## System diagnosis

`system_diagnosis` is a single read-only tool for broad requests such as
**“Analyze my computer”**, **“Do a full system diagnosis”**, or **“Check my PC’s
health and performance.”** It collects:

- OS build, install date, boot time, uptime, and hardware identity
- CPU model, cores, frequency, per-core utilization, and a sampled timeline
- Memory total/used/available/swap, plus current memory pressure
- GPU adapters, drivers, and GPU engine utilization by process
- Top processes by memory and CPU (the Task Manager view)
- Volumes, free space, physical disk media/health, and SMART reliability data
- Automatic services that are stopped, and startup applications
- Microsoft Defender, firewall, network adapters, and recent hotfixes
- Recent critical/error System events and pending reboot state
- Short CPU single/multi-thread and memory-copy benchmarks
- GPU utilization by process, including a warning when another app is using the
  GPU and slowing local model responses

It writes matplotlib charts to `memory/diagnostics/<timestamp>/`:

- `01_cpu_cores.png`
- `02_cpu_memory_timeline.png`
- `03_top_processes.png`
- `04_disks.png`
- `05_benchmarks.png`
- `06_gpu.png`

The same directory contains `report.json` with the complete structured report.
The tool returns a compact, model-friendly summary, including the per-core and
timeline series. The chat renders that data as native SVG/CSS visuals inside an
inline diagnosis card: health ring, CPU and memory charts, storage bars,
benchmark bars, findings by severity, top processes, and security chips. The
PNGs stay available as exported artifacts from the card footer but are not
posted as an image gallery. The answer itself is a short 250-350 word summary
(assessment, three priority findings, three actions), because the card already
carries the hardware, storage, security, process, and benchmark detail. The
prompt tells the model not to paste image links or call warnings critical, so
the text and visuals stay consistent.

The tool is read-only and needs no approval; it only writes its own report and
charts under `memory/diagnostics`. Deep file-integrity scans (`SFC`/`DISM`) are
not run automatically because they can take several minutes and may require
administrator rights. Benchmark numbers can be lower while other GPU/CPU-heavy
workloads are running.

For a broad diagnosis the agent calls this tool **once** instead of issuing many
individual PowerShell queries. Tool rounds are unlimited by default, so complex
follow-up diagnoses are not cut off. If you configure a positive
`agent.max_iterations` cap, the runtime asks the model for a final summary of the
collected evidence instead of failing with a “max tool steps” error.

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
confidence), plus window enumeration, display enumeration with previews, the
multi-display choice pause/selection/cancel flow, screen-analysis routing, and
system diagnosis collection, findings, chart generation, and tool dispatch.
Ollama is not required for the tests; the vision tests use a fake embedder and
fake capture/model methods, and the diagnosis tests use synthetic system data.

The opt-in live harness drives the real model, real approvals, and real
applications; it is not collected by pytest:

```powershell
backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario all
```

It denies Send/Delete/Format actions, isolates Chrome in a throwaway profile,
discards Outlook drafts through COM, and closes only its own windows.

### Phase 21: autonomous experiments and refinement

`backend/tests/autonomous_experiments.py` runs real CLI, GUI, multi-step, and
clarification experiments against a live backend and scores each one on
accuracy, performance, and UI/UX evidence. `autonomous_refine.py` turns failed
runs into a builder-ready bundle, restarts the backend or packaged app, and
reruns the failing ids. Reports, full SSE transcripts, annotated GUI
screenshots, and failure bundles are written under `build/experiments/`.

```powershell
cd backend
.\.venv\Scripts\python.exe -m tests.autonomous_experiments --category static --category cli --category edge
.\.venv\Scripts\python.exe -m tests.autonomous_experiments --category gui --include-gui
.\.venv\Scripts\python.exe -m tests.autonomous_refine --iterations 3 --focus-failures
```

The GUI suite moves the real mouse and keyboard and must only run while the
desktop is idle. The engine starts an isolated backend on port 8010 with
`keep_alive_seconds=0` and its own data folder; use
`--service external --base-url http://127.0.0.1:8000` to evaluate the running
packaged app instead. The architecture, scoring matrix, restart mechanism, and
honest limits are documented in
[`docs/autonomous-loop.md`](docs/autonomous-loop.md).

## Frontend

Ollama must be running. The desktop app starts the FastAPI backend itself as a
hidden child process, so no terminal is needed. In development:

```powershell
cd frontend
npm install
npm run tauri dev
```

For a terminal-free app, use the packaging script. It stages a private Python
runtime, validates the backend, builds the Tauri shell, and assembles a folder
whose only entry point is an `.exe`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Then double-click `build\Personal Agent\Personal Agent.exe`. The window appears
immediately while the backend and Ollama start hidden in the background. Add
`-Zip` for a portable archive or `-Installer` to also build the NSIS installer.
See [Windows app build](#windows-app-build) for details.

Do not use a plain `cargo build --release` for the distributable app: that can
leave the development URL in the binary and show `ERR_CONNECTION_REFUSED` when
Vite is not running.

The app reuses a healthy backend if one is already listening on port 8000.
Otherwise it starts the bundled runtime, redirects its output to
`%LOCALAPPDATA%\com.personalagent.desktop\logs`, and stops the whole backend
process tree when the app is quit. Launcher diagnostics are in `launcher.log` in
the same folder. During development it falls back to
`backend\.venv\Scripts\python.exe -m uvicorn`; set `PERSONAL_AGENT_BACKEND` to
the path of a packaged backend executable to override it.

The app starts hidden in the system tray. Click the tray icon to show or hide the
chat window; right-click it for **Show / Hide** and **Quit**. Closing the window
hides it instead of quitting.

The window is frameless and transparent: the header is a Tauri drag region, and
the **Hide to tray** button in the top-right calls `getCurrentWindow().hide()`.
The rounded card is drawn in CSS, so the area outside its corners stays
transparent. Use the tray icon to bring the window back.

The chat UI consumes `POST /chat/stream`, renders intermediate statuses, shows an
**Approval required** card with **Approve all / Deny** for the first modifying
step of a request, and restores a pending approval after a reload. The card uses
a compact **Now → Then** timeline: the exact current action(s), followed by every
planned step. Approving runs the remaining steps without another card.

When screen analysis is requested with more than one display connected, a
**Which display should I analyze?** card appears instead. It shows a live
thumbnail, index, resolution, primary badge, and position for each monitor;
clicking one analyzes exactly that display, and **Cancel** stops the capture.

While a turn runs, a compact task card updates in place with every tool step:
spinner for the current step, checkmarks for completed tasks, and expandable
error details for failures. It collapses after completion to a summary such as
**All tasks completed**, and can be expanded again to review the full list,
tool names, and durations.

The composer shows a rose **Stop** button while the agent is working. Stopping
keeps the partial assistant text and marks running steps as interrupted; the
backend records the text already shown plus **Interrupted by user.** so the
conversation stays coherent after a reload. A long-running tool finishes its
current operation before the turn actually stops. Messages render
GitHub-flavored Markdown with syntax-highlighted code blocks, tables, task
lists, and copy buttons. The dark theme uses Inter and a subtle
violet-to-emerald gradient frame. Set `VITE_API_URL` to override the default
backend URL `http://127.0.0.1:8000`.

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
  reply omits them. Tool results can expose a canonical `files` array, which is
  attached automatically. This keeps paths correct even if the model prints a
  wrong link or invents an external image URL; external image embeds are
  stripped on local image-search turns. `system_diagnosis` is the exception:
  its charts are rendered natively by the diagnosis card, so they are not
  duplicated as an image attachment gallery.
- Paths inside inline code and fenced code blocks are left untouched.
- The system prompt asks the model to emit
  `[name.ext](file:///C:/absolute/path/name.ext)` links with `%20` for spaces.

The Tauri capabilities grant only `shell:allow-open`,
`opener:allow-reveal-item-in-dir`, `clipboard-manager:allow-write-text`, and the
window hide/drag permissions. The asset protocol is enabled in
`src-tauri/tauri.conf.json` with a local-file read scope.

## Windows app build

`scripts\build_windows.ps1` turns the repository into a click-to-run Windows
app: the Tauri shell embeds the frontend, and the FastAPI backend ships with a
private embeddable CPython runtime plus the exact `site-packages` of
`backend\.venv`. The target machine does not need Python, Node.js, Rust, or a
terminal.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# optional: -Zip (portable archive), -Installer (NSIS installer)
```

The build machine still needs the prerequisites above: `backend\.venv` with
`requirements.txt` installed, Node.js, and the Rust toolchain with the C++
build tools. Only the target machine is dependency-free.

Output:

```text
build\Personal Agent\Personal Agent.exe   the only thing the user clicks
build\Personal Agent\backend\             FastAPI sources
build\Personal Agent\backend-runtime\     Python + all dependencies
build\Personal Agent\config\              default settings and personality
```

The script refuses to build if it cannot import every backend dependency and if
the bundled backend does not answer `/health` on a test port, so a broken
runtime is caught before the app is assembled. The Tauri shell is built with
the normal release profile, including the embedded frontend. The staged payload
is passed to the bundler as a build-time config override, so a plain
`npm run tauri dev` on a fresh checkout still works without a staged runtime.

On first launch the app:

1. shows the chat window immediately (the header reads **Starting** until the
   backend is ready);
2. starts Ollama from `%LOCALAPPDATA%\Programs\Ollama\ollama app.exe` if it is
   installed but not running;
3. starts the bundled backend with no console window and waits for `/health`;
4. copies `config\settings.json` and `config\personality.md` into the user data
   folder on first run.

The bundled backend runs from the install folder but writes only to
`%LOCALAPPDATA%\com.personalagent.desktop\data` (`config`, `memory`,
`knowledge_base.md`, the vision index, screenshots, and diagnosis charts).
Launcher and backend logs are in
`%LOCALAPPDATA%\com.personalagent.desktop\logs`. Edit the copied
`config\settings.json` or `personality.md` there and restart the app to apply
changes. Closing the window hides the app to the tray; **Quit** in the tray menu
stops the backend too.

Target requirements:

- Windows 10 or 11, 64-bit
- Microsoft Edge WebView2 runtime (preinstalled on Windows 11; the NSIS
  installer can bootstrap it)
- Ollama with the configured model (`ollama pull qwen3-vl:8b-instruct`)

The folder is about 1.3 GB unpacked because local vision/CLIP, OCR, and
matplotlib dependencies are included; the NSIS installer compresses it. The
executable is not code-signed, so SmartScreen may ask for confirmation the first
time it runs.



Generation speed depends far more on prompt length and GPU contention than on
tool schemas or streaming. Measured with `qwen3-vl:8b-instruct` Q4_K_M on the
RX 9070 XT reference machine:

| Workload | Prompt | Median output speed |
| --- | --- | --- |
| Short text prompt | 42 tokens | ~80 tok/s |
| App prompt + tools, 16k context | 9.4k tokens | ~14.6 tok/s |
| App prompt + tools, 20k context | 9.4k tokens | ~14.5 tok/s |
| App prompt + tools, 24k context | 9.4k tokens | ~26-27 tok/s |
| App prompt + tools, 28k context | 9.4k tokens | ~24.3 tok/s |
| App prompt + tools, 32k context | 9.4k tokens | ~22.5 tok/s |
| App prompt + tools, 24k context | 12.9k tokens | ~19.8 tok/s |
| Same workload with a game at ~98% GPU | 9-13k tokens | 2.6-9.4 tok/s |

`num_ctx=24576` is not just extra headroom: on this ROCm build it is roughly
75-85% faster than 16384 or 20480 for the same prompt, and faster than 28672 or
32768 as well. Tools, streaming, thermal throttling, and a stale Ollama runner
were ruled out. A GPU-heavy game or renderer sharing the GPU is the other major
cause of single-digit speeds; the diagnosis report detects this and names the
competing process. Response streaming keeps the perceived latency low while the
model generates. KV-cache quantization (`OLLAMA_KV_CACHE_TYPE=q8_0`) measured
the same as the default f16 cache and is not enabled.

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

Small local models can still try to answer a combined *action + question*
request from the currently visible state instead of performing the action
("what is the folder name?" answered before Ctrl+L navigation). The runtime and
prompt now enforce action-first ordering with foreground/focus checks, the
`outcome` state deltas, and loop detection; the live real-app harness verifies
this against Outlook, Chrome, and Explorer. If a model insists on the shortcut,
split the request into an action turn and a question turn.
