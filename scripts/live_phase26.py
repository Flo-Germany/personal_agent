"""Phase 26 live acceptance: UIA, isolated workspace, headless SoM, cursor safety.

    backend\\.venv\\Scripts\\python.exe scripts\\live_phase26.py

Uses throwaway instances of Character Map and Explorer. Every action must report
`cursor_moved: false`; the workspace must capture real pixels and restore the
window afterwards.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from settings import AutomationSettings, ToolSettings  # noqa: E402
from tools.executor import ToolExecutor  # noqa: E402
from tools.workspace import window_rect  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    executor = ToolExecutor(ToolSettings(), automation=AutomationSettings(enabled=True))
    app = subprocess.Popen(["charmap.exe"])
    time.sleep(1.6)
    hwnd = 0
    try:
        status = executor.execute("isolated_workspace", {"action": "status"})
        check("workspace status", status.get("ok") is True, json.dumps(status.get("capabilities")))

        attached = executor.execute(
            "isolated_workspace", {"action": "attach", "window": "Character Map"}
        )
        workspace = attached.get("workspace") or {}
        hwnd = int(workspace.get("hwnd") or 0)
        check(
            "attach moves the window out of the way",
            attached.get("ok") is True and workspace.get("mode") in {"offscreen", "virtual_desktop"},
            f"mode={workspace.get('mode')}",
        )
        check(
            "attach leaves the cursor alone",
            int(workspace.get("hwnd") or 0) > 0 and "cursor" not in workspace,
        )

        captured = executor.execute(
            "isolated_workspace", {"action": "capture", "window_handle": hwnd}
        )
        check(
            "PrintWindow capture is real",
            captured.get("ok") is True and float(captured.get("ink_ratio") or 0) > 0.2,
            f"ink={captured.get('ink_ratio')} size={captured.get('image_width')}x{captured.get('image_height')}",
        )

        tree = executor.execute(
            "ui_query", {"window_handle": hwnd, "depth": 4, "max_nodes": 80}
        )
        check(
            "UIA tree is readable in the background",
            tree.get("ok") is True and int(tree.get("interactable") or 0) >= 4,
            f"{tree.get('count')} elements / {tree.get('interactable')} interactable",
        )
        check("tree reports background input mode", tree.get("input_mode") == "background")

        edits = [
            item for item in tree.get("elements", []) if "value" in (item.get("patterns") or [])
        ]
        if edits:
            edit = edits[0]
            written = executor.execute(
                "ui_action",
                {"element_id": edit["id"], "action": "set_value", "value": "Phase26"},
            )
            check(
                "ValuePattern writes without focus",
                written.get("ok") is True
                and written.get("cursor_moved") is False
                and "Phase26" in str(written.get("value")),
                f"via={written.get('via')}",
            )
        else:
            check("ValuePattern writes without focus", False, "no value-pattern element found")

        buttons = [
            item for item in tree.get("elements", []) if "invoke" in (item.get("patterns") or [])
        ]
        if buttons:
            invoked = executor.execute(
                "ui_action", {"element_id": buttons[0]["id"], "action": "invoke"}
            )
            check(
                "InvokePattern works in the background",
                invoked.get("ok") is True and invoked.get("cursor_moved") is False,
                f"{buttons[0]['name']!r} via={invoked.get('via')}",
            )

        som = executor.execute(
            "analyze_screen_ui",
            {
                "question": "the Characters to copy text field",
                "window_title": "Character Map",
                "headless": True,
                "window_handle": hwnd,
            },
        )
        check(
            "headless SoM snapshot",
            som.get("ok") is True and som.get("input_mode") == "background",
            f"detection={som.get('detection')} suggested={som.get('suggested_id')}",
        )

        snapshot_id = som.get("snapshot_id")
        if snapshot_id and som.get("suggested_id"):
            clicked = executor.execute(
                "click_element",
                {
                    "snapshot_id": snapshot_id,
                    "element_id": int(som["suggested_id"]),
                    "element_name": (som.get("suggested_element") or {}).get("name", ""),
                },
            )
            check(
                "background click posts a message",
                clicked.get("ok") is True and clicked.get("mode") == "background",
                f"cursor_moved={clicked.get('cursor_moved')}",
            )
            check("background click never moves the cursor", clicked.get("cursor_moved") is False)
            typed = executor.execute("keyboard_type_text", {"text": "OK"})
            check(
                "background typing sends characters",
                typed.get("ok") is True and typed.get("mode") == "background",
                f"{typed.get('characters')} chars via {typed.get('via')}",
            )
        else:
            check("background click posts a message", False, "no suggested element")

        before = window_rect(hwnd)
        executor.execute("isolated_workspace", {"action": "release", "window_handle": hwnd})
        restored = executor.execute("ui_query", {"window_handle": hwnd, "max_nodes": 10})
        after = window_rect(hwnd)
        check(
            "release restores the window",
            restored.get("ok") is True and after != before,
            f"{before} -> {after}",
        )
        check(
            "window is back on the user's desktop",
            bool(restored.get("window", {}).get("visible")),
        )
    finally:
        try:
            executor.execute("isolated_workspace", {"action": "release"})
        except Exception:  # noqa: BLE001
            pass
        app.terminate()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("All Phase 26 live checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
