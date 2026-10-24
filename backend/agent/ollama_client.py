"""Native Ollama chat client exposed through an OpenAI-shaped interface.

Ollama's OpenAI-compatible endpoint does not support setting ``num_ctx`` (see
ollama/ollama#16814), so it silently truncates long prompts to the model's
default context window. Truncation drops the beginning of the system prompt,
including the agent's identity and the knowledge base. The native ``/api/chat``
endpoint accepts ``options.num_ctx``, so the agent uses that instead.

The response is converted to the small subset of the OpenAI response shape the
agent loop already consumes: ``choices[0].message.content`` and
``choices[0].message.tool_calls``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

DEFAULT_CONTEXT_WINDOW = 8192

PostJson = Callable[[str, bytes, float], bytes]


class NativeOllamaClient:
    """Minimal chat client that calls Ollama's native ``/api/chat`` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        num_ctx: int = DEFAULT_CONTEXT_WINDOW,
        max_retries: int = 0,
        post_json: PostJson | None = None,
    ) -> None:
        self._endpoint = f"{_native_root(base_url)}/api/chat"
        self._timeout = timeout
        self._num_ctx = num_ctx
        self._max_retries = max_retries
        self._post_json = post_json or _post_json
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs: Any) -> Any:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": _native_messages(kwargs["messages"]),
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                "temperature": kwargs.get("temperature", 0.7),
            },
        }
        if kwargs.get("max_tokens"):
            payload["options"]["num_predict"] = kwargs["max_tokens"]
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return _to_openai_shape(json.loads(self._post(body)))

    def _post(self, body: bytes) -> bytes:
        last_error: Exception | None = None

        for _ in range(self._max_retries + 1):
            try:
                return self._post_json(self._endpoint, body, self._timeout)
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
                last_error = exc

        if last_error is not None:
            raise last_error
        raise RuntimeError("Ollama request was not attempted.")


def _post_json(url: str, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _native_root(base_url: str) -> str:
    """Turn ``http://host:port/v1`` into ``http://host:port``."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root.rstrip("/")


def _native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stored (OpenAI-shaped) history into Ollama's native message form.

    OpenAI stores tool-call arguments as a JSON string, while Ollama's native
    endpoint expects an object. Supplying the string makes Ollama reject the
    follow-up turn with HTTP 400.
    """
    converted: list[dict[str, Any]] = []

    for message in messages:
        item: dict[str, Any] = {
            "role": message.get("role", "user"),
            "content": message.get("content") or "",
        }

        tool_calls = message.get("tool_calls")
        if tool_calls:
            item["tool_calls"] = [
                {
                    "function": {
                        "name": call.get("function", {}).get("name", ""),
                        "arguments": _parse_arguments(
                            call.get("function", {}).get("arguments", {})
                        ),
                    }
                }
                for call in tool_calls
            ]

        if message.get("role") == "tool":
            tool_name = message.get("tool_name") or message.get("name")
            if tool_name:
                item["tool_name"] = tool_name

        converted.append(item)

    return converted


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_openai_shape(data: dict[str, Any]) -> Any:
    message = data.get("message") or {}
    tool_calls = [
        SimpleNamespace(
            id=f"call_{uuid.uuid4().hex}",
            type="function",
            function=SimpleNamespace(
                name=(call.get("function") or {}).get("name", ""),
                arguments=(call.get("function") or {}).get("arguments", {}),
            ),
        )
        for call in (message.get("tool_calls") or [])
    ]

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=message.get("content") or "",
                    tool_calls=tool_calls or None,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        ),
    )
