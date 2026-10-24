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

import http.client
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

DEFAULT_CONTEXT_WINDOW = 16384

PostJson = Callable[[str, bytes, float], bytes]


class NativeOllamaClient:
    """Minimal chat client that calls Ollama's native ``/api/chat`` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        num_ctx: int = DEFAULT_CONTEXT_WINDOW,
        keep_alive: int = 0,
        max_retries: int = 0,
        post_json: PostJson | None = None,
    ) -> None:
        self._endpoint = f"{_native_root(base_url)}/api/chat"
        self._timeout = timeout
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive
        self._max_retries = max_retries
        self._post_json = post_json or _CONNECTION_POOL.post
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs: Any) -> Any:
        body = json.dumps(
            self._payload(kwargs, stream=False),
            ensure_ascii=False,
        ).encode("utf-8")
        return _to_openai_shape(json.loads(self._post(body)))

    def stream_chat(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Yield native Ollama chunks from a streaming ``/api/chat`` request."""
        body = json.dumps(
            self._payload(kwargs, stream=True),
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield json.loads(line)

    def _payload(
        self,
        kwargs: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": _native_messages(kwargs["messages"]),
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": {
                "num_ctx": self._num_ctx,
                "temperature": kwargs.get("temperature", 0.7),
            },
        }
        if kwargs.get("max_tokens"):
            payload["options"]["num_predict"] = kwargs["max_tokens"]
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        return payload

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


class _ConnectionPool:
    """Reuse one keep-alive Ollama connection per worker thread.

    The agent runs blocking chat calls in Starlette's threadpool. A thread-local
    connection avoids a TCP/TLS handshake for every model or tool round without
    sharing a socket between threads.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def post(self, url: str, body: bytes, timeout: float) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Unsupported Ollama URL: {url}")

        key = (parsed.scheme, parsed.hostname, parsed.port)
        connection = getattr(self._local, "connection", None)
        connection_key = getattr(self._local, "key", None)

        if connection is None or connection_key != key:
            if connection is not None:
                connection.close()
            factory = (
                http.client.HTTPSConnection
                if parsed.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = factory(parsed.hostname, parsed.port, timeout=timeout)
            self._local.connection = connection
            self._local.key = key

        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = response.read()
        except http.client.HTTPException as exc:
            self._reset()
            raise ConnectionError(str(exc)) from exc
        except OSError:
            self._reset()
            raise

        if response.status >= 400:
            raise urllib.error.HTTPError(
                url,
                response.status,
                response.reason,
                response.headers,
                io.BytesIO(payload),
            )

        return payload

    def _reset(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._local.connection = None
        self._local.key = None

    def close(self) -> None:
        """Close the current thread's pooled connection."""
        self._reset()


_CONNECTION_POOL = _ConnectionPool()


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
