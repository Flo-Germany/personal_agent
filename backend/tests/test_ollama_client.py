"""Tests for the native Ollama chat client and its response adaptation."""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from agent.ollama_client import NativeOllamaClient, _native_root


def test_native_client_sends_num_ctx_and_reads_text() -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, body: bytes, timeout: float) -> bytes:
        calls.append({"url": url, "payload": json.loads(body), "timeout": timeout})
        return json.dumps(
            {"message": {"role": "assistant", "content": "Compa."}}
        ).encode("utf-8")

    client = NativeOllamaClient(
        "http://127.0.0.1:11434/v1",
        timeout=42.0,
        num_ctx=16384,
        post_json=fake_post,
    )

    completion = client.chat.completions.create(
        model="hermes3",
        messages=[{"role": "user", "content": "What is your name?"}],
        tools=[{"type": "function", "function": {"name": "noop"}}],
        temperature=0.2,
        max_tokens=400,
    )

    call = calls[0]
    assert call["url"] == "http://127.0.0.1:11434/api/chat"
    assert call["timeout"] == 42.0
    assert call["payload"]["model"] == "hermes3"
    assert call["payload"]["stream"] is False
    assert call["payload"]["options"] == {
        "num_ctx": 16384,
        "temperature": 0.2,
        "num_predict": 400,
    }
    assert call["payload"]["tools"][0]["function"]["name"] == "noop"
    assert completion.choices[0].message.content == "Compa."
    assert completion.choices[0].message.tool_calls is None


def test_native_client_converts_tool_calls() -> None:
    def fake_post(url: str, body: bytes, timeout: float) -> bytes:
        return json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "get_open_windows",
                                "arguments": {"include_titles": True},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 120,
                "eval_count": 8,
            }
        ).encode("utf-8")

    client = NativeOllamaClient("http://localhost:11434", post_json=fake_post)
    completion = client.chat.completions.create(
        model="hermes3",
        messages=[{"role": "user", "content": "List my windows."}],
    )

    call = completion.choices[0].message.tool_calls[0]
    assert call.id.startswith("call_")
    assert call.type == "function"
    assert call.function.name == "get_open_windows"
    assert call.function.arguments == {"include_titles": True}
    assert completion.usage.prompt_tokens == 120
    assert completion.usage.completion_tokens == 8


def test_native_client_converts_tool_history_arguments() -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, body: bytes, timeout: float) -> bytes:
        captured.update(json.loads(body))
        return json.dumps({"message": {"content": "ok"}}).encode("utf-8")

    client = NativeOllamaClient("http://localhost:11434/v1", post_json=fake_post)
    client.chat.completions.create(
        model="hermes3",
        messages=[
            {"role": "user", "content": "List my windows."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_open_windows",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"ok": true}',
                "tool_call_id": "call_1",
                "name": "get_open_windows",
            },
        ],
    )

    messages = captured["messages"]
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == {}
    assert messages[2] == {
        "role": "tool",
        "content": '{"ok": true}',
        "tool_name": "get_open_windows",
    }


def test_native_client_retries_connection_errors() -> None:
    attempts = 0

    def flaky_post(url: str, body: bytes, timeout: float) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("connection refused")
        return json.dumps({"message": {"content": "ok"}}).encode("utf-8")

    client = NativeOllamaClient(
        "http://127.0.0.1:11434/v1",
        max_retries=1,
        post_json=flaky_post,
    )
    completion = client.chat.completions.create(
        model="hermes3",
        messages=[{"role": "user", "content": "Hi"}],
    )

    assert attempts == 2
    assert completion.choices[0].message.content == "ok"


def test_native_client_does_not_retry_http_errors() -> None:
    attempts = 0

    def missing_post(url: str, body: bytes, timeout: float) -> bytes:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    client = NativeOllamaClient(
        "http://127.0.0.1:11434/v1",
        max_retries=3,
        post_json=missing_post,
    )

    with pytest.raises(urllib.error.HTTPError):
        client.chat.completions.create(
            model="missing",
            messages=[{"role": "user", "content": "Hi"}],
        )

    assert attempts == 1


def test_native_root_strips_the_openai_suffix() -> None:
    assert _native_root("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert _native_root("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434"
    assert _native_root("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
