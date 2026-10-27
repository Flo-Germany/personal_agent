"""Native PowerShell access with a read-only query mode and gated actions.

``run_powershell_query`` is for inspection only and rejects a broad set of
state-changing cmdlets, aliases, redirection operators, and .NET mutation calls.
``run_powershell_action`` is unrestricted by design but is registered with the
HITL approval flow, so the user sees the command and the model's explanation
before anything runs.

Both modes use a strict timeout, kill the whole PowerShell process tree on
timeout, capture stdout/stderr, and truncate large output before it reaches the
model.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_COMMAND_CHARS = 8_000
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_OUTPUT_CHARS = 12_000

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Cmdlet verbs that change state. This is intentionally broad: a read-only
# query that trips it should be re-issued through run_powershell_action.
_MUTATING_CMDLET = re.compile(
    r"(?i)(?<![\w-])(?:add|block|clear|copy|disable|dismount|enable|export|"
    r"grant|import|initialize|install|lock|mount|move|new|optimize|register|"
    r"remove|rename|repair|reset|restart|resume|revoke|save|set|start|stop|"
    r"suspend|unblock|uninstall|unlock|unregister|update)-"
    r"[a-z][a-z0-9]*\b"
)

# A few cmdlets are read-only or formatting-only despite a mutating-looking
# verb; these exact names are exempted before the broad verb check.
_READ_ONLY_FORMATTING = re.compile(
    r"(?i)\b(?:Format-(?:Custom|List|Table|Wide)|Write-(?:Debug|Error|Host|"
    r"Information|Output|Progress|Verbose|Warning))\b"
)

# Command-line aliases and native tools that mutate system state.
_MUTATING_ALIAS = re.compile(
    r"(?i)(?:^|[;&|(`\s])(?:attrib|bcdedit|cacls|cmd|copy|del|diskpart|erase|"
    r"format|fsutil|icacls|kill|md|mkdir|mklink|move|msiexec|net|netsh|"
    r"powershell|pwsh|rd|reg|regedit|regsvr32|ren|rename|robocopy|rm|rmdir|"
    r"rundll32|sc|schtasks|set|setx|shutdown|start|taskkill|takeown|"
    r"vssadmin|wbadmin|wevtutil|winget|wmic|xcopy)\b"
)

_MUTATING_EXTRA = re.compile(
    r"(?i)\b(?:Format-Volume|Write-EventLog|Set-Variable|New-PSDrive|"
    r"Remove-PSDrive|Start-Transcript|Stop-Transcript)\b"
)

_MUTATING_REDIRECT = re.compile(r"(?:^|[^<>=])>{1,2}(?!=)")
_MUTATING_PARAMETER = re.compile(r"(?i)(?:-OutFile|-Destination)\b")
_MUTATING_DOTNET = re.compile(
    r"(?i)\[(?:system\.)?(?:io\.(?:file|directory)|diagnostics\.process|"
    r"environment|microsoft\.win32\.registry[^\]]*)\]\s*::\s*"
    r"(?:append|copy|create|delete|move|openwrite|replace|set|start|write)"
)
_MUTATING_METHOD = re.compile(
    r"(?i)\.(?:appendall|close|closemainwindow|copyto|create|createsubkey|"
    r"delete|deletesubkey|deletevalue|downloadfile|invoke|invokemember|kill|"
    r"move|openwrite|setvalue|start|writeall|writeline|write)\s*\("
)
_INVOKE_EXPRESSION = re.compile(
    r"(?i)(?:^|[;&|(`\s])(?:iex|invoke-expression|invoke-command|"
    r"invoke-cimmethod|invoke-wmimethod|add-type)\b"
)
_MUTATING_PROPERTY = re.compile(r"(?<![=<>!])\.[A-Za-z_][\w]*\s*=(?!=)")
_ENV_ASSIGNMENT = re.compile(r"(?i)\$env:[\w]+\s*=")

# Read-only queries are allowlisted, not denylisted. Anything that is not a
# Get-* cmdlet, one of these collectors/formatters, or a pure string/path
# helper is refused. This closes the "python -c ..." / "mshta ..." / Out-File
# hole that a verb-based denylist cannot cover.
_READ_ONLY_EXTRA = {
    "select-string",
    "select-object",
    "where-object",
    "sort-object",
    "foreach-object",
    "measure-object",
    "group-object",
    "compare-object",
    "out-string",
    "convertto-json",
    "convertto-csv",
    "convertto-xml",
    "convertfrom-json",
    "convertfrom-csv",
    "test-path",
    "test-connection",
    "test-netconnection",
    "resolve-path",
    "split-path",
    "join-path",
    "write-output",
    "write-host",
    "format-table",
    "format-list",
    "format-wide",
    "format-custom",
}
_READ_ONLY_GET = re.compile(r"(?i)^get-[a-z][a-z0-9]*$")
_READ_ONLY_FIRST_TOKEN = re.compile(r"[^\s|;]+")
_FORBIDDEN_IN_BLOCK = re.compile(
    r"(?i)(?:\b(?:invoke-|new-object|add-type|start-process|out-file|tee-object|"
    r"set-item|set-content|remove-item|copy-item|move-item|rename-item|"
    r"compress-archive|expand-archive|start-job|start-threadjob)\b|::|\$\(|`)"
)


def _strip_quoted(value: str) -> str:
    """Remove quoted strings so operators inside patterns are not false hits."""
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"`]|\\.)*\"", " ", value)


def _split_commands(command: str) -> list[str]:
    """Split on top-level ; | newline without breaking script blocks."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for char in command:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            continue
        if char in "({[":
            depth += 1
        elif char in ")}]" and depth:
            depth -= 1
        if depth == 0 and char in ";|\n":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts


def _allowlist_violation(command: str) -> str | None:
    """Fail closed unless every pipeline stage is an allowlisted read cmdlet."""
    raw = _strip_quoted(command)
    if "&&" in raw or "||" in raw or "&" in raw:
        return "command chaining"
    if "`" in raw or "$(" in raw:
        return "expression or subexpression execution"
    if _ENV_ASSIGNMENT.search(raw):
        return "environment assignment"
    for part in _split_commands(command):
        segment = part.strip()
        if not segment:
            continue
        if segment[0] in "&.@$({[":
            return "command invocation or expression"
        match = _READ_ONLY_FIRST_TOKEN.match(segment)
        if not match:
            return "empty command segment"
        token = match.group(0)
        leaf = token.rsplit("\\", 1)[-1].casefold()
        if leaf not in _READ_ONLY_EXTRA and not _READ_ONLY_GET.match(leaf):
            return f"'{token}' is not an allowlisted read-only cmdlet"
        block = _strip_quoted(segment)
        if _FORBIDDEN_IN_BLOCK.search(block):
            return "state-changing command inside a query"
        if _MUTATING_METHOD.search(block) or _MUTATING_REDIRECT.search(block):
            return "state-changing method or redirection"
        if _MUTATING_PARAMETER.search(block):
            return "file-writing parameter"
    return None


def _powershell_executable() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return f"{text[:limit]}\n...[truncated {omitted} characters]...", True


def _error_excerpt(text: str, limit: int = 300) -> str:
    """Return the first meaningful PowerShell error line, not stack position."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("At line:", "+", "CategoryInfo", "FullyQualifiedErrorId")):
            continue
        return stripped[:limit]
    return ""


def _failure_hint(stderr: str) -> str | None:
    lowered = stderr.casefold()
    if "no process found" in lowered or "cannot find a process" in lowered:
        return (
            "The PID or process name is stale or wrong. Re-query the current "
            "process list and retry with the exact PID/name."
        )
    if "access is denied" in lowered or "cannot stop process" in lowered:
        return (
            "Windows denied access. The process may be elevated or protected; "
            "try another method, or explain that administrator rights are required."
        )
    if "does not exist" in lowered or "not found" in lowered:
        return "The target no longer exists. Verify the current state before retrying."
    return None


def _read_only_violation(command: str) -> str | None:
    """Return a human-readable reason when a query looks state-changing."""
    # Format-Table/Write-Output are read-only even though their verb looks
    # mutating; remove those exact cmdlets before scanning the rest.
    scan = _READ_ONLY_FORMATTING.sub("", command)
    checks = (
        ("state-changing cmdlet", _MUTATING_CMDLET),
        ("state-changing command or alias", _MUTATING_ALIAS),
        ("state-changing command", _MUTATING_EXTRA),
        ("output redirection or file write", _MUTATING_REDIRECT),
        ("file-writing parameter", _MUTATING_PARAMETER),
        ("mutating .NET call", _MUTATING_DOTNET),
        ("mutating method call", _MUTATING_METHOD),
        ("mutating property assignment", _MUTATING_PROPERTY),
        ("dynamic code execution", _INVOKE_EXPRESSION),
    )
    for label, pattern in checks:
        if pattern.search(scan):
            return label
    return _allowlist_violation(command)


def _bounded_communicate(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    hard_limit_chars: int,
) -> tuple[str, str, bool, bool]:
    """Read both pipes with a hard memory cap; kill the tree when exceeded.

    ``communicate()`` buffers everything before truncation, so a recursive
    directory listing could exhaust RAM. This keeps at most ~4x the configured
    output limit and stops the process once the cap is crossed.
    """
    import threading

    out: list[str] = []
    err: list[str] = []
    size = [0]
    capped = [False]

    def reader(stream, sink: list[str]) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                if size[0] >= hard_limit_chars:
                    capped[0] = True
                    if process.poll() is None:
                        _kill_process_tree(process)
                    continue
                sink.append(chunk)
                size[0] += len(chunk)
        except (OSError, ValueError):
            return

    threads = [
        threading.Thread(target=reader, args=(process.stdout, out), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, err), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for thread in threads:
        thread.join(timeout=2)
    return "".join(out), "".join(err), timed_out, capped[0]


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                creationflags=_CREATE_NO_WINDOW,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        process.kill()
    except OSError:
        pass


def _run(
    command: str,
    *,
    mode: str,
    timeout: float,
    max_output_chars: int,
) -> dict[str, Any]:
    command = str(command or "").strip()
    if not command:
        return _error(mode, command, "The PowerShell command must not be empty.")
    if len(command) > MAX_COMMAND_CHARS:
        return _error(
            mode,
            command,
            f"The PowerShell command is too long (limit {MAX_COMMAND_CHARS} characters).",
        )

    timeout = max(0.5, min(float(timeout), 300.0))
    max_output_chars = max(1_000, min(int(max_output_chars), 100_000))

    error_action = "Continue" if mode == "query" else "Stop"
    script = (
        "$ProgressPreference='SilentlyContinue';"
        f"$ErrorActionPreference='{error_action}';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"{command}"
    )
    args = [
        _powershell_executable(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    environment = os.environ.copy()
    environment.setdefault("POWERSHELL_TELEMETRY_OPTOUT", "1")
    environment.setdefault("POWERSHELL_UPDATECHECK", "Off")

    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            args,
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            env=environment,
        )
    except OSError as exc:
        return _error(
            mode,
            command,
            f"Could not start PowerShell: {exc}",
        )

    stdout, stderr, timed_out, output_capped = _bounded_communicate(
        process,
        timeout=timeout,
        hard_limit_chars=max_output_chars * 4 + 65_536,
    )

    duration_ms = (time.perf_counter() - started) * 1000
    stdout, stdout_truncated = _truncate(stdout or "", max_output_chars)
    stderr, stderr_truncated = _truncate(stderr or "", max_output_chars)
    exit_code = process.returncode
    ok = not timed_out and exit_code == 0
    truncated = stdout_truncated or stderr_truncated

    if timed_out:
        message = (
            f"PowerShell {mode} timed out after {timeout:g} seconds and was "
            "terminated."
        )
    elif ok:
        message = f"PowerShell {mode} completed in {duration_ms:.0f} ms."
    else:
        message = f"PowerShell {mode} failed with exit code {exit_code}."
        excerpt = _error_excerpt(stderr)
        if excerpt:
            message += f" {excerpt}"

    if truncated:
        message += " Output was truncated."
    if output_capped:
        message += (
            " The command produced far more output than the limit, so it was "
            "stopped early; narrow the query."
        )

    result = {
        "ok": ok,
        "mode": mode,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "truncated": truncated,
        "timeout": timed_out,
        "output_capped": output_capped,
        "duration_ms": round(duration_ms, 1),
        "message": message,
    }
    if not ok:
        hint = _failure_hint(stderr)
        if hint:
            result["hint"] = hint
    return result


def _error(mode: str, command: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": mode,
        "command": command,
        "error": message,
        "message": message,
        "timeout": False,
        "truncated": False,
    }


def run_powershell_query(
    command: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Run a read-only PowerShell inspection command without approval."""
    command = str(command or "").strip()
    violation = _read_only_violation(command)
    if violation is not None:
        return _error(
            "query",
            command,
            "run_powershell_query is read-only and rejected this command "
            f"because it looks like a {violation}. Use run_powershell_action "
            "for state changes; it requires user approval.",
        )

    return _run(
        command,
        mode="query",
        timeout=timeout,
        max_output_chars=max_output_chars,
    )


def run_powershell_action(
    command: str,
    *,
    description: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Run a state-changing PowerShell command after HITL approval."""
    if not str(description or "").strip():
        return _error(
            "action",
            str(command or ""),
            "A description of exactly what the action will change is required.",
        )
    return _run(
        command,
        mode="action",
        timeout=timeout,
        max_output_chars=max_output_chars,
    )
