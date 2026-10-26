"""Phase 22 stress test: download, hot-swap, VRAM flush, CLI parity.

Run this against a backend whose ``engine.type`` is ``llamacpp``:

    python scripts/stress_engine.py --base-url http://127.0.0.1:8001

It installs a small GGUF through the public model API, switches the active text
model, verifies the server reports layers on the GPU, forces two additional
hot swaps, and runs a real CLI task through ``/chat/stream`` (approving the
file write). Every step is printed and the exit code is non-zero on failure.

Vision/GUI parity is exercised separately by ``tests/live_real_apps.py``; this
script focuses on the text engine, downloads, and VRAM flushing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _request(base: str, path: str, payload: dict | None = None, method: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read()
    return json.loads(body) if body else {}


def _sse(base: str, path: str, payload: dict) -> list[dict]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    continue
    return events


def _stream_progress(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=900) as response:
        last: dict = {}
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                last = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if last.get("state") in {"downloading", "queued"}:
                print(
                    f"  {last.get('state')}: {last.get('percent', 0):5.1f}% "
                    f"{last.get('downloaded', 0) / 1e6:7.1f} MB",
                    flush=True,
                )
        return last


def step(title: str) -> None:
    print(f"\n== {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", default="qwen3-0.6b-q4")
    parser.add_argument("--keep-model", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())
        if not ok:
            failures.append(name)

    step("Engine status")
    try:
        status = _request(base, "/engine/status")
    except Exception as exc:  # noqa: BLE001 - CLI
        print(f"Backend unreachable: {exc}")
        return 2
    check("engine is llamacpp", status.get("engine") == "llamacpp", str(status.get("engine")))
    hardware = status.get("hardware") or {}
    check("hardware profiled", bool(hardware.get("gpu_name")), hardware.get("gpu_name", ""))

    step("Runtime")
    if not (status.get("runtime") or {}).get("installed"):
        job = _request(base, "/engine/runtime/install", {})
        final = _stream_progress(base, f"/engine/runtime/progress/{job['id']}")
        check("runtime installed", final.get("state") == "done", final.get("error", ""))
    else:
        check("runtime already installed", True, status["runtime"].get("version", ""))

    step("Model install")
    catalog = _request(base, "/models/catalog").get("models") or []
    entry = next((item for item in catalog if item["id"] == args.model), None)
    if entry is None:
        print(f"Unknown model id {args.model}")
        return 2
    if not entry.get("installed"):
        job = _request(base, "/models/install", {"model_id": args.model})
        final = _stream_progress(base, f"/models/progress/{job['id']}")
        check(
            "model installed and checksum verified",
            final.get("state") == "done",
            final.get("error", ""),
        )
    else:
        check("model already installed", True, entry["name"])

    previous = status.get("active", {}).get("text")
    step("Hot swap to the test model")
    swapped = _request(
        base, "/models/active", {"task": "text", "model_id": args.model}
    )
    check("vram flushed before start", swapped.get("vram_flushed") is True)
    check(
        "gpu layers planned",
        int(swapped.get("n_gpu_layers") or 0) > 0,
        f"{swapped.get('n_gpu_layers')} layers",
    )

    step("CLI parity after the swap")
    workspace = Path.home() / "AppData/Local/Temp" / f"phase22-stress-{int(time.time())}"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "engine.txt"
    prompt = (
        f"Create the file {target} containing exactly 'vulkan ok'. "
        "Do not create or change anything else."
    )
    events = _sse(base, "/chat/stream", {"message": prompt})
    approval = next((e for e in events if e.get("type") == "action_required"), None)
    if approval is not None:
        events = _sse(
            base,
            "/chat/approve",
            {"approval_id": approval["approval_id"], "approved": True},
        )
    content = target.read_text(encoding="utf-8").strip() if target.is_file() else ""
    check("cli task wrote the file", content == "vulkan ok", repr(content))
    final = next((e for e in events if e.get("type") == "final"), {})
    check("final answer received", bool(final.get("reply")), final.get("reply", "")[:80])

    step("Repeated hot swaps (flush stress)")
    timings = []
    for _ in range(2):
        started = time.monotonic()
        again = _request(
            base, "/models/active", {"task": "text", "model_id": args.model}
        )
        timings.append(time.monotonic() - started)
        check("swap reported a flush", again.get("vram_flushed") is True)
    print("  swap seconds:", [round(item, 2) for item in timings])
    check("each swap under 60 s", all(item < 60 for item in timings))

    if previous and not args.keep_model:
        step(f"Restoring {previous}")
        try:
            _request(base, "/models/active", {"task": "text", "model_id": previous})
            check("restored previous model", True, previous)
        except Exception as exc:  # noqa: BLE001 - CLI
            check("restored previous model", False, str(exc))

    print("\nGUI/vision parity: run backend/tests/live_real_apps.py (same transport).")
    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} ({len(failures)} failure(s))")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
