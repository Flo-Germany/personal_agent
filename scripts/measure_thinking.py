"""Measure the hidden thinking phase of a local Ollama model.

The chat UI shows a reasoning phase as a collapsible "Thought for N s" button.
This script answers the related question: what does that phase actually cost on
this machine, for this model, with this thinking budget?

It streams ``/api/chat`` exactly like the backend does, records when the first
and last reasoning tokens arrive, when the first answer token arrives, and the
final token counts, and writes one JSON line per run to
``build/thinking_report.jsonl``.

Usage (from the repository root):

    backend\\.venv\\Scripts\\python.exe scripts\\measure_thinking.py --list-models
    backend\\.venv\\Scripts\\python.exe scripts\\measure_thinking.py ^
        --model qwen3-vl:8b-instruct --think none ^
        --model qwen3-vl:8b --think low

The script never changes ``config/settings.json``; it talks to the Ollama API
directly, runs one model at a time, and unloads each model between
configurations so two chat models never compete for VRAM.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"

PROMPTS = {
    "simple": "What is 17 * 23? Answer with the number only.",
    "reasoning": (
        "A folder has 3 subfolders, each with 4 files. Two files per subfolder "
        "are images. How many non-image files are there? Answer with the number "
        "and one short sentence."
    ),
    "agentic": (
        "The user says: 'move the newest report in Downloads into an Archive "
        "folder on the Desktop'. List the tool calls you would make, in order."
    ),
}


def request(path: str, payload: dict, timeout: float):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def list_models() -> int:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI
        print(f"Ollama is not reachable at {OLLAMA}: {exc}", file=sys.stderr)
        return 1

    for item in data.get("models") or []:
        name = item.get("model") or item.get("name")
        caps = item.get("capabilities") or []
        marker = "thinking" if "thinking" in caps else "        "
        print(f"{marker}  {name:32} {', '.join(caps)}")
    return 0


def unload(model: str) -> None:
    try:
        request(
            "/api/chat",
            {"model": model, "messages": [], "keep_alive": 0, "stream": False},
            timeout=20,
        ).read()
    except Exception:  # noqa: BLE001 - best effort
        pass
    time.sleep(2)


def run_once(
    model: str,
    think: bool | str | None,
    prompt: str,
    *,
    num_ctx: int,
    num_predict: int,
    keep_alive: int,
    timeout: float,
) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "keep_alive": keep_alive,
        "options": {
            "num_ctx": num_ctx,
            "temperature": 0.0,
            "num_predict": num_predict,
        },
    }
    if think is not None:
        payload["think"] = think

    started = time.monotonic()
    first_thinking = last_thinking = first_content = None
    thinking_chars = answer_chars = thinking_tokens = 0
    answer: list[str] = []
    final: dict = {}

    try:
        with request("/api/chat", payload, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                chunk = json.loads(line)
                now = time.monotonic() - started
                message = chunk.get("message") or {}
                thinking = message.get("thinking") or ""
                content = message.get("content") or ""

                if thinking:
                    thinking_tokens += 1
                    thinking_chars += len(thinking)
                    first_thinking = first_thinking or now
                    last_thinking = now
                if content:
                    answer_chars += len(content)
                    answer.append(content)
                    first_content = first_content or now
                if chunk.get("done"):
                    final = chunk
                    break
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": str(exc), "model": model, "think": think}

    total = time.monotonic() - started
    eval_ns = final.get("eval_duration") or 0
    eval_count = final.get("eval_count") or 0
    return {
        "model": model,
        "think": think,
        "prompt": prompt[:60],
        "first_thinking_s": round(first_thinking, 2) if first_thinking else None,
        "thinking_s": round(last_thinking - first_thinking, 2)
        if first_thinking and last_thinking
        else None,
        "first_answer_s": round(first_content, 2) if first_content else None,
        "total_s": round(total, 2),
        "thinking_chars": thinking_chars,
        "thinking_tokens": thinking_tokens,
        "answer_chars": answer_chars,
        "prompt_tokens": final.get("prompt_eval_count"),
        "eval_tokens": eval_count,
        "eval_tok_s": round(eval_count / (eval_ns / 1e9), 1) if eval_ns else None,
        "answer": "".join(answer)[:400],
    }


def parse_think(value: str) -> bool | str | None:
    lowered = value.strip().lower()
    if lowered in {"none", "off", "omit"}:
        return None
    if lowered in {"auto", "true", "on"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"low", "medium", "high"}:
        return lowered
    raise argparse.ArgumentTypeError(
        f"Unsupported thinking value: {value!r} (use none, off, auto, true, "
        f"false, low, medium, or high)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Ollama thinking latency and throughput. Results are "
            "appended to build/thinking_report.jsonl."
        )
    )
    parser.add_argument(
        "--model",
        action="append",
        help="Model to measure; repeat to compare several.",
    )
    parser.add_argument(
        "--think",
        action="append",
        type=parse_think,
        help=(
            "Thinking value for the preceding/first model run: none, off, "
            "auto, true, false, low, medium, high. Default: none."
        ),
    )
    parser.add_argument("--case", action="append", choices=sorted(PROMPTS))
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--num-predict", type=int, default=512)
    parser.add_argument("--keep-alive", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="build/thinking_report.jsonl")
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List installed models and their capabilities, then exit.",
    )
    args = parser.parse_args()

    if args.list_models:
        return list_models()

    models = args.model or ["qwen3-vl:8b-instruct"]
    thinks = args.think or [None]
    cases = args.case or list(PROMPTS)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Measuring {len(models)} model(s) x {len(thinks)} thinking mode(s) "
        f"x {len(cases)} prompt(s)",
        flush=True,
    )
    with out.open("a", encoding="utf-8") as handle:
        for model in models:
            for think in thinks:
                for name in cases:
                    row = run_once(
                        model,
                        think,
                        PROMPTS[name],
                        num_ctx=args.num_ctx,
                        num_predict=args.num_predict,
                        keep_alive=args.keep_alive,
                        timeout=args.timeout,
                    )
                    row["case"] = name
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
                    if "error" in row:
                        print(f"  {model} think={think} {name}: {row['error']}")
                        continue
                    print(
                        f"  {model:24} think={str(think):7} {name:9} "
                        f"thinking={row['thinking_s']}s "
                        f"answer_at={row['first_answer_s']}s "
                        f"total={row['total_s']}s "
                        f"thinking_chars={row['thinking_chars']} "
                        f"tok/s={row['eval_tok_s']}",
                        flush=True,
                    )
            unload(model)

    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
