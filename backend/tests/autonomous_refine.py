"""Phase 21 auto-refinement loop.

This is the orchestration layer around the experiment runner:

    run -> score -> write failure bundle -> (builder patch) -> restart -> rerun

It never edits production files by itself. When a run fails it emits a
structured bundle with the exact prompt, tool calls, failed checks, and event
log, then invokes an optional refinement command (the builder agent / OpenCode)
and restarts the service before the next iteration. That keeps unsupervised
code generation out of the loop while still making the loop autonomous when a
builder command is supplied.

Examples (from the repository's backend folder):

    # Safe CLI/edge loop against a private backend on port 8010
    .venv/Scripts/python.exe -m tests.autonomous_refine --iterations 3

    # Include real GUI automation while the desktop is idle
    .venv/Scripts/python.exe -m tests.autonomous_refine --iterations 2 --include-gui

    # Evaluate the packaged app, rebuild it after each failed iteration
    .venv/Scripts/python.exe -m tests.autonomous_refine --target app --rebuild --iterations 2

    # Hand failures to a builder command, then rerun the exact failures
    .venv/Scripts/python.exe -m tests.autonomous_refine --iterations 3 \
        --focus-failures \
        --refine-command "opencode run \"Fix the failures in {bundle} and rebuild Compa\""
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.autonomous_evaluate import (
    failure_bundle,
    render_failure_brief,
    render_summary,
    write_report,
)
from tests.autonomous_experiments import (
    build_experiments,
    keep_alive_payload_check,
    preflight_checks,
    run_suite,
)
from tests.autonomous_lib import (
    REPO_ROOT,
    BackendClient,
    BackendService,
    DesktopService,
    TEMP_ROOT,
    new_run_id,
)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_refine_command(
    template: str,
    *,
    bundle_path: Path,
    iteration: int,
    run_dir: Path,
    failed_ids: list[str],
) -> int:
    command = template.format(
        bundle=str(bundle_path),
        iteration=iteration,
        run_dir=str(run_dir),
        failed_ids=",".join(failed_ids),
    )
    print(f"\nRefinement command: {command}", flush=True)
    environment = os.environ.copy()
    environment.update(
        {
            "COMPA_BUNDLE": str(bundle_path),
            "COMPA_ITERATION": str(iteration),
            "COMPA_RUN_DIR": str(run_dir),
            "COMPA_FAILED_IDS": ",".join(failed_ids),
        }
    )
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(REPO_ROOT),
        env=environment,
        check=False,
    )
    return result.returncode


def _preflight(
    client: BackendClient,
    *,
    target: str,
    include_gui: bool,
    require_keep_alive_zero: bool,
    allow_cpu: bool,
    needs_model: bool,
) -> list[Any]:
    checks = preflight_checks(
        client,
        service_mode=target,
        include_gui=include_gui,
        require_keep_alive_zero=require_keep_alive_zero,
        allow_cpu=allow_cpu,
        needs_model=needs_model,
    )
    checks.append(keep_alive_payload_check())
    return checks


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - older consoles
        pass
    parser = argparse.ArgumentParser(
        description="Iteratively run, score, and refine Compa against real tasks."
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--target",
        choices=["backend", "app"],
        default="backend",
        help="backend = private uvicorn on --port; app = packaged Tauri app on 8000.",
    )
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--only", default="")
    parser.add_argument("--include-gui", action="store_true")
    parser.add_argument("--mutate", action="store_true")
    parser.add_argument("--keep-alive", type=int, default=0)
    parser.add_argument("--require-keep-alive-zero", action="store_true")
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Continue when Ollama is on CPU (much slower; use a higher --timeout).",
    )
    parser.add_argument("--focus-failures", action="store_true", help="After iteration 1, rerun only the previously failing ids.")
    parser.add_argument("--always-full", action="store_true", help="Always run the whole suite, even with --focus-failures.")
    parser.add_argument("--no-restart", action="store_true", help="Do not restart the service between iterations.")
    parser.add_argument("--rebuild", action="store_true", help="With --target app: rebuild the portable app each iteration.")
    parser.add_argument(
        "--refine-command",
        default="",
        help="Command run after a failed iteration. {bundle}, {iteration}, {run_dir}, {failed_ids} are substituted.",
    )
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.iterations < 1:
        print("--iterations must be at least 1", file=sys.stderr)
        return 2

    experiments = build_experiments(include_gui=args.include_gui)
    categories = args.category or ["static", "cli", "edge"] + (
        ["gui"] if args.include_gui else []
    )
    wanted_categories = {item.casefold() for item in categories}
    wanted_ids = {item.strip().casefold() for item in args.only.split(",") if item.strip()}
    selected = [
        experiment
        for experiment in experiments
        if experiment.category.casefold() in wanted_categories
        and (not wanted_ids or experiment.id.casefold() in wanted_ids)
    ]
    if not selected:
        print("No experiments selected.", file=sys.stderr)
        return 2

    run_id = new_run_id()
    root = Path(args.out_dir) if args.out_dir else (TEMP_ROOT / run_id)
    root.mkdir(parents=True, exist_ok=True)

    if args.target == "app":
        base_url = args.base_url or "http://127.0.0.1:8000"
        desktop = DesktopService()
        service = None
    else:
        base_url = args.base_url or f"http://127.0.0.1:{args.port}"
        desktop = None
        service = BackendService(
            base_url,
            port=args.port,
            keep_alive_seconds=args.keep_alive,
        )

    client = BackendClient(base_url)
    pending_ids: set[str] = set()
    latest_report: dict[str, Any] | None = None

    def ensure_target(iteration: int) -> None:
        if args.target == "app":
            assert desktop is not None
            if iteration == 1:
                if client.health():
                    print("Reusing the running packaged app.")
                else:
                    desktop.launch()
            elif not args.no_restart:
                print("Restarting the packaged app...", flush=True)
                desktop.stop()
                if args.rebuild:
                    print("Rebuilding the portable app...", flush=True)
                    desktop.rebuild()
                desktop.launch()
            return

        assert service is not None
        if iteration == 1:
            if service.client.health():
                service.force_stop_port()
            service.start()
        elif not args.no_restart:
            print("Restarting the private backend...", flush=True)
            service.restart(force=True)

    try:
        for iteration in range(1, args.iterations + 1):
            ensure_target(iteration)
            iteration_dir = root / f"iteration-{iteration:02d}"
            iteration_dir.mkdir(parents=True, exist_ok=True)

            preflight = _preflight(
                client,
                target=args.target,
                include_gui=args.include_gui,
                require_keep_alive_zero=args.require_keep_alive_zero,
                allow_cpu=args.allow_cpu,
                needs_model=any(
                    experiment.prompt_template is not None
                    for experiment in selected
                ),
            )
            failed_preflight = [
                item
                for item in preflight
                if not item.ok and item.dimension in {"accuracy", "performance", "ux"}
            ]
            if failed_preflight:
                for item in failed_preflight:
                    print(
                        f"Preflight FAIL {item.dimension}/{item.name}: {item.detail}",
                        file=sys.stderr,
                    )
                print(
                    "Refusing to score against a degraded environment. Fix the "
                    "preflight failure, or use --allow-cpu for a slow CPU run.",
                    file=sys.stderr,
                )
                return 2

            if not args.quiet:
                print(
                    f"\n=== Iteration {iteration}/{args.iterations} ===\n"
                    f"Run: {iteration_dir}  target={args.target}  backend={base_url}",
                    flush=True,
                )

            subset = selected
            if (
                args.focus_failures
                and not args.always_full
                and iteration > 1
                and pending_ids
            ):
                subset = [
                    experiment for experiment in selected if experiment.id in pending_ids
                ]
                if not subset:
                    subset = selected

            report = run_suite(
                subset,
                client=client,
                run_dir=iteration_dir,
                mutate=args.mutate,
                fail_fast=args.fail_fast,
                progress=not args.quiet,
            )
            report["preflight"] = [item.as_dict() for item in preflight]
            report["iteration"] = iteration
            report["target"] = args.target
            report["run_id"] = run_id
            report_path = iteration_dir / "report.json"
            write_report(report, report_path)
            latest_report = report

            summary = report["summary"]
            failed_records = [
                record for record in report["experiments"] if not record["passed"]
            ]
            if not args.quiet:
                print()
                print(render_summary(report))
                print(f"\nReport: {report_path}", flush=True)

            if not failed_records and not failed_preflight:
                print(f"\nAll {summary['total']} experiment(s) passed on iteration {iteration}.")
                pointer = TEMP_ROOT / "latest_report.txt"
                pointer.parent.mkdir(parents=True, exist_ok=True)
                pointer.write_text(str(report_path), encoding="utf-8")
                return 0

            pending_ids = {record["id"].split("__")[0] for record in failed_records}

            bundle = failure_bundle(report)
            bundle["preflight"] = [item.as_dict() for item in failed_preflight]
            bundle["iteration"] = iteration
            bundle_path = iteration_dir / "failures.json"
            _write_json(bundle_path, bundle)
            (iteration_dir / "failures.md").write_text(
                render_failure_brief(bundle),
                encoding="utf-8",
            )
            _write_json(TEMP_ROOT / "latest_failure_bundle.json", bundle)
            (TEMP_ROOT / "latest_report.txt").write_text(
                str(report_path), encoding="utf-8"
            )

            if iteration == args.iterations:
                break

            if not args.quiet:
                print(f"\nFailure bundle: {bundle_path}", flush=True)

            if args.refine_command:
                code = _run_refine_command(
                    args.refine_command,
                    bundle_path=bundle_path,
                    iteration=iteration,
                    run_dir=iteration_dir,
                    failed_ids=sorted(pending_ids),
                )
                if code != 0:
                    print(
                        f"Refinement command exited with {code}; continuing to the restart.",
                        file=sys.stderr,
                    )
            else:
                print(
                    "No --refine-command supplied. The failure bundle is ready for the "
                    "builder agent; the loop will rerun the experiment set after restart.",
                    file=sys.stderr,
                )

        print("\nIteration limit reached without a 100% pass.", file=sys.stderr)
        if latest_report is not None:
            print(f"Last report: {root}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
