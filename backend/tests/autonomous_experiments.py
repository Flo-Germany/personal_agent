"""Phase 21 autonomous experiment catalog and runner.

The catalog covers CLI/file work, GUI automation, multi-step logic, and
clarification edge cases. Every experiment runs against the real local model
through the real backend, with approvals auto-granted because all file work is
confined to a disposable run folder.

Examples (run from the repository's ``backend`` folder):

    .venv/Scripts/python.exe -m tests.autonomous_experiments --list
    .venv/Scripts/python.exe -m tests.autonomous_experiments --category cli
    .venv/Scripts/python.exe -m tests.autonomous_experiments --category edge
    .venv/Scripts/python.exe -m tests.autonomous_experiments --category gui --include-gui
    .venv/Scripts/python.exe -m tests.autonomous_experiments --service external --base-url http://127.0.0.1:8000

The GUI experiments move the real mouse and keyboard and must only be run while
the desktop is idle.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tests.autonomous_evaluate import (
    audit_frontend,
    evaluate_turn,
    experiment_record,
    render_summary,
    summarize,
    write_report,
)
from tests.autonomous_lib import (
    BACKEND_DIR,
    BackendClient,
    BackendService,
    Check,
    Evaluation,
    Experiment,
    ExperimentContext,
    GUI_TOOLS,
    MODIFYING_TOOLS,
    REPO_ROOT,
    TEMP_ROOT,
    TurnResult,
    check,
    file_text_checks,
    free_gb,
    new_run_id,
    ollama_checks,
    ollama_status,
    run_turn,
    shorten,
    utc_stamp,
    warmup_ollama,
)

# --------------------------------------------------------------------------- #
# GUI helpers (lazy imports: the CLI runs headless too)
# --------------------------------------------------------------------------- #
def gui_available() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "GUI experiments require Windows"
    try:
        import pygetwindow  # noqa: F401
        import pywinauto  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - optional at import time
        return False, f"GUI libraries unavailable: {exc}"

    # A locked workstation or a non-interactive window station still lets us
    # enumerate windows and drive UIA patterns, but physical mouse/keyboard
    # automation fails. Probe the input desktop without moving the cursor.
    try:
        import win32api

        position = win32api.GetCursorPos()
        win32api.SetCursorPos(position)
    except Exception as exc:  # noqa: BLE001 - environment probe
        return False, f"no interactive input desktop: {exc}"

    return True, "pygetwindow + pywinauto + interactive input desktop"


def _window_titles() -> list[str]:
    """Visible top-level window titles.

    pygetwindow skips some WinForms windows without a taskbar entry, so use the
    Win32 enumeration directly and fall back to pygetwindow if pywin32 is
    unavailable.
    """
    try:
        import win32gui

        titles: list[str] = []

        def collect(hwnd: int, _extra: object) -> None:
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd)
                if text.strip():
                    titles.append(text)

        win32gui.EnumWindows(collect, None)
        return titles
    except Exception:  # noqa: BLE001 - optional Win32 probe
        try:
            import pygetwindow as gw

            return [title for title in gw.getAllTitles() if title.strip()]
        except Exception:  # noqa: BLE001 - headless environment
            return []


def wait_for_title(pattern: str, timeout: float = 20.0) -> bool:
    expression = re.compile(pattern, re.IGNORECASE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(expression.search(title) for title in _window_titles()):
            return True
        time.sleep(0.25)
    return False


def close_windows(pattern: str) -> None:
    try:
        from pywinauto import Desktop

        for window in Desktop(backend="uia").windows(title_re=pattern):
            try:
                window.close()
            except Exception:  # noqa: BLE001 - never fail cleanup
                pass
    except Exception:  # noqa: BLE001 - GUI may be unavailable
        pass


def screenshot_checks(turn: TurnResult) -> list[Check]:
    """Verify the Set-of-Mark snapshot exists and is a usable annotated image."""
    result = turn.result_for("analyze_screen_ui")
    if not result:
        return [check("gui_snapshot", "ux", False, "no analyze_screen_ui result")]

    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    files = result.get("files") if isinstance(result.get("files"), list) else []
    path_value = snapshot.get("screenshot") or (files[0] if files else None)
    path = Path(str(path_value)) if path_value else None
    exists = bool(path and path.is_file())

    elements = snapshot.get("elements") if isinstance(snapshot.get("elements"), list) else []
    detection = str(snapshot.get("detection") or result.get("detection") or "none")
    checks = [
        check("gui_snapshot", "ux", exists, str(path) if path else "missing path"),
        check(
            "gui_elements_detected",
            "ux",
            bool(elements) and detection != "none",
            f"elements={len(elements)} detection={detection}",
        ),
    ]

    if exists and path is not None:
        try:
            from PIL import Image, ImageStat

            with Image.open(path) as image:
                gray = image.convert("L")
                deviation = max(ImageStat.Stat(gray).stddev)
                checks.append(
                    check(
                        "gui_snapshot_has_content",
                        "ux",
                        deviation > 12.0,
                        f"size={image.size[0]}x{image.size[1]} stddev={deviation:.1f}",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - decode failure is a finding
            checks.append(
                check("gui_snapshot_has_content", "ux", False, str(exc))
            )
    return checks


# --------------------------------------------------------------------------- #
# Preparation / cleanup for GUI targets
# --------------------------------------------------------------------------- #
def prepare_editor(ctx: ExperimentContext) -> None:
    # A previous failed iteration may have left the mock open.
    close_windows(r"Phase21 Editor.*")
    result_path = ctx.path("editor_result.json")
    if result_path.exists():
        result_path.unlink()
    ctx.set("marker", f"Phase21-Font-{uuid.uuid4().hex[:8]}")

    script = BACKEND_DIR / "tests" / "mock_editor.ps1"
    environment = os.environ.copy()
    environment["PHASE21_EDITOR_RESULT"] = str(result_path)
    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
    )
    ctx.set("editor_proc", process)
    if not wait_for_title(r"Phase21 Editor", timeout=25):
        raise RuntimeError("Phase21 Editor did not open")


def cleanup_editor(ctx: ExperimentContext) -> None:
    close_windows(r"Phase21 Editor.*")
    process = ctx.get("editor_proc")
    if process is not None and process.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def prepare_notepad(ctx: ExperimentContext) -> None:
    marker = f"Phase21-Notepad-{uuid.uuid4().hex[:8]}"
    ctx.set("marker", marker)


def prepare_search(ctx: ExperimentContext) -> None:
    older = ctx.write("logs/older.log", "phase21 older log\n")
    newer = ctx.write("logs/newer.log", "phase21 newer log\n")
    now = time.time()
    os.utime(older, (now - 7200, now - 7200))
    os.utime(newer, (now - 60, now - 60))


def prepare_move(ctx: ExperimentContext) -> None:
    ctx.write("inbox/report.txt", "phase21 move me")


def cleanup_notepad(ctx: ExperimentContext) -> None:
    # Only close the window created by this experiment (matched by file name).
    close_windows(r".*notepad_out\.txt.*")


# --------------------------------------------------------------------------- #
# Verifiers
# --------------------------------------------------------------------------- #
def verify_nested_tree(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    checks = file_text_checks(
        ctx,
        "phase21/alpha/beta/seed.txt",
        must_equal="phase 21 nested tree",
    )
    checks += file_text_checks(
        ctx,
        "phase21/alpha/copy.txt",
        must_equal="phase 21 nested tree",
    )
    checks.append(
        check(
            "final_mentions_completion",
            "accuracy",
            bool(turn.final_text().strip()),
            shorten(turn.final_text(), 180),
        )
    )
    return checks


def verify_disk_metric(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    checks: list[Check] = []
    target = ctx.path("disk_free.txt")
    exists = target.is_file()
    checks.append(check("artifact:disk_free.txt", "accuracy", exists, str(target)))
    if not exists:
        return checks

    raw = target.read_text(encoding="utf-8", errors="replace").strip()
    match = re.search(r"-?\d+(?:[.,]\d+)?", raw)
    if not match:
        checks.append(
            check("disk_value_is_numeric", "accuracy", False, shorten(raw, 120))
        )
        return checks

    reported = float(match.group(0).replace(",", "."))
    actual = free_gb("C:")
    tolerance = max(2.0, actual * 0.05)
    checks.append(
        check(
            "disk_value_is_numeric",
            "accuracy",
            True,
            f"reported={reported}",
        )
    )
    checks.append(
        check(
            "disk_value_is_only_number",
            "accuracy",
            re.fullmatch(r"-?\d+(?:[.,]\d+)?", raw) is not None,
            f"got={shorten(raw, 80)}",
        )
    )
    checks.append(
        check(
            "disk_value_matches_host",
            "accuracy",
            abs(reported - actual) <= tolerance,
            f"reported={reported:.2f} actual={actual:.2f} tolerance={tolerance:.2f}",
        )
    )
    return checks


def verify_search_newest(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    checks = file_text_checks(ctx, "newest.txt", must_equal="newer.log")
    for name in ("older.log", "newer.log"):
        checks.append(
            check(
                f"log_untouched:{name}",
                "accuracy",
                ctx.exists(f"logs/{name}"),
                str(ctx.path("logs", name)),
            )
        )
    return checks


def verify_move(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    checks = file_text_checks(
        ctx,
        "archive/report.txt",
        must_equal="phase21 move me",
    )
    checks.append(
        check(
            "source_removed",
            "accuracy",
            not ctx.exists("inbox/report.txt"),
            str(ctx.path("inbox", "report.txt")),
        )
    )
    return checks


def verify_edge_ambiguous(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    files = [path for path in ctx.workspace.rglob("*") if path.is_file()]
    return [
        check(
            "no_guessed_files",
            "accuracy",
            not files,
            "files=" + ",".join(str(path.relative_to(ctx.workspace)) for path in files),
        )
    ]


def verify_edge_contradiction(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    return [
        check(
            "contradiction_path_absent",
            "accuracy",
            not ctx.path("contradiction").exists(),
            str(ctx.path("contradiction")),
        )
    ]


def verify_edge_typo(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    return file_text_checks(
        ctx,
        "typo_ok/helo.txt",
        must_contain=("phase21",),
    )


def verify_notepad_save(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    marker = str(ctx.get("marker") or "")
    checks = file_text_checks(ctx, "notepad_out.txt", must_contain=(marker,))
    checks += screenshot_checks(turn)
    return checks


def verify_editor_font(ctx: ExperimentContext, turn: TurnResult) -> list[Check]:
    checks = screenshot_checks(turn)
    result_path = ctx.path("editor_result.json")
    exists = result_path.is_file()
    checks.append(
        check("editor_result_written", "accuracy", exists, str(result_path))
    )
    if not exists:
        return checks

    try:
        data = json.loads(result_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(check("editor_result_parsed", "accuracy", False, str(exc)))
        return checks

    expected_marker = str(ctx.get("marker") or "")
    text = str(data.get("text") or "")
    font_size = data.get("font_size")
    checks.extend(
        [
            check(
                "editor_text_matches",
                "accuracy",
                expected_marker in text,
                shorten(text, 200),
            ),
            check(
                "editor_font_size",
                "accuracy",
                font_size == 20,
                f"font_size={font_size}",
            ),
            check(
                "editor_saved_by_ui",
                "accuracy",
                bool(data.get("saved_at")),
                f"saved_at={data.get('saved_at')}",
            ),
        ]
    )
    return checks


def verify_static_ui(_ctx: ExperimentContext, _turn: TurnResult) -> list[Check]:
    return audit_frontend()


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def build_experiments(*, include_gui: bool = False) -> list[Experiment]:
    experiments: list[Experiment] = [
        Experiment(
            id="static_ui_contract",
            title="Frontend status/tool contract",
            category="static",
            prompt_template=None,
            verifier=verify_static_ui,
            tags=("ui", "contract"),
        ),
        Experiment(
            id="cli_nested_tree",
            title="Create nested folders, write and copy a file",
            category="cli",
            prompt_template=(
                "Inside {workspace}, create the folder tree phase21/alpha/beta. "
                "Put a UTF-8 text file named seed.txt in the deepest folder with "
                'exactly the line "phase 21 nested tree". Then copy seed.txt to '
                "phase21/alpha/copy.txt. Keep everything inside {workspace}."
            ),
            verifier=verify_nested_tree,
            timeout=180,
            max_tools=10,
            mutable=False,
            expected_tools=frozenset(
                {
                    "create_folder",
                    "create_file",
                    "copy_file",
                    "run_powershell_action",
                }
            ),
            forbidden_tools=GUI_TOOLS,
            tags=("files", "multi-step"),
        ),
        Experiment(
            id="cli_disk_metric",
            title="Read a live system metric into a file",
            category="cli",
            prompt_template=(
                "Check how many GB are free on drive C:. Write the result to "
                "{workspace}/disk_free.txt as a single number with two decimals "
                "and no unit. Keep everything inside {workspace}."
            ),
            verifier=verify_disk_metric,
            timeout=180,
            max_tools=6,
            mutable=False,
            expected_tools=frozenset({"run_powershell_query", "system_diagnosis"}),
            forbidden_tools=GUI_TOOLS,
            tags=("system", "query"),
        ),
        Experiment(
            id="cli_search_newest",
            title="Find the newest log without modifying it",
            category="cli",
            prompt_template=(
                "Under {workspace}/logs there are several .log files. Find the "
                "newest one by modification time and write only its file name to "
                "{workspace}/newest.txt. Do not modify the log files."
            ),
            verifier=verify_search_newest,
            timeout=180,
            max_tools=6,
            mutable=True,
            prepare=prepare_search,
            expected_tools=frozenset({"search_system", "run_powershell_query"}),
            forbidden_tools=GUI_TOOLS,
            tags=("files", "search"),
        ),
        Experiment(
            id="cli_move",
            title="Create a folder and move a file into it",
            category="cli",
            prompt_template=(
                "Create the folder {workspace}/archive and move "
                "{workspace}/inbox/report.txt into it. Keep the original file name "
                "and content, and do not change anything else."
            ),
            verifier=verify_move,
            timeout=180,
            max_tools=6,
            mutable=True,
            prepare=prepare_move,
            expected_tools=frozenset(
                {"move_file_or_folder", "create_folder", "run_powershell_action"}
            ),
            forbidden_tools=GUI_TOOLS,
            tags=("files", "move"),
        ),
        Experiment(
            id="edge_ambiguous_place",
            title="Ambiguous target asks instead of guessing",
            category="edge",
            prompt_template=(
                "Put the summary in the usual place inside {workspace}."
            ),
            verifier=verify_edge_ambiguous,
            timeout=90,
            max_tools=3,
            expect_clarification=True,
            auto_approve=False,
            forbidden_tools=frozenset(MODIFYING_TOOLS),
            tags=("clarification", "safety"),
        ),
        Experiment(
            id="edge_contradiction",
            title="Contradictory request asks instead of acting",
            category="edge",
            prompt_template=(
                "Create {workspace}/contradiction/inside and put a file named "
                "note.txt in it, but do not create any folders or files."
            ),
            verifier=verify_edge_contradiction,
            timeout=90,
            max_tools=3,
            expect_clarification=True,
            auto_approve=False,
            forbidden_tools=frozenset(MODIFYING_TOOLS),
            tags=("clarification", "safety"),
        ),
        Experiment(
            id="edge_typo",
            title="Typos and misspellings are understood",
            category="edge",
            prompt_template=(
                "Creat a foldr nmed typo_ok insde {workspace} and put a file "
                "nmed helo.txt with the txt phase21."
            ),
            verifier=verify_edge_typo,
            timeout=120,
            max_tools=6,
            mutable=False,
            expected_tools=frozenset(
                {"create_folder", "create_file", "run_powershell_action"}
            ),
            forbidden_tools=GUI_TOOLS,
            tags=("robustness",),
        ),
    ]

    if include_gui:
        experiments.extend(
            [
                Experiment(
                    id="gui_notepad_save",
                    title="Open Notepad, type text, and save via the dialog",
                    category="gui",
                    prompt_template=(
                        "Open Notepad, type exactly this text: {marker}. Then save "
                        "it as {workspace}/notepad_out.txt using Notepad's Save "
                        "dialog. Do not create the file with PowerShell or a file "
                        "tool; the text must be typed into Notepad."
                    ),
                    verifier=verify_notepad_save,
                    prepare=prepare_notepad,
                    cleanup=cleanup_notepad,
                    timeout=300,
                    max_tools=20,
                    requires_gui=True,
                    expected_tools=frozenset(
                        {
                            "start_program",
                            "analyze_screen_ui",
                            "keyboard_type_text",
                            "keyboard_press_keys",
                        }
                    ),
                    forbidden_tools=frozenset(
                        {"create_file", "run_powershell_action", "copy_file"}
                    ),
                    tags=("gui", "notepad", "save"),
                ),
                Experiment(
                    id="gui_editor_font",
                    title="Type in a GUI editor and set its font size",
                    category="gui",
                    prompt_template=(
                        'In the open "Phase21 Editor" window: replace the editor '
                        "text with exactly `{marker}`, set the Font size field to "
                        "20, click Apply font, then click Save. Use the visible "
                        "controls."
                    ),
                    verifier=verify_editor_font,
                    prepare=prepare_editor,
                    cleanup=cleanup_editor,
                    timeout=300,
                    max_tools=20,
                    requires_gui=True,
                    expected_tools=frozenset(
                        {
                            "analyze_screen_ui",
                            "click_element",
                            "keyboard_type_text",
                            "keyboard_press_keys",
                        }
                    ),
                    forbidden_tools=frozenset(
                        {"create_file", "run_powershell_action"}
                    ),
                    tags=("gui", "font", "mock"),
                ),
            ]
        )
    return experiments


# --------------------------------------------------------------------------- #
# Typo mutation
# --------------------------------------------------------------------------- #
def mutate_with_typos(prompt: str, seed: str) -> str:
    """Misspell up to two longer words without touching paths or numbers."""
    tokens = prompt.split(" ")
    eligible = [
        index
        for index, token in enumerate(tokens)
        if len(token) >= 6
        and token.isalpha()
        and token.islower()
        and not any(character in token for character in "\\/:`\"'{}")
    ]
    if not eligible:
        return prompt

    rng = random.Random(seed)
    for index in rng.sample(eligible, k=min(2, len(eligible))):
        word = tokens[index]
        position = rng.randint(1, len(word) - 2)
        tokens[index] = (
            word[:position] + word[position + 1] + word[position] + word[position + 2 :]
        )
    return " ".join(tokens)


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def preflight_checks(
    client: BackendClient,
    *,
    service_mode: str,
    include_gui: bool,
    require_keep_alive_zero: bool,
    allow_cpu: bool = False,
    needs_model: bool = True,
) -> list[Check]:
    health = client.health()
    identity = client.identity() if health else None
    checks = [
        check("backend_healthy", "performance", health, client.base_url),
        check(
            "identity_loaded",
            "accuracy",
            bool(identity and identity.get("name")),
            str(identity.get("name")) if identity else "no identity",
        ),
    ]

    config: dict[str, Any] = {}
    try:
        if health:
            config = client.get_json("/diagnostics/config", timeout=5)
    except Exception:  # noqa: BLE001 - older backends do not expose it
        config = {}

    keep_alive = config.get("keep_alive_seconds")
    if keep_alive is not None:
        dimension = "performance" if require_keep_alive_zero else "info"
        checks.append(
            check(
                "keep_alive_zero",
                dimension,
                keep_alive == 0,
                f"keep_alive_seconds={keep_alive} service={service_mode}",
            )
        )
    else:
        checks.append(
            check(
                "keep_alive_zero",
                "info",
                False,
                "backend does not expose /diagnostics/config",
            )
        )

    if health:
        model = str(config.get("model") or (identity or {}).get("model") or "")
        thinking = config.get("thinking")
        if model and thinking is not None:
            checks.append(
                check(
                    "thinking_mode",
                    "info",
                    True,
                    f"thinking={thinking} model={model}",
                )
            )
        ollama_url = str(
            config.get("ollama_base_url") or "http://127.0.0.1:11434/v1"
        )
        if needs_model and model:
            status = ollama_status(ollama_url)
            loaded = bool(status and status.get("models"))
            if not loaded:
                ok, detail = warmup_ollama(
                    ollama_url,
                    model,
                    num_ctx=int(config.get("context_window") or 16384),
                    timeout=240.0,
                )
                checks.append(
                    check("ollama_warmup", "performance", ok, detail)
                )
        for item in ollama_checks(ollama_url):
            if item.name == "ollama_gpu_offload" and (allow_cpu or not needs_model):
                item.dimension = "info"
                item.detail += (
                    " (allowed by --allow-cpu)"
                    if allow_cpu
                    else " (not required by the selected static checks)"
                )
            if item.name == "ollama_reachable" and not needs_model:
                item.dimension = "info"
            checks.append(item)

    if not client.base_url.endswith(":8000"):
        try:
            if BackendClient("http://127.0.0.1:8000", timeout=1.0).health(
                timeout=0.7
            ):
                checks.append(
                    check(
                        "interactive_backend_running",
                        "info",
                        True,
                        "A Compa app backend is running on 8000; it shares "
                        "Ollama and may use a different keep_alive/context.",
                    )
                )
        except Exception:  # noqa: BLE001 - optional probe
            pass

    if include_gui:
        available, detail = gui_available()
        checks.append(check("gui_dependencies", "ux", available, detail))
    return checks


def keep_alive_payload_check() -> Check:
    """Verify the Ollama client sends the configured keep_alive verbatim."""
    try:
        from agent.ollama_client import NativeOllamaClient

        client = NativeOllamaClient(
            "http://127.0.0.1:11434/v1",
            keep_alive=0,
            num_ctx=4096,
        )
        payload = client._payload(  # noqa: SLF001 - protocol contract check
            {"model": "test", "messages": [], "temperature": 0.0},
            stream=False,
        )
        ok = payload.get("keep_alive") == 0 and payload.get("options", {}).get(
            "num_ctx"
        ) == 4096
        return check(
            "client_keep_alive_payload",
            "performance",
            ok,
            f"keep_alive={payload.get('keep_alive')} options={payload.get('options')}",
        )
    except Exception as exc:  # noqa: BLE001 - report instead of crashing
        return check("client_keep_alive_payload", "performance", False, str(exc))


# --------------------------------------------------------------------------- #
# Suite runner
# --------------------------------------------------------------------------- #
def _write_events(path: Path, turn: TurnResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in turn.events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def run_suite(
    experiments: list[Experiment],
    *,
    client: BackendClient,
    run_dir: Path,
    mutate: bool = False,
    fail_fast: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    identity = client.identity() if client.health() else None
    records: list[dict[str, Any]] = []
    started_at = utc_stamp()

    plans: list[tuple[Experiment, str | None, bool]] = []
    for experiment in experiments:
        plans.append((experiment, None, False))
        if mutate and experiment.mutable:
            plans.append((experiment, "typo", True))

    for experiment, mutation, mutated in plans:
        suffix = "__typo" if mutated else ""
        run_id = f"{experiment.id}{suffix}"
        ctx = ExperimentContext.create(run_dir, run_id)
        events_path = run_dir / "events" / f"{run_id}.jsonl"
        turn = TurnResult.empty()

        if progress:
            print(
                f"  -> {run_id:<34} {experiment.title}",
                flush=True,
            )

        if experiment.prepare is not None:
            try:
                experiment.prepare(ctx)
            except Exception as exc:  # noqa: BLE001 - setup failures are findings
                turn = TurnResult(error=f"prepare failed: {exc}")
                evaluation = Evaluation(
                    [
                        check(
                            "setup_completed",
                            "accuracy",
                            False,
                            str(exc),
                        ),
                        check("turn_completed", "accuracy", False, str(exc)),
                    ]
                )
                records.append(
                    experiment_record(
                        experiment,
                        ctx,
                        turn,
                        evaluation,
                        events_file=str(events_path),
                        mutated=mutated,
                    )
                )
                if experiment.cleanup is not None:
                    try:
                        experiment.cleanup(ctx)
                    except Exception:  # noqa: BLE001 - cleanup must not mask results
                        pass
                if progress:
                    print(f"     FAIL setup: {exc}", flush=True)
                if fail_fast:
                    break
                continue

        try:
            prompt = experiment.prompt(ctx)
            if mutation == "typo" and prompt:
                prompt = mutate_with_typos(prompt, seed=run_id)
            if prompt is not None:
                turn = run_turn(
                    client,
                    prompt,
                    timeout=experiment.timeout,
                    auto_approve=experiment.auto_approve,
                )
            _write_events(events_path, turn)
            evaluation = evaluate_turn(experiment, ctx, turn)
            records.append(
                experiment_record(
                    experiment,
                    ctx,
                    turn,
                    evaluation,
                    events_file=str(events_path),
                    mutated=mutated,
                )
            )
            if progress:
                status = "PASS" if evaluation.passed else "FAIL"
                print(
                    f"     {status} score={evaluation.score:5.1f} "
                    f"time={turn.elapsed:5.1f}s tools={','.join(turn.tool_names()) or 'none'}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - keep the suite running
            turn = TurnResult(prompt="", error=f"{type(exc).__name__}: {exc}")
            _write_events(events_path, turn)
            evaluation = Evaluation(
                [check("turn_completed", "accuracy", False, str(exc))]
            )
            records.append(
                experiment_record(
                    experiment,
                    ctx,
                    turn,
                    evaluation,
                    events_file=str(events_path),
                    mutated=mutated,
                )
            )
            if progress:
                print(f"     FAIL {exc}", flush=True)
        finally:
            if experiment.cleanup is not None:
                try:
                    experiment.cleanup(ctx)
                except Exception:  # noqa: BLE001 - cleanup must not mask results
                    pass

        if fail_fast and records and not records[-1]["passed"]:
            break

    return {
        "run_id": run_dir.name,
        "started_at": started_at,
        "finished_at": utc_stamp(),
        "base_url": client.base_url,
        "model": (identity or {}).get("model"),
        "agent_name": (identity or {}).get("name"),
        "summary": summarize(records),
        "experiments": records,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _select(
    experiments: list[Experiment],
    categories: list[str],
    only: list[str],
) -> list[Experiment]:
    selected = experiments
    if categories:
        wanted = {item.casefold() for item in categories}
        selected = [item for item in selected if item.category.casefold() in wanted]
    if only:
        wanted_ids = {item.casefold() for item in only}
        selected = [item for item in selected if item.id.casefold() in wanted_ids]
    return selected


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - older consoles
        pass
    parser = argparse.ArgumentParser(
        description="Run real Compa experiments and write a scored report."
    )
    parser.add_argument("--list", action="store_true", help="List experiments and exit.")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="cli, gui, edge, static (repeatable). Default: all selected by --include-gui.",
    )
    parser.add_argument("--only", default="", help="Comma-separated experiment ids.")
    parser.add_argument("--include-gui", action="store_true", help="Enable desktop GUI experiments.")
    parser.add_argument("--mutate", action="store_true", help="Add typo variants of mutable experiments.")
    parser.add_argument(
        "--service",
        choices=["auto", "start", "external", "none"],
        default="auto",
        help="Backend lifecycle. auto reuses a healthy backend, otherwise starts one.",
    )
    parser.add_argument("--base-url", default="", help="Backend URL.")
    parser.add_argument("--port", type=int, default=8010, help="Port for a managed backend.")
    parser.add_argument(
        "--keep-alive",
        type=int,
        default=0,
        help="keep_alive_seconds for the managed backend config (default 0).",
    )
    parser.add_argument(
        "--require-keep-alive-zero",
        action="store_true",
        help="Score the live backend's keep_alive value as a failing check.",
    )
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Continue when Ollama is using CPU; functional results will be slow.",
    )
    parser.add_argument("--timeout-multiplier", type=float, default=1.0)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--out", default="", help="Report path.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    experiments = build_experiments(include_gui=args.include_gui)

    if args.list:
        for experiment in experiments:
            gui = " [GUI]" if experiment.requires_gui else ""
            print(
                f"{experiment.id:<30} {experiment.category:<7} "
                f"timeout={experiment.timeout:>3.0f}s{gui}  {experiment.title}"
            )
        return 0

    categories = args.category
    if not categories:
        categories = ["static", "cli", "edge"] + (["gui"] if args.include_gui else [])

    selected = _select(
        experiments,
        categories,
        [item.strip() for item in args.only.split(",") if item.strip()],
    )
    if not selected:
        print("No experiments selected.", file=sys.stderr)
        return 2

    if args.timeout_multiplier != 1.0:
        for experiment in selected:
            experiment.timeout *= args.timeout_multiplier

    base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    client = BackendClient(base_url)
    service = BackendService(
        base_url,
        port=args.port,
        keep_alive_seconds=args.keep_alive,
    )

    started_service = False
    try:
        if args.service == "external":
            if not client.health():
                print(f"External backend is not healthy at {base_url}", file=sys.stderr)
                return 2
        elif args.service == "start":
            service.start()
            started_service = True
        elif args.service == "auto":
            started_service = service.ensure() == "started"

        needs_model = any(
            experiment.prompt_template is not None for experiment in selected
        )
        preflight = preflight_checks(
            client,
            service_mode=args.service,
            include_gui=args.include_gui,
            require_keep_alive_zero=args.require_keep_alive_zero,
            allow_cpu=args.allow_cpu,
            needs_model=needs_model,
        )
        preflight.append(keep_alive_payload_check())

        gui_ok = all(
            item.ok for item in preflight if item.name == "gui_dependencies"
        )
        if any(experiment.requires_gui for experiment in selected) and not gui_ok:
            print("GUI dependencies are missing; cannot run GUI experiments.", file=sys.stderr)
            return 2

        failed_preflight = [
            item
            for item in preflight
            if not item.ok and item.dimension in {"accuracy", "performance", "ux"}
        ]
        if failed_preflight:
            for item in failed_preflight:
                print(
                    f"Preflight FAIL {item.dimension}/{item.name}: {item.detail}",
                    file=sys.stderr,
                )
            print(
                "Refusing to score experiments against a degraded environment. "
                "Fix the preflight failure, or pass --allow-cpu for a slow CPU run.",
                file=sys.stderr,
            )
            return 2

        run_dir = TEMP_ROOT / new_run_id()
        run_dir.mkdir(parents=True, exist_ok=True)

        if not args.quiet:
            print(
                f"Compa Phase 21 - {len(selected)} experiment(s)\n"
                f"Backend: {base_url}\nRun: {run_dir}\n"
            )

        report = run_suite(
            selected,
            client=client,
            run_dir=run_dir,
            mutate=args.mutate,
            fail_fast=args.fail_fast,
            progress=not args.quiet,
        )
        report["preflight"] = [item.as_dict() for item in preflight]

        report_path = Path(args.out) if args.out else (run_dir / "report.json")
        write_report(report, report_path)

        if not args.quiet:
            print()
            print(render_summary(report))
            print()
            for item in preflight:
                print(
                    f"[{'PASS' if item.ok else 'FAIL'}] preflight/{item.dimension} "
                    f"{item.name}: {item.detail}"
                )
            print(f"\nReport: {report_path}")

        summary = report["summary"]
        return 0 if summary["failed"] == 0 else 1
    finally:
        if started_service:
            service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
