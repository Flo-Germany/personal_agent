"""Editable settings and knowledge endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app
from memory.store import ConversationStore
from settings import MemorySettings, Settings


class _NoopChat:
    def start_turn(self, *args, **kwargs):
        return iter(())

    def resume(self, *args, **kwargs):
        return iter(())

    def resume_display(self, *args, **kwargs):
        return iter(())

    def get_pending(self, approval_id):
        raise KeyError(approval_id)

    def interrupt(self, conversation_id):
        return True

    def discard_pending(self, conversation_id):
        return False

    def learn_from_exchange(self, user_message):
        return None


def _client(tmp_path: Path, settings: Settings, config: Path) -> TestClient:
    store = ConversationStore(max_conversations=5)
    app = create_app(
        settings=settings,
        store=store,
        chat_service=_NoopChat(),  # type: ignore[arg-type]
        config_path=config,
    )
    return TestClient(app)


def test_editable_settings_roundtrip_and_persistence(tmp_path: Path) -> None:
    settings = Settings(
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md")
    )
    config = tmp_path / "config" / "settings.json"
    with _client(tmp_path, settings, config) as client:
        current = client.get("/settings/editable")
        assert current.status_code == 200
        snapshot = current.json()["settings"]
        assert snapshot["language"] in {"auto", "de", "en", "zh"}
        assert snapshot["config_path"] == str(config)

        response = client.patch(
            "/settings/editable",
            json={
                "agent_name": "Nova",
                "language": "de",
                "communication_style": "concise",
                "autonomy": "cautious",
                "uia_depth": 7,
                "vision_max_width": 1280,
                "allow_foreground_fallback": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["restart_required"] is False
        assert payload["settings"]["agent_name"] == "Nova"

        assert settings.agent_name == "Nova"
        assert settings.agent.language == "de"
        assert settings.agent.communication_style == "concise"
        assert settings.agent.autonomy == "cautious"
        assert settings.automation.max_element_depth == 7
        assert settings.automation.screenshot_max_width == 1280
        assert settings.vision.screen_max_width == 1280
        assert settings.automation.allow_foreground_fallback is False

        saved = json.loads(config.read_text(encoding="utf-8"))
        assert saved["agent_name"] == "Nova"
        assert saved["agent"]["language"] == "de"

        engine_change = client.patch(
            "/settings/editable", json={"context_window": 32768}
        )
        assert engine_change.status_code == 200
        assert engine_change.json()["restart_required"] is True
        assert settings.engine.context_window == 32768
        assert settings.ollama.context_window == 32768

        # An explicit null clears an automatic override instead of being
        # silently dropped like an absent field.
        pinned = client.patch("/settings/editable", json={"gpu_layers": 12})
        assert pinned.json()["settings"]["gpu_layers"] == 12
        cleared = client.patch("/settings/editable", json={"gpu_layers": None})
        assert cleared.status_code == 200
        assert cleared.json()["settings"]["gpu_layers"] is None
        assert settings.engine.gpu_layers is None

        invalid = client.patch("/settings/editable", json={"uia_depth": 1})
        assert invalid.status_code == 422


def test_knowledge_read_write_and_reset(tmp_path: Path) -> None:
    settings = Settings(
        memory=MemorySettings(knowledge_base_path=tmp_path / "knowledge.md")
    )
    store = ConversationStore(max_conversations=5)
    app = create_app(
        settings=settings,
        store=store,
        chat_service=_NoopChat(),  # type: ignore[arg-type]
        config_path=tmp_path / "settings.json",
    )
    with TestClient(app) as client:
        initial = client.get("/knowledge")
        assert initial.status_code == 200
        assert isinstance(initial.json()["content"], str)

        saved = client.put(
            "/knowledge",
            json={"content": "# User\n- Name: Flo\n"},
        )
        assert saved.status_code == 200
        assert "Name: Flo" in client.get("/knowledge").json()["content"]

        cleared = client.delete("/knowledge")
        assert cleared.status_code == 200
        assert "Name: Flo" not in cleared.json()["content"]
        backups = list((tmp_path).glob("knowledge.md.bak"))
        assert backups, "a backup should exist before overwriting"
