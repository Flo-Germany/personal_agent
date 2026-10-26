# Phase 21 - autonomous experiment and refinement loop

This document describes the testing engine in `backend/tests/autonomous_*.py`.
It runs real tasks against the real Compa backend, scores each run on
accuracy, performance, and UI/UX evidence, and produces a failure bundle that
the builder agent can turn into a patch. After a patch, the loop restarts the
service and reruns the experiment.

## Components

```text
backend/tests/
  autonomous_lib.py           types, SSE client, HITL runner, scoring, services
  autonomous_experiments.py   experiment catalog, verifiers, CLI runner
  autonomous_evaluate.py      dimension scoring, static UI audit, reports
  autonomous_refine.py        iterate -> patch hook -> restart -> rerun
  mock_editor.ps1             deterministic GUI target with verifiable state
  test_autonomous_harness.py  unit tests for the harness itself
```

Runtime artifacts:

```text
build/experiments/<run-id>/
  iteration-01/
    report.json               scores, checks, prompts, tool calls, metrics
    events/<experiment>.jsonl full SSE transcript for every experiment
    failures.json             builder-ready bundle (failed checks + evidence)
    failures.md               human-readable brief
  workspaces/<experiment>/    disposable files created by the experiment
build/experiments/service/    isolated backend data + logs for the harness
```

## Experiment catalog

| id | Category | What it proves |
|---|---|---|
| `static_ui_contract` | static | The frontend still renders the status/tool contract (component, animation, truncation, tool_result handling, matching backend events) |
| `cli_nested_tree` | CLI | Creates a nested folder tree, writes exact file content, copies the file |
| `cli_disk_metric` | CLI | Queries a live system metric and formats it into a file within 5% of the host value |
| `cli_search_newest` | CLI | Finds the newest `.log` by mtime without modifying the logs |
| `cli_move` | CLI | Creates a folder and moves a file, preserving name and content |
| `edge_ambiguous_place` | edge | Asks a clarifying question instead of guessing where "the usual place" is |
| `edge_contradiction` | edge | Detects contradictory instructions and changes nothing |
| `edge_typo` | edge | Understands misspellings and still produces the exact artifact |
| `gui_notepad_save` | GUI | Launches Notepad, types text with the keyboard, saves through the dialog, no file-tool shortcut |
| `gui_editor_font` | GUI | Uses Set-of-Mark, replaces text, sets an exact font size, applies it and saves |

GUI experiments are opt-in (`--include-gui`) because they move the physical
mouse and keyboard. Run them only while the desktop is idle. The preflight also
probes `SetCursorPos` without moving the cursor; a locked workstation or a
non-interactive window station is reported as `no interactive input desktop`
and the GUI suite is refused instead of failing every experiment.

Every file experiment runs under
`build/experiments/<run>/iteration-XX/workspaces/<id>/`. Approval requests are
auto-approved for those tasks; the two clarification experiments run with
auto-approval disabled so a guessed destructive action cannot execute.

The mock editor has been verified independently of the model: its UIA tree
exposes `Editor`, `Font size`, `Apply font`, and `Save`, and driving those
controls writes `{"text": "...", "font_size": 20, ...}`. That gives the GUI
experiments a deterministic result instead of relying on a real editor's
per-window font state.

## Evaluation matrix

Every experiment produces named checks, each tagged with a dimension:

- **accuracy**: final answer present, correct artifact/content, optimal tool
  family, no forbidden tool family, clarification instead of guessing.
- **performance**: wall-clock within the experiment timeout, tool-call budget,
  final tokens/s and prompt tokens recorded, live `keep_alive` value, the
  client's actual Ollama payload.
- **ux**: every `tool_result` paired with a preceding `tool_started`, compact
  status summaries (<=240 chars), compact tool labels, streamed assistant text,
  no error events, valid annotated GUI screenshot with detected elements.
- **info**: findings that are reported but not scored (for example an older
  backend that does not expose `/diagnostics/config`).

A dimension score is the weighted percentage of passing checks in that
dimension. The experiment score is the mean of the dimensions that have
checks. An experiment passes only when every scored check passes. The suite
reports accuracy, performance, and UX separately, plus the overall average.

UI/UX is deliberately split into machine-checkable evidence and artifacts:

1. the SSE event contract (ordering, compactness, streaming);
2. a static source contract for the compact status component;
3. annotated Set-of-Mark screenshots (valid PNG, non-blank, elements detected)
   written by the backend and kept in the run folder for visual review.

The harness does not pretend to judge aesthetics with a threshold. It fails
regressions it can prove and leaves the screenshots and event transcripts for
the builder to inspect.

### Preflight and environment guards

Before any experiment the runner checks:

- backend `/health` and `/identity`;
- Ollama reachability (`/api/ps`);
- whether a loaded model is actually in VRAM (`size_vram > 0`), not silently
  running on CPU;
- the live `keep_alive` from `GET /diagnostics/config`;
- the client payload (`keep_alive`, `num_ctx`);
- GUI dependencies and an interactive input desktop when GUI tasks are selected;
- whether another Compa backend is running on port 8000 (it shares Ollama and
  may use different runtime settings).

A failing scored preflight aborts the run with exit code 2 instead of
attributing an environment problem to Compa. `--allow-cpu` downgrades the
CPU check to an informational finding for a deliberately slow CPU run.
On this machine the guard caught a real case: DWM held roughly 90% of the GPU,
Ollama's ROCm discovery timed out, and the 8B model fell back to CPU at
~8.5 tok/s. The harness now refuses to score that as an agent regression.

### keep_alive

The client unit test asserts that `keep_alive` and `options.num_ctx` reach the
native Ollama payload verbatim. The private evaluation backend is started with
`keep_alive_seconds=0` in its isolated config; `GET /diagnostics/config`
exposes the live value, and `--require-keep-alive-zero` turns a mismatch into a
scored failure. The packaged interactive app intentionally keeps the model warm
with `1800`; evaluating that binary with the flag will report the difference
instead of hiding it.

## Running

From `backend/`:

```powershell
# Fast, safe, no GUI: static + CLI + edge
.venv/Scripts/python.exe -m tests.autonomous_experiments --category static --category cli --category edge

# One experiment
.venv/Scripts/python.exe -m tests.autonomous_experiments --only cli_nested_tree

# Real GUI automation, desktop must be idle
.venv/Scripts/python.exe -m tests.autonomous_experiments --category gui --include-gui

# Evaluate the packaged app that is already running on port 8000
.venv/Scripts/python.exe -m tests.autonomous_experiments --service external --base-url http://127.0.0.1:8000

# Add typo variants of mutable experiments
.venv/Scripts/python.exe -m tests.autonomous_experiments --category cli --mutate
```

The runner starts its own backend on port 8010 by default. It uses an isolated
data folder (`build/experiments/service/data`) and disables the global CLIP
crawler there, so experiments never touch the user's knowledge base or index.
`--service auto` reuses a healthy backend on the selected port instead.

Exit codes: `0` all passed, `1` failed experiments, `2` setup/preflight failure.

## Auto-refinement loop

```powershell
.venv/Scripts/python.exe -m tests.autonomous_refine --iterations 3 --category cli --category edge
```

Per iteration:

1. ensure the target is running (private backend on 8010, or the packaged app);
2. run the selected experiments and write `report.json` and `events/*.jsonl`;
3. on failure, write `failures.json` / `failures.md` and update
   `build/experiments/latest_failure_bundle.json`;
4. if `--refine-command` is supplied, run it with
   `COMPA_BUNDLE`, `COMPA_ITERATION`, `COMPA_RUN_DIR`, and `COMPA_FAILED_IDS`;
5. restart the target (backend process or app) so the patch is active;
6. rerun. With `--focus-failures`, iterations after the first run only the
   previously failing ids, then the suite can be widened again.

The harness never rewrites production code on its own. The refinement command
is the builder hook:

```powershell
--refine-command "opencode run \"Fix the failures in {bundle}. Read the event logs, patch the backend or frontend, and stop after the fix.\""
```

That separation is deliberate: the bundle is a compact, reproducible brief and
the builder is responsible for the diff. A model that can edit code is not
granted unsupervised write access by the test runner.

### Restart mechanism (exact)

**Backend target** (`--target backend`, default):

```text
BackendService.restart(force=True)
  -> taskkill /PID <uvicorn> /T /F     (whole process tree)
  -> start:
       <python> -m uvicorn main:app --host 127.0.0.1 --port 8010
       cwd = backend/
       PERSONAL_AGENT_DATA_DIR = build/experiments/service/data
       CREATE_NO_WINDOW
  -> poll GET /health every 300 ms for up to 90 s
```

A new process means changed Python modules, prompts, and `settings.json` are
loaded fresh. `--force` also stops a stale Python listener on the harness port
so an old generation cannot be reused.

**App target** (`--target app`):

```text
DesktopService.restart(rebuild=--rebuild)
  -> taskkill /IM "Personal Agent.exe" /F
  -> stop python.exe processes whose path is inside build/Personal Agent/
  -> optional: powershell scripts/build_windows.ps1
       (re-embeds the React build and repackages backend + runtime + installer)
  -> start build/Personal Agent/Personal Agent.exe
  -> poll GET /health on 127.0.0.1:8000 for up to 90 s
```

Use `--target app --rebuild` after a frontend/Tailwind change. Backend-only
changes only need a backend restart, which takes seconds.

## Verification of the harness

`test_autonomous_harness.py` covers the loop itself:

- SSE turn runner, approval and display continuations, closed-stream handling;
- dimension scoring and the all-checks-pass gate;
- clarification vs. guessing;
- paired and compact UI events;
- artifact checks;
- deterministic typo mutation that never corrupts paths;
- the frontend source contract;
- the native Ollama `keep_alive` / `num_ctx` payload.

Run it together with the normal suite:

```powershell
.venv/Scripts/python.exe -m pytest -q
```

## Current live results

On 2026-10-26 the static contract suite passed 100%. A GPU-backed live run
completed `cli_nested_tree` with the correct tools and artifacts
(`create_folder -> create_file -> copy_file`) and scored 93.3 because the first
approved action had no `tool_started` event. That was a real UI-status gap, not
a test artifact: the frontend had to synthesize a completed row without the
tool label. The backend now emits a per-action `tool_started` event before each
approved result, and `tests/test_hitl_api.py` locks the ordering in.

The next full live run was refused by the preflight: DWM was holding roughly
90% of the GPU, Ollama's ROCm discovery timed out, and the model fell back to
CPU at ~8.5 tok/s (120 s client timeouts). That is the intended behavior of the
guard. Re-run the suite after the GPU is healthy (or use `--allow-cpu` with a
large `--timeout-multiplier` for a slow functional check). The harness does not
turn an environment failure into a fake pass.

## Honest limits

- **100% is a gate, not a guarantee.** The loop can prove that every task in
  the current catalog passed on this machine, with the current model and
  config. "Any arbitrary desktop workflow" cannot be proven by a finite
  catalog; new tasks must be added when new capabilities are added.
- **No isolated VM.** GUI experiments drive the logged-in desktop. They must
  not run while the user is working, and a modal owned by another app can make
  a run fail for environmental reasons. The event logs and screenshots make
  that distinction visible.
- **Aesthetic judgment is human/builder work.** The harness enforces structural
  UI contracts and captures screenshots; it does not replace a visual review.
- **`keep_alive=0` is slower by design.** The evaluation profile pays a model
  reload per step. Use `--keep-alive 1800` for speed when the VRAM check is not
  the goal.
- **The model can still fail.** The correct response to a red experiment is a
  prompt/tool/frontend patch, not deleting the check. The failure bundle keeps
  the evidence needed to choose the right fix.
