"""OpenAI-compatible client for a managed llama-server process.

The chat service already speaks the small OpenAI subset (``content`` +
``tool_calls``), so this client translates llama.cpp's SSE deltas into the
Ollama-shaped chunks the agent loop consumes, including reasoning text.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


class LlamaServerClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 180.0,
        model_label: str = "llama.cpp",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._endpoint = f"{self.base_url}/chat/completions"
        self._timeout = timeout
        self.model_label = model_label
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def supports_thinking(self, model: str) -> bool:
        # llama.cpp applies the model's Jinja template, which knows whether the
        # family supports reasoning. ``think`` becomes enable_thinking.
        return True

    # ------------------------------------------------------------------ api
    def _create(self, **kwargs: Any) -> Any:
        body = json.dumps(
            self._payload(kwargs, stream=False), ensure_ascii=False
        ).encode("utf-8")
        data = self._post(body)
        return _to_openai_shape(data, self.model_label)

    def stream_chat(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        body = json.dumps(
            self._payload(kwargs, stream=True), ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )

        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        started = time.monotonic()

        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    usage = chunk["usage"]
                timings = chunk.get("timings")
                if isinstance(timings, dict):
                    usage = {**usage, **timings}

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    thinking = delta.get("reasoning_content") or delta.get(
                        "reasoning"
                    ) or ""
                    if content:
                        yield {"message": {"role": "assistant", "content": content}}
                    if thinking:
                        yield {
                            "message": {"role": "assistant", "thinking": thinking}
                        }

                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index") or 0)
                        slot = tool_calls.setdefault(
                            index,
                            {
                                "id": call.get("id")
                                or f"call_{uuid.uuid4().hex}",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] = function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]

        if tool_calls:
            complete = []
            for index in sorted(tool_calls):
                slot = tool_calls[index]
                arguments = slot["function"].get("arguments") or "{}"
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed = {}
                complete.append(
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {
                            "name": slot["function"].get("name", ""),
                            "arguments": parsed,
                        },
                    }
                )
            yield {"message": {"role": "assistant", "content": "", "tool_calls": complete}}

        metrics: dict[str, Any] = {}
        prompt_tokens = usage.get("prompt_tokens") or usage.get("prompt_n")
        completion_tokens = usage.get("completion_tokens") or usage.get("predicted_n")
        if isinstance(prompt_tokens, int):
            metrics["prompt_eval_count"] = prompt_tokens
        if isinstance(completion_tokens, int):
            metrics["eval_count"] = completion_tokens
        prompt_ms = usage.get("prompt_ms")
        predicted_ms = usage.get("predicted_ms")
        if isinstance(prompt_ms, (int, float)):
            metrics["prompt_eval_duration"] = int(float(prompt_ms) * 1_000_000)
        if isinstance(predicted_ms, (int, float)):
            metrics["eval_duration"] = int(float(predicted_ms) * 1_000_000)
        if prompt_ms is not None or predicted_ms is not None:
            total_ms = float(prompt_ms or 0) + float(predicted_ms or 0)
            metrics["total_duration"] = int(total_ms * 1_000_000)
        metrics.setdefault(
            "total_duration",
            int((time.monotonic() - started) * 1_000_000),
        )
        yield {"done": True, **metrics}

    # -------------------------------------------------------------- payload
    def _payload(self, kwargs: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": kwargs.get("model") or self.model_label,
            "messages": _openai_messages(kwargs.get("messages") or []),
            "stream": stream,
            "temperature": kwargs.get("temperature", 0.5),
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if kwargs.get("max_tokens"):
            payload["max_tokens"] = kwargs["max_tokens"]
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
            payload["tool_choice"] = kwargs.get("tool_choice") or "auto"
            payload["parallel_tool_calls"] = True

        think = kwargs.get("think")
        if think is not None:
            payload["chat_template_kwargs"] = {
                "enable_thinking": bool(think) if isinstance(think, bool) else True
            }
        return payload

    def _post(self, body: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(
                f"llama-server returned HTTP {exc.code}: {detail}"
            ) from exc


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.get("role", "user"),
            "content": message.get("content") or "",
        }
        calls = message.get("tool_calls")
        if calls:
            item["tool_calls"] = [
                {
                    "id": call.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": call.get("function", {}).get("name", ""),
                        "arguments": json.dumps(
                            call.get("function", {}).get("arguments") or {},
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in calls
            ]
        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                item["tool_call_id"] = tool_call_id
            elif message.get("name"):
                item["tool_call_id"] = message["name"]
        converted.append(item)
    return converted


def _to_openai_shape(data: dict[str, Any], model_label: str) -> Any:
    choices = data.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    tool_calls = [
        SimpleNamespace(
            id=call.get("id") or f"call_{uuid.uuid4().hex}",
            type="function",
            function=SimpleNamespace(
                name=(call.get("function") or {}).get("name", ""),
                arguments=(call.get("function") or {}).get("arguments") or {},
            ),
        )
        for call in (message.get("tool_calls") or [])
    ]
    usage = data.get("usage") or {}
    timings = data.get("timings") or {}
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=message.get("content") or "",
                    thinking=message.get("reasoning_content")
                    or message.get("reasoning")
                    or None,
                    tool_calls=tool_calls or None,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens") or timings.get("prompt_n"),
            completion_tokens=usage.get("completion_tokens")
            or timings.get("predicted_n"),
        ),
        timings=timings,
        model=model_label,
    )
