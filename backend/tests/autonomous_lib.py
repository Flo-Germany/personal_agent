"""Core types and helpers for the Phase 21 autonomous experiment loop.

The harness drives a real Compa backend over HTTP, evaluates the resulting
SSE event stream and filesystem artifacts, and writes a machine-readable
report. It intentionally has no import-time dependency on Ollama, Tauri, or
Windows GUI libraries so the CLI and unit tests work everywhere.

Run the suite from the repository root:

    backend/.venv/Scripts/python.exe -m tests.autonomous_experiments --list
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
TEMP_ROOT = REPO_ROOT / "build" / "experiments"

DIMENSIONS = ("accuracy", "performance", "ux")
MODIFYING_TOOLS = frozenset(
    {
        "create_folder",
        "create_file",
        "copy_file",
        "move_file_or_folder",
        "delete_file_or_folder",
        "unzip_file",
        "start_program",
        "open_file",
        "run_powershell_action",
        "move_cursor_to",
        "click_element",
        "keyboard_type_text",
        "keyboard_press_keys",
    }
)
GUI_TOOLS = frozenset(
    {
        "analyze_screen_ui",
        "get_desktop_context",
        "click_element",
        "move_cursor_to",
        "keyboard_type_text",
        "keyboard_press_keys",
        "get_open_windows",
    }
)
READ_ONLY_TOOLS = frozenset(
    {
        "search_system",
        "read_file_content",
        "run_powershell_query",
        "system_diagnosis",
        "get_open_windows",
        "read_window_content",
        "analyze_screen",
        "analyze_screen_ui",
        "get_desktop_context",
        "semantic_image_search",
        "propose_plan",
    }
)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_id() -> str:
    return f"{utc_stamp()}-{uuid.uuid4().hex[:6]}"


def json_safe(value: Any) -> Any:
    """Return a JSON-serialisable copy of arbitrary tool/event data."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        return repr(value)


def shorten(text: str, limit: int = 600) -> str:
    text = str(text).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def contains_question(text: str) -> bool:
    """True when the assistant is asking the user for information."""
    lowered = (text or "").casefold()
    markers = (
        "?",
        "which ",
        "where ",
        "what do you",
        "could you",
        "can you tell",
        "please specify",
        "please clarify",
        "do you want",
        "should i",
        "welche",
        "wo ",
        "was meinst",
        "bitte sag",
        "bitte präzis",
    )
    return any(marker in lowered for marker in markers)


def free_gb(drive: str = "C:") -> float:
    root = Path(drive + os.sep)
    usage = shutil.disk_usage(root)
    return usage.free / (1024**3)


def ollama_status(base_url: str = "http://127.0.0.1:11434") -> dict[str, Any] | None:
    """Return the native Ollama /api/ps payload, or None when unreachable."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        with urllib.request.urlopen(f"{root}/api/ps", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def warmup_ollama(
    base_url: str,
    model: str,
    *,
    num_ctx: int = 16384,
    keep_alive: int = 60,
    timeout: float = 240.0,
) -> tuple[bool, str]:
    """Load the model before scoring so CPU fallback is detected up front."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": 1,
            "temperature": 0,
        },
    }
    request = urllib.request.Request(
        f"{root}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - preflight evidence
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"model loaded in {time.monotonic() - started:.1f}s"


def ollama_checks(base_url: str = "http://127.0.0.1:11434") -> list[Check]:
    """Verify Ollama is reachable and, when loaded, actually on the GPU."""
    status = ollama_status(base_url)
    if status is None:
        return [
            check(
                "ollama_reachable",
                "performance",
                False,
                f"{base_url}/api/ps is unreachable",
            )
        ]

    checks = [
        check("ollama_reachable", "performance", True, base_url),
    ]
    models = status.get("models") if isinstance(status.get("models"), list) else []
    if not models:
        checks.append(
            check(
                "ollama_gpu_offload",
                "info",
                True,
                "no model loaded yet; offload is verified after the first request",
            )
        )
        return checks

    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "model")
        size = float(model.get("size") or 0)
        size_vram = float(model.get("size_vram") or 0)
        ratio = (size_vram / size * 100.0) if size > 0 else 0.0
        checks.append(
            check(
                "ollama_gpu_offload",
                "performance" if size_vram <= 0 else "info",
                size_vram > 0,
                f"{name}: {ratio:.0f}% in VRAM "
                f"({size_vram / (1024**3):.1f}/{size / (1024**3):.1f} GiB)",
            )
        )
        break
    return checks


# --------------------------------------------------------------------------- #
# Backend client (SSE + JSON)
# --------------------------------------------------------------------------- #
class BackendUnavailable(RuntimeError):
    pass


class BackendClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- plain JSON -------------------------------------------------------
    def _open(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        accept: str = "application/json",
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": accept,
            },
            method="POST" if data is not None else "GET",
        )
        return urllib.request.urlopen(request, timeout=timeout or self.timeout)

    def get_json(self, path: str, timeout: float | None = None) -> dict[str, Any]:
        with self._open(path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def health(self, timeout: float = 2.0) -> bool:
        try:
            with self._open("/health", timeout=timeout) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def identity(self) -> dict[str, Any] | None:
        try:
            return self.get_json("/identity")
        except (OSError, urllib.error.URLError):
            return None

    def vision_status(self) -> dict[str, Any] | None:
        try:
            return self.get_json("/vision/status")
        except (OSError, urllib.error.URLError):
            return None

    def interrupt(self, conversation_id: str) -> None:
        try:
            with self._open(
                "/chat/interrupt",
                {"conversation_id": conversation_id},
                timeout=5.0,
            ):
                pass
        except (OSError, urllib.error.URLError):
            pass

    # -- SSE --------------------------------------------------------------
    def stream_events(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        """Yield decoded SSE events from a chat/approval/display endpoint."""
        response = self._open(
            path,
            payload,
            timeout=max(5.0, timeout),
            accept="text/event-stream",
        )
        data_lines: list[str] = []
        event_name = "message"
        try:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        raw_data = "\n".join(data_lines)
                        try:
                            event = json.loads(raw_data)
                        except json.JSONDecodeError:
                            event = {
                                "type": "parse_error",
                                "event": event_name,
                                "raw": shorten(raw_data),
                            }
                        event.setdefault("_sse_event", event_name)
                        yield event
                    data_lines = []
                    event_name = "message"
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        finally:
            response.close()


# --------------------------------------------------------------------------- #
# One agent turn, including HITL approvals
# --------------------------------------------------------------------------- #
@dataclass
class TurnResult:
    prompt: str = ""
    conversation_id: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    elapsed: float = 0.0
    final: str | None = None
    error: str | None = None
    pending: dict[str, Any] | None = None

    @classmethod
    def empty(cls) -> "TurnResult":
        return cls()

    @property
    def tool_starts(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event.get("type") == "status" and event.get("phase") == "tool_started"
        ]

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == "tool_result"]

    @property
    def metrics_final(self) -> dict[str, Any] | None:
        metrics = [
            event
            for event in self.events
            if event.get("type") == "metrics" and not event.get("live")
        ]
        return metrics[-1] if metrics else None

    def tool_names(self) -> list[str]:
        return [str(event.get("tool") or "") for event in self.tool_results]

    def used(self, *names: str) -> bool:
        used = set(self.tool_names())
        return bool(used.intersection(names))

    def used_any(self, names: set[str] | frozenset[str]) -> bool:
        return bool(set(self.tool_names()).intersection(names))

    def result_for(self, name: str) -> dict[str, Any] | None:
        for event in self.tool_results:
            if event.get("tool") == name:
                result = event.get("result")
                return result if isinstance(result, dict) else None
        return None

    def final_text(self) -> str:
        return self.final or ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.final is not None


def run_turn(
    client: BackendClient,
    prompt: str,
    *,
    auto_approve: bool = True,
    display: int | None = 1,
    timeout: float = 180.0,
    conversation_id: str | None = None,
) -> TurnResult:
    """Run one prompt and follow every approval/display continuation.

    The harness auto-approves because the experiments use disposable test
    folders. The events are kept verbatim so the evaluator can inspect tool
    choice, ordering, timing, and UI-facing status data.
    """
    started = time.monotonic()
    deadline = started + timeout
    cid = conversation_id or str(uuid.uuid4())
    events: list[dict[str, Any]] = []
    final: str | None = None
    error: str | None = None
    pending: dict[str, Any] | None = None

    path = "/chat/stream"
    payload: dict[str, Any] = {"message": prompt, "conversation_id": cid}
    continuations = 0

    while True:
        if time.monotonic() >= deadline:
            error = f"timed out after {timeout:.0f}s"
            client.interrupt(cid)
            break

        continuation: tuple[str, dict[str, Any]] | None = None
        stream_closed = False
        stream = client.stream_events(
            path,
            payload,
            timeout=max(5.0, deadline - time.monotonic()),
        )
        try:
            for event in stream:
                events.append(event)
                cid = str(event.get("conversation_id") or cid)
                event_type = event.get("type")

                if event_type == "final":
                    final = str(event.get("reply") or "")
                    break
                if event_type == "error":
                    error = str(event.get("message") or "backend error")
                    break
                if event_type == "action_required":
                    pending = event
                    if auto_approve:
                        continuation = (
                            "/chat/approve",
                            {
                                "approval_id": event.get("approval_id"),
                                "approved": True,
                            },
                        )
                    break
                if event_type == "display_required":
                    pending = event
                    if display is not None:
                        continuation = (
                            "/chat/display",
                            {
                                "approval_id": event.get("approval_id"),
                                "display": int(display),
                            },
                        )
                    else:
                        continuation = (
                            "/chat/display",
                            {
                                "approval_id": event.get("approval_id"),
                                "display": None,
                            },
                        )
                    break
            else:
                stream_closed = True
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

        if final is not None or error is not None:
            break
        if continuation is None:
            if stream_closed and pending is None:
                error = "event stream closed before a final answer"
            break

        continuations += 1
        if continuations > 6:
            error = "too many approval/display continuations"
            break
        pending = None
        path, payload = continuation

    elapsed = time.monotonic() - started
    return TurnResult(
        prompt=prompt,
        conversation_id=cid,
        events=events,
        elapsed=elapsed,
        final=final,
        error=error,
        pending=pending,
    )


# --------------------------------------------------------------------------- #
# Evaluation types
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    dimension: str
    ok: bool
    detail: str = ""
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dimension": self.dimension,
            "ok": self.ok,
            "detail": self.detail,
            "weight": self.weight,
        }


@dataclass
class Evaluation:
    checks: list[Check]

    @property
    def scored(self) -> list[Check]:
        return [check for check in self.checks if check.dimension in DIMENSIONS]

    def dimensions(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for dimension in DIMENSIONS:
            checks = [check for check in self.checks if check.dimension == dimension]
            if not checks:
                continue
            total = sum(check.weight for check in checks)
            passed = sum(check.weight for check in checks if check.ok)
            result[dimension] = round(100.0 * passed / total, 1) if total else 0.0
        return result

    @property
    def score(self) -> float:
        scores = list(self.dimensions().values())
        return round(sum(scores) / len(scores), 1) if scores else 0.0

    @property
    def passed(self) -> bool:
        return bool(self.scored) and all(check.ok for check in self.scored)

    def failures(self) -> list[Check]:
        return [check for check in self.scored if not check.ok]


def check(
    name: str,
    dimension: str,
    ok: bool,
    detail: str = "",
    *,
    weight: float = 1.0,
) -> Check:
    return Check(
        name=name,
        dimension=dimension,
        ok=bool(ok),
        detail=detail,
        weight=weight,
    )


# --------------------------------------------------------------------------- #
# Experiment context
# --------------------------------------------------------------------------- #
@dataclass
class ExperimentContext:
    run_dir: Path
    experiment_id: str
    workspace: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, run_dir: Path, experiment_id: str) -> "ExperimentContext":
        workspace = run_dir / "workspaces" / experiment_id
        workspace.mkdir(parents=True, exist_ok=True)
        return cls(run_dir=run_dir, experiment_id=experiment_id, workspace=workspace)

    def path(self, *parts: str) -> Path:
        return self.workspace.joinpath(*parts)

    def write(self, relative: str, text: str, encoding: str = "utf-8") -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding)
        return target

    def read(self, relative: str) -> str | None:
        target = self.path(relative)
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def exists(self, relative: str) -> bool:
        return self.path(relative).exists()

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.15)
        return predicate()


@dataclass
class Experiment:
    id: str
    title: str
    category: str
    prompt_template: str | None
    verifier: Callable[[ExperimentContext, TurnResult], list[Check]]
    timeout: float = 180.0
    max_tools: int = 10
    prepare: Callable[[ExperimentContext], None] | None = None
    cleanup: Callable[[ExperimentContext], None] | None = None
    requires_gui: bool = False
    mutable: bool = False
    auto_approve: bool = True
    tags: tuple[str, ...] = ()
    expected_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()
    expect_clarification: bool = False

    def prompt(self, ctx: ExperimentContext) -> str | None:
        if self.prompt_template is None:
            return None
        values: dict[str, str] = {
            "workspace": str(ctx.workspace),
            "run_dir": str(ctx.run_dir),
        }
        for key, value in ctx.data.items():
            if isinstance(value, (str, int, float)):
                values[key] = str(value)
        return self.prompt_template.format(**values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "requires_gui": self.requires_gui,
            "mutable": self.mutable,
            "tags": list(self.tags),
            "timeout": self.timeout,
            "max_tools": self.max_tools,
        }


# --------------------------------------------------------------------------- #
# Shared checks
# --------------------------------------------------------------------------- #
def event_checks(turn: TurnResult) -> list[Check]:
    """UI/UX contract checks derived from the SSE stream."""
    checks: list[Check] = []

    open_starts: dict[str, int] = {}
    paired = True
    for event in turn.events:
        if event.get("type") == "status" and event.get("phase") == "tool_started":
            name = str(event.get("tool") or "")
            open_starts[name] = open_starts.get(name, 0) + 1
        elif event.get("type") == "tool_result":
            name = str(event.get("tool") or "")
            if open_starts.get(name, 0) <= 0:
                paired = False
            else:
                open_starts[name] -= 1
    checks.append(
        check(
            "tool_events_paired",
            "ux",
            paired,
            "Every tool_result follows a matching tool_started event.",
        )
    )

    summaries = [str(event.get("summary") or "") for event in turn.tool_results]
    compact = all(0 < len(summary) <= 240 for summary in summaries)
    checks.append(
        check(
            "status_summaries_compact",
            "ux",
            compact,
            f"max={max((len(item) for item in summaries), default=0)} chars",
        )
    )

    streamed = any(
        event.get("type") in {"assistant_delta", "final"}
        and (event.get("type") == "assistant_delta" or event.get("streamed"))
        for event in turn.events
    )
    checks.append(
        check(
            "assistant_streamed",
            "ux",
            streamed,
            "Assistant text arrived as a streamed block or a streamed final.",
        )
    )

    no_error_events = not any(event.get("type") == "error" for event in turn.events)
    checks.append(
        check(
            "no_error_events",
            "ux",
            no_error_events,
            "No error event was emitted.",
        )
    )

    tool_labels = [
        str(event.get("message") or "")
        for event in turn.events
        if event.get("type") == "status" and event.get("phase") == "tool_started"
    ]
    labels_ok = all(0 < len(label) <= 160 for label in tool_labels)
    checks.append(
        check(
            "tool_labels_compact",
            "ux",
            labels_ok,
            f"{len(tool_labels)} tool labels, max "
            f"{max((len(item) for item in tool_labels), default=0)} chars",
        )
    )
    return checks


def performance_checks(
    turn: TurnResult,
    *,
    timeout: float,
    max_tools: int,
    expect_model: bool = True,
) -> list[Check]:
    checks = [
        check(
            "within_timeout",
            "performance",
            turn.error is None and turn.elapsed <= timeout,
            f"{turn.elapsed:.1f}s of {timeout:.0f}s"
            + (f" ({turn.error})" if turn.error else ""),
        ),
        check(
            "tool_budget",
            "performance",
            len(turn.tool_results) <= max_tools,
            f"{len(turn.tool_results)} tool call(s), budget {max_tools}",
        ),
    ]

    metrics = turn.metrics_final
    if expect_model and turn.events:
        checks.append(
            check(
                "throughput_recorded",
                "performance",
                bool(metrics and (metrics.get("tokens_per_second") or 0) > 0),
                f"tokens/s={metrics.get('tokens_per_second') if metrics else None}",
            )
        )
        checks.append(
            check(
                "prompt_tokens_recorded",
                "performance",
                bool(metrics and (metrics.get("prompt_tokens") or 0) > 0),
                f"prompt_tokens={metrics.get('prompt_tokens') if metrics else None}",
            )
        )
    return checks


def tool_choice_checks(
    turn: TurnResult,
    *,
    expected: frozenset[str],
    forbidden: frozenset[str],
) -> list[Check]:
    used = set(turn.tool_names())
    checks: list[Check] = []
    if expected:
        checks.append(
            check(
                "optimal_tool_family",
                "accuracy",
                bool(used.intersection(expected)),
                "used=" + ",".join(sorted(used)),
            )
        )
    if forbidden:
        violation = sorted(used.intersection(forbidden))
        checks.append(
            check(
                "no_wrong_tool_family",
                "accuracy",
                not violation,
                "unexpected=" + ",".join(violation) if violation else "ok",
            )
        )
    return checks


def clarification_checks(turn: TurnResult, *, label: str) -> list[Check]:
    text = turn.final_text()
    modifying = sorted(set(turn.tool_names()).intersection(MODIFYING_TOOLS))
    return [
        check(
            "asks_for_clarification",
            "accuracy",
            contains_question(text),
            f"final={shorten(text, 220)}",
        ),
        check(
            "no_destructive_guess",
            "accuracy",
            not modifying,
            "modifying_tools=" + (",".join(modifying) or "none"),
        ),
        check(
            "clarification_answer_present",
            "ux",
            bool(text.strip()),
            f"{label} produced a user-facing answer",
        ),
    ]


def file_text_checks(
    ctx: ExperimentContext,
    path: str,
    *,
    must_contain: tuple[str, ...] = (),
    must_equal: str | None = None,
) -> list[Check]:
    checks: list[Check] = []
    target = ctx.path(path)
    exists = target.is_file()
    checks.append(
        check(
            f"artifact:{path}",
            "accuracy",
            exists,
            str(target),
        )
    )
    if not exists:
        return checks

    text = target.read_text(encoding="utf-8", errors="replace")
    if must_equal is not None:
        checks.append(
            check(
                f"artifact_content:{path}",
                "accuracy",
                text.strip() == must_equal.strip(),
                f"got={shorten(text, 180)}",
            )
        )
    for needle in must_contain:
        checks.append(
            check(
                f"artifact_contains:{needle}",
                "accuracy",
                needle.casefold() in text.casefold(),
                f"got={shorten(text, 180)}",
            )
        )
    return checks


# --------------------------------------------------------------------------- #
# Backend process management
# --------------------------------------------------------------------------- #
class BackendService:
    """Start, stop, and restart the FastAPI backend for an experimental run.

    The harness uses an isolated data directory and disables the global CLIP
    crawler in its copy of settings.json. That keeps experiments fast and
    prevents a test run from rewriting the user's real index.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        *,
        port: int = 8010,
        data_dir: Path | None = None,
        log_dir: Path | None = None,
        keep_alive_seconds: int | None = None,
    ) -> None:
        self.client = BackendClient(base_url)
        self.port = port
        self.keep_alive_seconds = keep_alive_seconds
        self.data_dir = data_dir or (TEMP_ROOT / "service" / "data")
        self.log_dir = log_dir or (TEMP_ROOT / "service" / "logs")
        self._process: subprocess.Popen[bytes] | None = None
        self._owns_process = False

    def _prepare_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        config_dir = self.data_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        target = config_dir / "settings.json"
        if target.exists() and self.keep_alive_seconds is None:
            return
        source = REPO_ROOT / "config" / "settings.json"
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        # The crawler is irrelevant to the experiments and can run for hours.
        data.setdefault("vision", {})["enabled"] = False
        if self.keep_alive_seconds is not None:
            data.setdefault("ollama", {})["keep_alive_seconds"] = self.keep_alive_seconds
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")

        personality = REPO_ROOT / "config" / "personality.md"
        if personality.is_file():
            shutil.copy2(personality, config_dir / "personality.md")

    def ensure(self) -> str:
        """Return ``external`` when a healthy backend already exists."""
        if self.client.health():
            return "external"
        self.start()
        return "started"

    def start(self) -> None:
        if self.client.health():
            self._owns_process = False
            return
        self._prepare_data_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        out = (self.log_dir / "backend.out.log").open("ab")
        err = (self.log_dir / "backend.err.log").open("ab")
        env = os.environ.copy()
        env["PERSONAL_AGENT_DATA_DIR"] = str(self.data_dir)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x0800_0000  # CREATE_NO_WINDOW
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
        )
        self._owns_process = True
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.client.health(timeout=1.0):
                return
            if self._process.poll() is not None:
                raise BackendUnavailable(
                    "backend exited during startup; see "
                    f"{self.log_dir / 'backend.err.log'}"
                )
            time.sleep(0.3)
        self.stop()
        raise BackendUnavailable("backend did not become healthy within 90s")

    def stop(self) -> None:
        if not self._owns_process or self._process is None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
        self._owns_process = False

    def force_stop_port(self) -> None:
        """Stop a Python listener on the harness port (stale previous run)."""
        if os.name != "nt":
            return
        script = (
            f"$pids = (Get-NetTCPConnection -LocalPort {self.port} -State Listen "
            "-ErrorAction SilentlyContinue).OwningProcess; "
            "foreach ($id in $pids) { "
            "$p = Get-Process -Id $id -ErrorAction SilentlyContinue; "
            "if ($p -and $p.ProcessName -like 'python*') { Stop-Process -Id $id -Force } }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.client.health(timeout=0.5):
                return
            time.sleep(0.2)

    def restart(self, *, force: bool = False) -> None:
        if force and not self._owns_process:
            self.force_stop_port()
        self.stop()
        self.start()

    def __enter__(self) -> "BackendService":
        self.ensure()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


# --------------------------------------------------------------------------- #
# Desktop app process management
# --------------------------------------------------------------------------- #
class DesktopService:
    """Stop, rebuild, and relaunch the packaged Tauri app.

    Only used by the refinement loop with an explicit flag, because it closes
    the user's running Compa window.
    """

    def __init__(self, app_dir: Path | None = None) -> None:
        self.app_dir = app_dir or (REPO_ROOT / "build" / "Personal Agent")
        self.exe = self.app_dir / "Personal Agent.exe"

    def is_running(self) -> bool:
        if os.name != "nt":
            return False
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Personal Agent.exe", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Personal Agent.exe" in result.stdout

    def stop(self) -> None:
        if os.name != "nt":
            return
        subprocess.run(
            ["taskkill", "/IM", "Personal Agent.exe", "/F"],
            capture_output=True,
            check=False,
        )
        # The Tauri app owns the bundled backend; stop that tree as well so the
        # next launch starts clean code. Only processes inside the portable
        # folder are touched.
        script = (
            "Get-Process python -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.Path -like '{self.app_dir}\\*' }} | "
            "Stop-Process -Force"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
        )

    def launch(self, timeout: float = 90.0) -> None:
        if not self.exe.is_file():
            raise FileNotFoundError(f"packaged app not found: {self.exe}")
        subprocess.Popen([str(self.exe)], cwd=str(self.app_dir))
        client = BackendClient()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if client.health(timeout=1.0):
                return
            time.sleep(0.4)
        raise BackendUnavailable("packaged app did not expose /health in time")

    def rebuild(self, *, installer: bool = False) -> None:
        script = REPO_ROOT / "scripts" / "build_windows.ps1"
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if installer:
            args.append("-Installer")
        subprocess.run(args, cwd=str(REPO_ROOT), check=True)

    def restart(self, *, rebuild: bool = False) -> None:
        self.stop()
        if rebuild:
            self.rebuild()
        self.launch()


__all__ = [
    "BACKEND_DIR",
    "BackendClient",
    "BackendService",
    "BackendUnavailable",
    "Check",
    "DIMENSIONS",
    "DesktopService",
    "Evaluation",
    "Experiment",
    "ExperimentContext",
    "GUI_TOOLS",
    "MODIFYING_TOOLS",
    "REPO_ROOT",
    "READ_ONLY_TOOLS",
    "TEMP_ROOT",
    "TurnResult",
    "check",
    "clarification_checks",
    "contains_question",
    "event_checks",
    "file_text_checks",
    "free_gb",
    "json_safe",
    "new_run_id",
    "ollama_checks",
    "ollama_status",
    "performance_checks",
    "run_turn",
    "shorten",
    "tool_choice_checks",
    "utc_stamp",
    "warmup_ollama",
]
