"""Visual QA for Compa screenshots using the local vision model.

The Phase 24 loop is only honest if a *separate* model judges the pixels. This
script starts the configured llama.cpp vision model (Qwen3-VL by default), sends
each screenshot with a strict design rubric, and writes a machine-readable
verdict plus a readable Markdown report.

Usage (repo root):

    backend\\.venv\\Scripts\\python.exe scripts\\visual_qa.py --shots build\\phase24\\shots

Exit code is 0 only when every screenshot passes with no critical/major issue
and all four scores are at least the threshold (default 8).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The packaged app keeps models under the per-user data folder, not the repo.
_DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "com.personalagent.desktop" / "data"
if "PERSONAL_AGENT_DATA_DIR" not in os.environ and _DEFAULT_DATA.is_dir():
    os.environ["PERSONAL_AGENT_DATA_DIR"] = str(_DEFAULT_DATA)

from engine.service import EngineService  # noqa: E402
from settings import DATA_ROOT, load_settings  # noqa: E402

RUBRIC = """You are a senior product designer reviewing a screenshot of a
Windows 11 desktop app called Compa. It is a local AI assistant with two shells:
a classic chat window and a Raycast-style command palette.

Judge ONLY what is visible in the image. Ignore the wallpaper behind the window
and any content that is clearly placeholder text.

Evaluate four areas on a 1-10 scale:
1. typography  - crisp, readable, consistent size/weight hierarchy
2. spacing     - padding, margins, alignment, no cramped or floating elements
3. contrast    - readable text on its background, OLED black looks deliberate,
                 accent/status colors are legible, Mica looks clean (no haze,
                 no double borders, no muddy edges)
4. layout      - nothing clipped, overlapping, misaligned or awkwardly scrolled;
                 the composition feels balanced at this window size

Then list concrete defects. Only report a defect you can point at in the image.
Do not invent problems. A minor nit is acceptable; a major defect is something a
paying customer would notice immediately.

Answer with JSON only, no prose and no code fences:
{"verdict":"pass" or "fail",
 "scores":{"typography":0,"spacing":0,"contrast":0,"layout":0},
 "issues":[{"area":"typography|spacing|contrast|layout|content",
            "severity":"critical|major|minor",
            "where":"short location in the image",
            "problem":"what is wrong",
            "fix":"concrete CSS/React change"}],
 "best_thing":"the strongest visual quality",
 "worst_thing":"the single worst visual flaw"}

Use "fail" if any score is below 8 or any issue is critical or major. Use
"pass" only when the screen looks polished enough to ship as a premium app."""

FOLLOW_UP = """Look at the same screenshot again. Name the three biggest visual
flaws a demanding designer would fix first, ordered by impact. Be blunt and
specific about alignment, spacing, contrast or clipping. Answer in at most 80
words, plain text."""


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in model answer: {text[:300]!r}")
    return json.loads(cleaned[start : end + 1])


def _chat(
    base_url: str,
    prompt: str,
    image_b64: str | None,
    *,
    max_tokens: int,
    timeout: float,
    temperature: float = 0.1,
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            }
        )
    payload = {
        "model": "vision",
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"].get("content") or ""


def evaluate_shot(
    base_url: str,
    path: Path,
    *,
    max_tokens: int,
    timeout: float,
    follow_up: bool,
) -> dict[str, Any]:
    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    started = time.perf_counter()
    raw = _chat(base_url, RUBRIC, image_b64, max_tokens=max_tokens, timeout=timeout)
    elapsed = time.perf_counter() - started
    try:
        verdict = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        verdict = {
            "verdict": "fail",
            "scores": {},
            "issues": [
                {
                    "area": "content",
                    "severity": "critical",
                    "where": "model output",
                    "problem": f"could not parse the verdict ({exc})",
                    "fix": "re-run the evaluation",
                }
            ],
            "raw": raw[:1200],
        }
    notes = ""
    if follow_up:
        try:
            notes = _chat(
                base_url,
                FOLLOW_UP,
                image_b64,
                max_tokens=220,
                timeout=timeout,
                temperature=0.3,
            ).strip()
        except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover
            notes = f"follow-up failed: {exc}"
    return {
        "shot": path.name,
        "seconds": round(elapsed, 2),
        "verdict": verdict,
        "notes": notes,
        "raw": raw,
    }


def decide(result: dict[str, Any], threshold: int) -> tuple[bool, list[str]]:
    verdict = result.get("verdict") or {}
    reasons: list[str] = []
    scores = verdict.get("scores") or {}
    for area in ("typography", "spacing", "contrast", "layout"):
        try:
            value = int(scores.get(area, 0))
        except (TypeError, ValueError):
            value = 0
        if value < threshold:
            reasons.append(f"{area} score {value} < {threshold}")
    for issue in verdict.get("issues") or []:
        severity = str(issue.get("severity", "")).lower()
        if severity in {"critical", "major"}:
            reasons.append(f"{issue.get('area', '?')}: {issue.get('problem', '?')}")
    if str(verdict.get("verdict", "")).lower() != "pass":
        reasons.append("model verdict is not pass")
    return (not reasons, reasons)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = ["# Visual QA report", "", f"Generated: {report['generated_at']}", ""]
    summary = report["summary"]
    lines.append(
        f"**{summary['passed']}/{summary['total']} screenshots pass** "
        f"(threshold {report['threshold']})."
    )
    lines.append("")
    for result in report["results"]:
        verdict = result["verdict"]
        ok, reasons = decide(result, report["threshold"])
        lines.append(f"## {result['shot']} - {'PASS' if ok else 'FAIL'}")
        scores = verdict.get("scores") or {}
        if scores:
            lines.append(
                "Scores: "
                + ", ".join(f"{key} {scores.get(key, '?')}" for key in ("typography", "spacing", "contrast", "layout"))
            )
        if reasons:
            lines.append("")
            lines.append("Reasons: " + "; ".join(reasons))
        issues = verdict.get("issues") or []
        if issues:
            lines.append("")
            lines.append("| severity | area | where | problem | fix |")
            lines.append("| --- | --- | --- | --- | --- |")
            for issue in issues:
                lines.append(
                    "| {severity} | {area} | {where} | {problem} | {fix} |".format(
                        severity=str(issue.get("severity", "")).replace("|", "/"),
                        area=str(issue.get("area", "")).replace("|", "/"),
                        where=str(issue.get("where", "")).replace("|", "/"),
                        problem=str(issue.get("problem", "")).replace("|", "/"),
                        fix=str(issue.get("fix", "")).replace("|", "/"),
                    )
                )
        if verdict.get("best_thing"):
            lines.append("")
            lines.append(f"Best: {verdict['best_thing']}")
        if verdict.get("worst_thing"):
            lines.append(f"Worst: {verdict['worst_thing']}")
        if result.get("notes"):
            lines.append("")
            lines.append(f"Second look: {result['notes']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision QA for Compa screenshots")
    parser.add_argument("--shots", type=Path, default=REPO / "build" / "phase24" / "shots")
    parser.add_argument("--out", type=Path, default=REPO / "build" / "phase24")
    parser.add_argument("--threshold", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=700)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--follow-up",
        action="store_true",
        help="Also ask a free-text second question. The 8B model tends to invent\n        generic nits here, so it is off by default and never gates the verdict.",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Use an already running vision server instead of starting one.",
    )
    parser.add_argument("--keep-vision", action="store_true")
    args = parser.parse_args()

    shots = sorted(p for p in args.shots.glob("*.png")) if args.shots.is_dir() else []
    if not shots:
        print(f"no screenshots in {args.shots}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    engine: EngineService | None = None
    base_url = args.base_url
    if not base_url:
        settings = load_settings()
        engine = EngineService(settings.engine, DATA_ROOT / "engine-data")
        base_url = engine.ensure_vision()
        print(f"vision server: {base_url}")

    results: list[dict[str, Any]] = []
    try:
        for index, shot in enumerate(shots, 1):
            print(f"[{index}/{len(shots)}] {shot.name} ...", end="", flush=True)
            result = evaluate_shot(
                base_url,
                shot,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                follow_up=args.follow_up,
            )
            ok, reasons = decide(result, args.threshold)
            print(f" {'PASS' if ok else 'FAIL'} ({result['seconds']}s)")
            if not ok:
                for reason in reasons:
                    print(f"    - {reason}")
            results.append(result)
    finally:
        if engine is not None and not args.keep_vision:
            try:
                engine.shutdown()
            except Exception:  # noqa: BLE001 - cleanup must not mask results
                pass

    passed = sum(1 for item in results if decide(item, args.threshold)[0])
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "threshold": args.threshold,
        "shots_dir": str(args.shots),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "results": results,
    }
    json_path = args.out / "vision-report.json"
    md_path = args.out / "vision-report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(report, md_path)
    print(f"\n{passed}/{len(results)} pass -> {md_path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
