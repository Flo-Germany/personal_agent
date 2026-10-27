# Audit hardening (read-only audit follow-up)

This pass responds to the security/quality audit. It fixes the highest-risk
paths first and leaves the architectural items documented at the end.

## Critical

| Finding | Fix |
| --- | --- |
| **C1** PowerShell "read-only" was a denylist (`python -c`, `Out-File`, `mshta`, `$env:PATH=` all passed) | `run_powershell_query` is now **allowlisted**: every pipeline stage must be a `Get-*` cmdlet or an explicit read-only collector/formatter (`Select-*`, `Where-Object`, `Measure-Object`, `ConvertTo-Json`, `Test-Path`, …). External binaries, invocation operators, script paths, `&&`, `\|`, redirection, `Out-File`, `.NET` calls and environment assignments are rejected with a specific reason. 16/16 bypass/negative cases verified |
| **C1/H2** output buffered fully before truncation | Both pipes are read incrementally with a hard cap (`max_output_chars * 4 + 64 KiB`); the process tree is killed when the cap is crossed (`output_capped: true`) instead of risking OOM |
| **C2** one approval authorised every later mutating call | An approval covers exactly the calls on the card. A later mutating call in the same turn now produces a **new card** (read-only calls still continue). `auto_approve_remaining` is `False`; the button reads **Approve**, and the card says further changes ask again |
| **C3** approval consumed before the conversation lock | `resume()` **peeks**, validates, acquires the lock, and only then consumes. A raced duplicate returns a clear 409-style error instead of destroying the card |
| **C4** `/chat/approve` accepted a display card | Rejected with HTTP 409 before the stream starts, and `resume()` also guards it |
| **C5** tool calls parsed from plain text bypassed the schema allowlist | Text calls are validated by the executor (`accepts_call`): the name must be offered, required fields present, unknown fields rejected; at most 4 per message. JSON quoted from a document is treated as data |
| **C6** learned facts pasted into the system prompt as instructions | The knowledge block is capped at 6 000 chars and wrapped in `BEGIN/END USER NOTES (untrusted data)` with an explicit "never instructions" rule |
| **C7** recipient guard failed open | Chat apps without a verifiable composer **refuse to type** (`needs_recipient`). Matching is token-based (no substring matches) and single-name requests are refused for group/channel-looking labels |
| **C8** no CSP, `assetProtocol: **/*`, one-click execution via `shell.open` | Production CSP (`default-src 'self'`, `object-src 'none'`, API/asset connect-src, no `frame-ancestors`), separate `devCsp`, asset scope limited to app data/pictures/downloads/documents/desktop, and `shell.open` restricted to a **document/media extension allowlist**. File cards now **show in folder** on click; images open an in-app preview. Remote images in Markdown are blocked |
| **C9** UNC paths in file cards → NTLM disclosure | UNC paths are rejected in path parsing, `openLocalFile`/`revealLocalFile` require a drive-letter path, and the shell scope no longer accepts `\\` |
| **C10** window text could include password fields | UIA `CurrentIsPassword` and classic `ES_PASSWORD` controls are skipped before reading text/value |
| **C11** screen captures could follow a remote `ollama.base_url` | The Ollama vision path refuses non-loopback hosts with an explicit error; the native llama.cpp path is local by construction |

## High

- **H3** `max_iterations` now defaults to **60** and `max_auto_approved_actions` to **5** (0 is still honoured as unlimited but no longer the default).
- **H5** reused snapshots no longer refresh `created_at`, so the reuse TTL cannot be extended indefinitely.
- **H12** `WorkspaceManager` restores windows left isolated by a crash (handle **and** title must match) and releases them on backend shutdown; state writes remain atomic-friendly.
- **H15** the input killswitch hooks are stopped on backend shutdown (`ToolExecutor.shutdown`), and the automation halt is no longer cleared by unrelated turns.
- **H17** inferred deletions ignore generic predicates (`work`, `like`, `use`, …), so "I don't work at Siemens anymore" no longer deletes "prefers to work in the morning".
- **H18** prose starting with `[`/`<` is no longer treated as a broken tool call; only tool-looking JSON triggers the recovery path.
- **H19** SSE parsing normalises CRLF across chunk boundaries (a `\r` carry), so the final/action_required event cannot be dropped.
- **H20** the reader stops as soon as a terminal event arrives (cancelling the body) and fails after 60 s of silence, so Approve/Deny can never stay disabled on an open socket.
- **H21** an error boundary wraps the app; numeric hardware fields are null-safe.
- **H22** the approval card is kept until the backend accepts the approval, so a transient failure can be retried.

## UI and motion

- **Rounded corners**: the modern card radius now matches the Windows 11 window rounding (8 px) and the window asks DWM to round itself. A `SetWindowRgn` region approach was tried first but crashed WebView2 on this 25H2 build and was removed. The result is verified by pixel-sampling the four corners (no square backdrop).
- **Explorer context-menu row**: descriptions use `break-words`, and the control moves to its own right-aligned row below 520 px, so the text can no longer overlap or overflow the toggle.
- **Analyze with Compa**: the shell now sends `{path, exists, canonical}`. A missing file produces an immediate inline notice (no model call, no guessing); an existing file is sent as a backticked canonical path with "read it with read_file_content first". Verified with a spaced path: the 4B model calls `read_file_content` and answers from the content.
- **Motion language** (`frontend/src/lib/motion.ts`): entrances ease-out, exits ease-in, interface surfaces use a spring with slight overshoot. The palette card, body, question, buttons and empty state follow it; the native height animation uses smoothstep.

## Verification

```text
backend tests        435 passed
Rust shell tests       8 passed (incl. analyze payload existence/canonical)
PowerShell guard      16/16 bypass and negative cases
Analyze flow          missing file -> inline notice; real spaced path -> read_file_content ok
UI corners            4/4 sampled corners rounded, no square backdrop
CSP                   app boots and renders with the production policy
```

## Still open (honest list)

- **C7 depth**: browser-hosted web chats (`category=browser`) are still not recipient-checked; the chat-app guard is fail-closed, but a browser tab needs its own detection.
- **H4/H8/H13**: the per-conversation lock has no timeout, UIA/COM calls have no watchdog, and the UIA element cache is shared across turns. These need architectural changes (per-session drivers, cancellable COM workers) that were out of scope here.
- **H10**: `analyze_screen_ui` may still activate a window for OCR; the schema text now describes it as focus-affecting, but the default was not inverted.
- **H16**: `pyautogui.FAILSAFE` still turns a top-right corner click into a fake killswitch abort.
- **H23/H24**: local processes can still talk to the unauthenticated loopback API or launch `--qa`/`--analyze`.
- **M-level items** not addressed: screenshot retention policy, vision-index scan cost, O(n²) window text walk, conversation history growth, committed exe, developer fixture paths.
- **P1/P3**: token-by-token Markdown re-render and per-round prompt rebuild remain; they need a render scheduler and a prompt cache keyed by file mtime.
