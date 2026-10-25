"""Phase 17 end-to-end GUI automation test: the Outlook email scenario.

Runs against a real WinForms mock of Outlook so no real email is ever sent.
It exercises the same services the agent tools use:

  * launch the mock app through a CLI command,
  * Set-of-Mark capture and numbered element detection,
  * HITL-equivalent guarded click with highlight and smooth glide,
  * keyboard typing into To/Subject/Body,
  * the physical-input killswitch, including the halt of later GUI tools,
  * the final Send click and verification of the captured fields.

Run manually (not collected by pytest):

    backend/.venv/Scripts/python.exe backend/tests/e2e_gui_outlook.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from settings import AutomationSettings  # noqa: E402
from tools.gui_automation import GUIAutomationService  # noqa: E402
from tools.som_vision import SOMSnapshot  # noqa: E402

MOCK_SCRIPT = Path(__file__).with_name("mock_outlook.ps1")
WINDOW_TITLE = "Outlook - Mock"


class MockGrounder:
    """Deterministic stand-in for the vision model using UIA element names."""

    def __call__(
        self,
        question: str,
        snapshot: SOMSnapshot,
        image_b64: str,
    ) -> dict[str, object]:
        intent = " ".join(question.casefold().split())
        preferred_type: str | None = None
        wanted: list[str] = []

        if "new email" in intent:
            wanted = ["new email"]
            preferred_type = "Button"
        elif "subject" in intent:
            wanted = ["subject"]
            preferred_type = "Edit"
        elif "body" in intent:
            wanted = ["body"]
            preferred_type = "Edit"
        elif "to field" in intent or intent.endswith(" to"):
            wanted = ["to"]
            preferred_type = "Edit"
        elif "send" in intent:
            wanted = ["send"]
            preferred_type = "Button"
        else:
            wanted = [
                token
                for token in ("to", "subject", "body", "send", "new email")
                if token in intent
            ]

        candidates = []
        for element in snapshot.elements:
            name = " ".join(element.name.casefold().split())
            if not name:
                continue
            score = 0
            for target in wanted:
                if name == target:
                    score = max(score, 100)
                elif target in name:
                    score = max(score, 70)
                elif name in target:
                    score = max(score, 50)
            if preferred_type and element.control_type == preferred_type:
                score += 10
            if score:
                candidates.append((score, element))

        if not candidates:
            return {"id": 0, "reason": "no matching element", "confidence": 0.0}

        candidates.sort(key=lambda item: (item[0], -item[1].id), reverse=True)
        score, element = candidates[0]
        return {
            "id": element.id,
            "reason": f"matched '{element.name}' for intent '{intent}'",
            "confidence": min(0.99, score / 100),
        }


def wait_for_window(title: str, timeout: float = 15.0) -> bool:
    import pygetwindow as gw

    deadline = time.time() + timeout
    while time.time() < deadline:
        if gw.getWindowsWithTitle(title):
            return True
        time.sleep(0.25)
    return False


def element_by_name(snapshot_id: str, name: str, service: GUIAutomationService):
    snapshot = service.store.get(snapshot_id)
    if snapshot is None:
        return None
    for element in snapshot.elements:
        if " ".join(element.name.casefold().split()) == name.casefold():
            return element
    return None


def click_named(
    service: GUIAutomationService,
    question: str,
    expected_name: str,
    *,
    window_title: str = WINDOW_TITLE,
) -> dict:
    analysis = service.analyze(question, window_title=window_title)
    if not analysis.get("ok"):
        raise AssertionError(f"analyze failed: {analysis}")
    snapshot_id = analysis["snapshot_id"]
    element = element_by_name(snapshot_id, expected_name, service)
    if element is None:
        names = [
            item.get("name")
            for item in analysis.get("elements", [])
            if item.get("type") in {"Button", "Edit"}
        ]
        raise AssertionError(
            f"element '{expected_name}' not found. Visible controls: {names}"
        )
    result = service.click(
        snapshot_id,
        element.id,
        element.name,
    )
    if not result.get("ok"):
        raise AssertionError(f"click failed: {result}")
    return result


def type_and_verify(service: GUIAutomationService, text: str) -> None:
    result = service.type_text(text)
    if not result.get("ok"):
        raise AssertionError(f"typing failed: {result}")


def killswitch_phase(service: GUIAutomationService) -> None:
    print("Killswitch: arming and simulating a physical mouse twitch...")
    analysis = service.analyze(
        "Find the New Email button",
        window_title=WINDOW_TITLE,
    )
    if not analysis.get("ok") or not analysis.get("suggested_element"):
        raise AssertionError(f"killswitch analyze failed: {analysis}")
    element = analysis["suggested_element"]
    snapshot_id = analysis["snapshot_id"]

    service.guard.begin_execution()
    service.guard.simulate_physical_input(
        "Physical mouse twitch (test simulation)."
    )
    aborted = service.click(
        snapshot_id,
        int(element["id"]),
        str(element["name"]),
    )
    if not aborted.get("aborted"):
        raise AssertionError(f"killswitch did not abort the click: {aborted}")

    blocked = service.analyze(
        "Find the New Email button",
        window_title=WINDOW_TITLE,
    )
    if not blocked.get("aborted"):
        raise AssertionError(
            f"GUI tools were not halted after the abort: {blocked}"
        )

    service.reset()
    print("Killswitch: click aborted and later GUI tools were halted. PASS")


def main() -> int:
    if os.name != "nt":
        print("SKIP: this test requires Windows UI Automation.")
        return 0
    if not MOCK_SCRIPT.is_file():
        print(f"FAIL: mock script not found: {MOCK_SCRIPT}")
        return 1

    result_path = Path(
        os.environ.get("TEMP", str(Path.home()))
    ) / "compa_mock_outlook_result.json"
    if result_path.exists():
        result_path.unlink()

    env = dict(os.environ)
    env["MOCK_OUTLOOK_RESULT"] = str(result_path)
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(MOCK_SCRIPT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    settings = AutomationSettings(
        enabled=True,
        move_duration_seconds=0.45,
        highlight_seconds=0.25,
        arm_delay_seconds=0.6,
        type_interval_seconds=0.005,
        screenshot_max_width=1600,
        max_elements=80,
    )
    service = GUIAutomationService(settings, vision=None, grounder=MockGrounder())

    try:
        if not wait_for_window(WINDOW_TITLE):
            raise AssertionError("mock Outlook window did not appear")

        killswitch_phase(service)

        print("Email: opening a new message...")
        click_named(service, "Find the New Email button", "New Email")
        time.sleep(0.5)

        print("Email: filling the To field...")
        click_named(service, "Find the To field", "To")
        type_and_verify(service, "test@example.com")

        print("Email: filling the Subject field...")
        click_named(service, "Find the Subject field", "Subject")
        type_and_verify(service, "Phase 17 E2E")

        print("Email: filling the Body field...")
        click_named(service, "Find the Body field", "Body")
        type_and_verify(service, "Hello from Compa.")

        print("Email: clicking Send in the mock app...")
        click_named(service, "Find the Send button", "Send")

        deadline = time.time() + 8
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.2)
        if not result_path.exists():
            raise AssertionError(
                "mock Send did not write the result file; the click likely "
                "missed or the compose fields were not filled"
            )

        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        if "test@example.com" not in str(payload.get("to", "")):
            raise AssertionError(f"To field mismatch: {payload}")
        if "Phase 17 E2E" not in str(payload.get("subject", "")):
            raise AssertionError(f"Subject field mismatch: {payload}")
        if "Hello from Compa." not in str(payload.get("body", "")):
            raise AssertionError(f"Body field mismatch: {payload}")

        print("Email payload:", json.dumps(payload, ensure_ascii=False))
        print("PASS: Outlook mock scenario completed with the killswitch intact.")
        return 0
    except Exception as exc:  # noqa: BLE001 - test harness
        print(f"FAIL: {exc}")
        return 1
    finally:
        try:
            service.guard.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        if result_path.exists():
            try:
                result_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
