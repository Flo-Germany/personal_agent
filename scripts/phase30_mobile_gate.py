"""Phase 30 verification server: real bridge, deterministic chat, no model.

Runs the actual FastAPI app with the LAN bridge enabled and a tiny fake chat
service so the Android gate can verify pairing, tasks and notifications without
loading a language model.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import uvicorn

from main import create_app
from settings import load_settings


class DemoChat:
    def start_turn(self, conversation_id: str, text: str):
        yield {
            "type": "status",
            "conversation_id": conversation_id,
            "message": "Working on your mobile task…",
        }
        time.sleep(0.6)
        yield {
            "type": "final",
            "conversation_id": conversation_id,
            "reply": f"Mobile task received: {text}",
        }

    def resume(self, approval_id: str, approved: bool):
        yield {
            "type": "final",
            "reply": "Approved from the Android companion."
            if approved
            else "Denied from the Android companion.",
        }

    def interrupt(self, conversation_id: str) -> bool:
        return True


def main() -> int:
    settings = load_settings()
    settings.bridge.enabled = True
    settings.bridge.host = "0.0.0.0"
    settings.bridge.port = 8765
    settings.bridge.token_file = Path("phase30-token.txt")
    app = create_app(settings=settings, chat_service=DemoChat())
    bridge = app.state.bridge
    if bridge is None:
        print("FAIL: bridge was not created")
        return 1
    print("Phase 30 gate server")
    print(f"  token: {bridge.token()}")
    print("  local:  http://127.0.0.1:8020")
    print("  bridge: ws://0.0.0.0:8765/ws")
    uvicorn.run(app, host="127.0.0.1", port=8020, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
