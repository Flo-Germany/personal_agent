"""Opt-in live acceptance tests against real Windows applications.

This script drives the real agent (real local model, real approvals, real
programs). It is deliberately not named ``test_*.py`` so pytest does not run it
unattended.

Safety rules:
  * Send/Delete/Format actions are always denied by the harness.
  * Only windows/processes created by the harness are closed afterwards.
  * Outlook drafts are discarded through the Outlook object model, never sent.

Usage:
    backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario outlook
    backend/.venv/Scripts/python.exe backend/tests/live_real_apps.py --scenario all
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - older consoles
        pass

import psutil  # noqa: E402
import pygetwindow as gw  # noqa: E402

from agent.chat import ChatService  # noqa: E402
from memory.knowledge import KnowledgeBase  # noqa: E402
from memory.store import ConversationStore  # noqa: E402
from settings import load_settings  # noqa: E402
from tools.executor import ToolExecutor  # noqa: E402
from tools.vision_tools import VisionService  # noqa: E402

STAMP = time.strftime("%Y%m%d-%H%M%S")
TEMP = Path(tempfile.gettempdir())


def title_exists(title: str) -> bool:
    wanted = title.casefold()
    return any(
        (window.title or "").strip().casefold() == wanted
        and bool(getattr(window, "visible", True))
        for window in gw.getAllWindows()
    )


def wait_for_title(needle: str, timeout: float = 20.0) -> str | None:
    folded = needle.casefold()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in gw.getAllWindows():
            title = (window.title or "").strip()
            if title and folded in title.casefold() and getattr(window, "visible", True):
                return title
        time.sleep(0.2)
    return None


def close_windows_matching(needle: str, *, require: str | None = None) -> None:
    folded = needle.casefold()
    required = require.casefold() if require else None
    for window in gw.getAllWindows():
        title = (window.title or "").strip()
        if (
            title
            and folded in title.casefold()
            and (required is None or required in title.casefold())
            and getattr(window, "visible", True)
        ):
            try:
                window.close()
            except Exception:  # noqa: BLE001
                pass


class LiveRunner:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.store = ConversationStore(
            max_conversations=self.settings.memory.max_conversations
        )
        self.knowledge = KnowledgeBase(
            self.settings.memory.resolved_knowledge_path()
        )
        self.vision = VisionService(
            self.settings.vision,
            ollama_base_url=self.settings.ollama.base_url,
            keep_alive_seconds=self.settings.ollama.keep_alive_seconds,
            context_window=self.settings.ollama.context_window,
        )
        self.service = ChatService(
            self.settings,
            self.store,
            self.knowledge,
            ToolExecutor(
                self.settings.tools,
                vision=self.vision,
                automation=self.settings.automation,
            ),
        )

    @staticmethod
    def risky(action: dict[str, Any]) -> str | None:
        tool = str(action.get("tool") or "")
        arguments = action.get("arguments") or {}
        if tool == "click_element":
            name = str(arguments.get("element_name") or "").casefold()
            for word in ("send", "senden", "delete", "löschen", "remove"):
                if word in name:
                    return f"blocked click on {name!r}"
        if tool == "keyboard_press_keys":
            keys = [str(key).casefold() for key in arguments.get("keys") or []]
            if "enter" in keys and ({"ctrl", "alt"} & set(keys)):
                return "blocked Ctrl/Alt+Enter (send shortcut)"
        if tool == "run_powershell_action":
            command = str(arguments.get("command") or "").casefold()
            for word in ("remove-item", "rm ", "del ", "format-volume", "stop-computer"):
                if word in command:
                    return f"blocked command containing {word!r}"
        return None

    def run(
        self,
        prompt: str,
        *,
        timeout: float = 240.0,
        max_actions: int = 24,
        deny_tools: set[str] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        conversation_id = conversation_id or self.store.new_conversation_id()
        summary: dict[str, Any] = {
            "conversation_id": conversation_id,
            "tool_calls": [],
            "approvals": 0,
            "denied": [],
            "final": None,
            "error": None,
            "timed_out": False,
        }
        stopped = threading.Event()

        def watchdog() -> None:
            time.sleep(timeout)
            if not stopped.is_set():
                summary["timed_out"] = True
                self.service.interrupt(conversation_id)

        threading.Thread(target=watchdog, daemon=True).start()
        started = time.monotonic()

        events = self.service.start_turn(conversation_id, prompt)
        while True:
            pending: dict[str, Any] | None = None
            finished = False
            for event in events:
                kind = event.get("type")
                elapsed = time.perf_counter() - started
                if kind == "status":
                    message = str(event.get("message") or "")
                    if event.get("phase") in {"tool_started", "generating"}:
                        print(f"  [{elapsed:6.1f}s] {event.get('phase')}: {message}")
                elif kind == "tool_result":
                    result = event.get("result") or {}
                    entry = {
                        "tool": event.get("tool"),
                        "ok": bool(event.get("ok")),
                        "summary": str(event.get("summary") or "")[:300],
                        "outcome": result.get("outcome"),
                        "loop": bool(result.get("loop_detected")),
                        "window": result.get("window_title"),
                    }
                    summary["tool_calls"].append(entry)
                    print(
                        f"  [{elapsed:6.1f}s] tool {entry['tool']} ok={entry['ok']} "
                        f"window={entry['window']!r} loop={entry['loop']}"
                    )
                    print(f"      {entry['summary']}")
                    outcome = entry["outcome"] or {}
                    if outcome:
                        print(
                            "      outcome:"
                            f" opened={[w.get('title') for w in outcome.get('window_opened') or []]}"
                            f" title={outcome.get('title_changed')}"
                            f" changed={outcome.get('ui_changed')}"
                        )
                elif kind == "action_required":
                    pending = event
                    print(f"  [{elapsed:6.1f}s] APPROVAL: {event.get('actions')}")
                elif kind == "display_required":
                    pending = event
                    print(f"  [{elapsed:6.1f}s] DISPLAY REQUIRED")
                elif kind == "final":
                    summary["final"] = event.get("reply")
                    print(f"  [{elapsed:6.1f}s] FINAL: {summary['final']}")
                    finished = True
                elif kind == "error":
                    summary["error"] = event
                    print(f"  [{elapsed:6.1f}s] ERROR: {event.get('message')}")
                    finished = True
                elif kind == "cancelled":
                    summary["error"] = {"message": "cancelled"}
                    print(f"  [{elapsed:6.1f}s] CANCELLED")
                    finished = True

            if finished or pending is None:
                break

            if summary["approvals"] >= max_actions:
                summary["error"] = {"message": "approval limit reached"}
                self.service.interrupt(conversation_id)
                break

            summary["approvals"] += 1
            if pending.get("type") == "display_required":
                print("      cancelling unexpected display choice")
                events = self.service.resume_display(pending["approval_id"], None)
                continue

            calls = pending.get("actions") or []
            blocked = next(
                (
                    reason
                    for reason in (self.risky(call) for call in calls)
                    if reason
                ),
                None,
            )
            if not blocked and deny_tools:
                blocked = next(
                    (
                        f"tool {call.get('tool')} is not allowed in this scenario"
                        for call in calls
                        if str(call.get("tool") or "") in deny_tools
                    ),
                    None,
                )
            if blocked:
                summary["denied"].append(blocked)
                print(f"      DENY: {blocked}")
                events = self.service.resume(pending["approval_id"], False)
                continue

            print("      APPROVE")
            events = self.service.resume(pending["approval_id"], True)

        stopped.set()
        summary["elapsed"] = round(time.perf_counter() - started, 2)
        return summary


def outlook_test(runner: LiveRunner) -> bool:
    print("\n=== OUTLOOK: one compose window, filled, never sent")
    prompt = (
        "Open Microsoft Outlook. Create exactly one new email and fill it in: "
        "To = compa.test@example.com, Subject = 'Compa live test "
        f"{STAMP}', Body = 'This is an automated UI test. Do not send.' "
        "Do NOT send the email and do NOT create a second draft. Use the "
        "compose window that opens; when the fields are filled, stop and tell "
        "me the compose window title. If a compose window is already open, use "
        "it instead of opening another."
    )
    result = runner.run(prompt)
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str)[:4000])

    opened = [
        entry
        for entry in result["tool_calls"]
        if any(
            (window.get("title") or "").startswith("Untitled")
            or "message (" in (window.get("title") or "").casefold()
            for window in (entry.get("outcome") or {}).get("window_opened") or []
        )
    ]

    # Read and discard any compose window through COM, never send.
    script = TEMP / "compa_close_draft.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$ol = New-Object -ComObject Outlook.Application\n"
        "$items = @($ol.Inspectors)\n"
        "Write-Output (\"inspectors=\" + $items.Count)\n"
        "foreach ($i in $items) {\n"
        "  try {\n"
        "    $item = $i.CurrentItem\n"
        "    Write-Output (\"subject=\" + $item.Subject)\n"
        "    Write-Output (\"to=\" + $item.To)\n"
        "    Write-Output (\"body=\" + $item.Body)\n"
        "    $i.Close(1)\n"
        "  } catch { Write-Output (\"close failed: \" + $_) }\n"
        "}\n",
        encoding="utf-8",
    )
    inspection = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(inspection.stdout)
    if inspection.stderr.strip():
        print("com stderr:", inspection.stderr[:500])

    filled = (
        f"subject=Compa live test {STAMP}" in inspection.stdout
        and "to=compa.test@example.com" in inspection.stdout
        and "Do not send." in inspection.stdout
    )
    send_attempt = any("send" in reason.lower() for reason in result["denied"])
    loops = [entry for entry in result["tool_calls"] if entry.get("loop")]
    one_compose = len(opened) == 1
    gui_used = any(
        str(entry.get("tool") or "").startswith(("click_element", "keyboard_"))
        for entry in result["tool_calls"]
    )

    print(
        f"  verify: filled={filled} one_compose={one_compose} "
        f"send_attempt={send_attempt} loops={len(loops)} gui_used={gui_used}"
    )
    return (
        filled
        and one_compose
        and not send_attempt
        and not loops
        and gui_used
        and result["final"] is not None
    )


def browser_test(runner: LiveRunner) -> bool:
    print("\n=== BROWSER: isolated Chrome profile, navigate and read")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        print("  Chrome not found, skipped")
        return False

    start_page = TEMP / f"compa_browser_start_{STAMP}.html"
    target_page = TEMP / f"compa_browser_target_{STAMP}.html"
    heading = f"Compa Browser Target {STAMP}"
    start_page.write_text(
        "<!doctype html><html><head><title>Compa Browser Start</title></head>"
        "<body><h1>Compa Browser Start</h1><p>Navigate to the target page.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    target_page.write_text(
        "<!doctype html><html><head><title>Compa Browser Target</title></head>"
        f"<body><h1>{heading}</h1><p>Local test page.</p></body></html>",
        encoding="utf-8",
    )
    profile = Path(tempfile.mkdtemp(prefix="compa_chrome_"))
    process = subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--new-window",
            start_page.as_uri(),
        ]
    )
    title = wait_for_title("Compa Browser Start", timeout=25)
    print("  chrome window:", title)
    if not title:
        print("  no Chrome window, skipped")
        return False

    prompt = (
        f"A Chrome window is already open (title: {title!r}). Do not open "
        "another Chrome window, do not use PowerShell, and do not click page "
        "links. In that "
        "window, press Ctrl+L, type the URL "
        f"file:///{target_page.as_posix()} and press Enter. Wait for the page, "
        "then read the page text with read_window_content and tell me the exact "
        "H1 heading (it includes a timestamp). Do not answer from the window "
        "title."
    )
    result = runner.run(
        prompt,
        timeout=180,
        deny_tools={"open_file"},
    )
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str)[:3000])
    final = (result.get("final") or "").casefold()
    tools = {str(entry.get("tool")) for entry in result["tool_calls"]}
    used_cli = bool(
        tools
        & {
            "open_file",
            "run_powershell_query",
            "run_powershell_action",
            "read_file_content",
        }
    )
    ok = heading.casefold() in final and not result["denied"] and not used_cli
    print(f"  verify: heading_in_reply={heading.casefold() in final} used_cli={used_cli}")

    # Stop only the isolated Chrome instance.
    try:
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except Exception:  # noqa: BLE001
                pass
        parent.terminate()
    except Exception as exc:  # noqa: BLE001
        print("  chrome cleanup:", exc)
    return ok


def explorer_test(runner: LiveRunner) -> bool:
    print("\n=== EXPLORER: real window, address bar navigation")
    folder = TEMP / f"compa_explorer_{STAMP}"
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", f"/n,{folder}"])
    title = wait_for_title(folder.name, timeout=20)
    print("  explorer window:", title)
    if not title:
        print("  no Explorer window, skipped")
        return False

    prompt = (
        f"In the open File Explorer window titled {title!r}, navigate to the "
        f"folder {BACKEND.parent} using Ctrl+L and Enter. Do not use "
        "PowerShell, start_program, or open_file, and do not create or delete "
        "anything. When the window title has changed, reply with the single "
        "word: navigated."
    )
    result = runner.run(
        prompt,
        timeout=180,
        deny_tools={"open_file"},
    )
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str)[:3000])
    navigated = any(
        "personal_agent" in (window.title or "").casefold()
        and "file explorer" in (window.title or "").casefold()
        for window in gw.getAllWindows()
    )
    print(f"  verify: window_title_is_personal_agent={navigated}")

    read = runner.run(
        "What is the exact folder name shown in the File Explorer window title? "
        "Answer with just the folder name.",
        timeout=120,
        conversation_id=result["conversation_id"],
    )
    print(json.dumps(read, ensure_ascii=False, indent=1, default=str)[:2000])
    final = (read.get("final") or "").casefold()
    tools = {
        str(entry.get("tool"))
        for entry in result["tool_calls"] + read["tool_calls"]
    }
    used_cli = bool(
        tools
        & {
            "open_file",
            "run_powershell_query",
            "run_powershell_action",
            "read_file_content",
        }
    )
    answered = "personal_agent" in final
    ok = (
        navigated
        and answered
        and not result["denied"]
        and not read["denied"]
        and not used_cli
    )
    print(f"  verify: navigated={navigated} answered={answered} used_cli={used_cli}")
    close_windows_matching("personal_agent", require="file explorer")
    close_windows_matching(folder.name, require="file explorer")
    return ok


SCENARIOS: dict[str, Callable[[LiveRunner], bool]] = {
    "outlook": outlook_test,
    "browser": browser_test,
    "explorer": explorer_test,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        default="outlook",
        choices=[*SCENARIOS, "all"],
    )
    args = parser.parse_args()

    runner = LiveRunner()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results: dict[str, bool] = {}
    for name in names:
        try:
            results[name] = SCENARIOS[name](runner)
        except Exception as exc:  # noqa: BLE001
            print(f"{name} failed with exception: {exc}")
            results[name] = False
        time.sleep(1.0)

    print("\nSummary")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
